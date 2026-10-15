"""适配器基类与统一接口。

所有平台适配器都实现 :class:`PlatformAdapter`，把各自平台的 API 差异
（字段名、分页方式、URL 规则、鉴权头）封装在内部，向上只暴露统一模型
（:class:`~fissue.models.RawItem`）。
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

import httpx

from ..config import RepoConfig
from ..errors import PlatformError, RateLimitError
from ..logging_setup import get_logger
from ..models import AuthorKind, FileChange, ItemType, Platform, RawItem, RepoRef
from ..net import httpx_verify, is_ssl_error, ssl_help_text

log = get_logger(__name__)

# 单次 HTTP 请求默认超时
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
# 分页默认页大小
DEFAULT_PER_PAGE = 50
# 被限流时的默认等待上限（秒）。平台给的 `Retry-After` / `X-RateLimit-Reset`
# 可能是个巨大的值（GitHub 的 reset 常常在一小时之后），照等会让整条流水线
# 表现为「莫名卡住」。覆盖项见 Settings.platforms.max_retry_after_seconds。
DEFAULT_MAX_RETRY_AFTER = 300.0
# 幂等的 HTTP 方法：失败可以安全重放（重放不会产生第二份副作用）。
# POST/PATCH 不在其中——它们可能已经在上游执行成功，只是响应丢在回来的路上。
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# 仓库标签名缓存的默认 TTL（秒）。覆盖项见 Settings.platforms.label_cache_ttl_seconds。
LABEL_CACHE_TTL_SECONDS = 600.0
# 标签缓存条目数上限：长跑进程里仓库数只增不减，需封顶（过期者优先淘汰）。
LABEL_CACHE_MAX_ENTRIES = 64


class PlatformAdapter(ABC):
    """代码托管平台的统一接口。

    子类需要实现 :meth:`_build_client`、:meth:`_headers`、:meth:`fetch_items` 等抽象成员。
    """

    platform: Platform

    def __init__(
        self,
        *,
        token: str = "",
        api_base: str | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        max_retries: int = 4,
        timeout: httpx.Timeout | None = None,
        label_cache_ttl: float | None = None,
        max_retry_after: float | None = None,
    ) -> None:
        self.token = token
        self.api_base = (api_base or self.default_api_base).rstrip("/")
        self.per_page = per_page
        self.max_retries = max_retries
        self.timeout = timeout or DEFAULT_TIMEOUT
        #: 被限流时单次最多等多久（秒）。平台给的 `Retry-After` / `X-RateLimit-Reset`
        #: 是「建议值」，照等没有上限——最坏能把整条流水线按住一小时。
        self.max_retry_after = float(
            DEFAULT_MAX_RETRY_AFTER if max_retry_after is None else max_retry_after
        )
        self._client: httpx.AsyncClient | None = None
        #: 仓库标签名缓存的存活时长（秒）。``<= 0`` 表示不缓存，每次都拉。
        self.label_cache_ttl: float = (
            LABEL_CACHE_TTL_SECONDS if label_cache_ttl is None else float(label_cache_ttl)
        )
        # 仓库标签名缓存：``"owner/name" -> ({标签名}, 写入时刻)``。
        # 打标签是批量的（一次 flush 给几十条各打若干标签），而每次
        # ``ensure_label`` 都要先拉一遍全量标签列表（还有分页）。
        # 不回填这个缓存就会产生「同一份标签列表被反复 GET」的 O(条目) 次请求。
        # 带 TTL 是因为常驻服务长跑：平台侧新建的标签若被永久判为「不存在」，
        # 会一直尝试创建并吃到 422，进而拖垮整条打标签链路。
        # 时刻用 ``time.monotonic()``：不受系统时钟回拨影响。
        self._label_cache: dict[str, tuple[set[str], float]] = {}
        # 惰性创建：适配器可能在事件循环之外构造，避免绑定错误的 loop
        self._label_lock: asyncio.Lock | None = None

    # -- 子类必须提供 ------------------------------------------------------

    default_api_base: str = ""

    @abstractmethod
    def _headers(self) -> dict[str, str]:
        """构造鉴权与 Accept 头。"""

    @abstractmethod
    async def fetch_items(
        self,
        repo: RepoRef,
        *,
        item_types: Iterable[ItemType] = (ItemType.ISSUE, ItemType.PR),
        since: datetime | None = None,
        limit: int | None = None,
        state: str = "all",
    ) -> AsyncIterator[RawItem]:
        """按仓库拉取条目（异步流式）。"""
        raise NotImplementedError
        yield  # pragma: no cover - 使函数成为 async generator

    @abstractmethod
    def item_url(self, repo: RepoRef, number: int, item_type: ItemType) -> str:
        """条目的网页地址。"""

    @abstractmethod
    async def fetch_diff(self, repo: RepoRef, number: int) -> str:
        """拉取 PR 的完整 diff/patch。"""

    # -- 通用能力（子类可覆写） -------------------------------------------

    async def get_default_branch(self, repo: RepoRef) -> str:
        """仓库默认分支。"""
        raise PlatformError(f"{self.platform.display} 适配器未实现默认分支探测")

    async def whoami(self) -> str:
        """当前 token 对应的用户名（用于判断 PR 是否自己提的）。"""
        raise PlatformError(f"{self.platform.display} 适配器未实现 whoami")

    async def fetch_repo_labels(self, repo: RepoRef) -> list[str]:
        """仓库已有标签名列表（用于复用而非新建）。"""
        return []

    # -- 标签：进程内缓存 + 批量确保 --------------------------------------

    def _label_key(self, repo: RepoRef) -> str:
        return f"{repo.owner}/{repo.name}"

    def _label_guard(self) -> asyncio.Lock:
        """惰性创建互斥锁（适配器可能在事件循环外构造）。"""
        if self._label_lock is None:
            self._label_lock = asyncio.Lock()
        return self._label_lock

    def _label_entry(self, key: str, *, now: float | None = None) -> set[str] | None:
        """取未过期的缓存标签集合；过期则删掉并返回 ``None``。

        惰性淘汰：没有后台清理任务，由每次读写顺手判断——
        调用点少、开销可忽略，也避免了「定时器绑在错误的 loop 上」这类坑。
        """
        entry = self._label_cache.get(key)
        if entry is None:
            return None
        names, stored_at = entry
        ttl = self.label_cache_ttl
        if ttl <= 0:                       # 不缓存：留在这里只会永远判为过期
            self._label_cache.pop(key, None)
            return None
        age = (time.monotonic() if now is None else now) - stored_at
        if age >= ttl:
            self._label_cache.pop(key, None)
            return None
        return names

    def _store_label_entry(self, key: str, names: set[str]) -> None:
        """写入/刷新缓存条目，并在超上限时淘汰最陈旧的那些。"""
        self._label_cache[key] = (names, time.monotonic())
        if len(self._label_cache) > LABEL_CACHE_MAX_ENTRIES:
            # 按写入时刻排序淘汰一半：留下的都是较新的，避免每次都淘汰
            # 导致「刚写入就被踢」的抖动。
            stale = sorted(self._label_cache.items(), key=lambda kv: kv[1][1])
            for k, _ in stale[: len(stale) // 2]:
                self._label_cache.pop(k, None)

    def label_cache_age(self, repo: RepoRef) -> float | None:
        """缓存条目的已存活秒数；未缓存（或未配 TTL）返回 ``None``。

        供调用方决定「是否需要先对齐一次远端」——比按批号/条目数猜更稳。
        """
        if self.label_cache_ttl <= 0:
            return None
        entry = self._label_cache.get(self._label_key(repo))
        return None if entry is None else time.monotonic() - entry[1]

    async def cached_repo_labels(self, repo: RepoRef, *, refresh: bool = False) -> set[str]:
        """仓库标签名集合，**进程内按 TTL 复用**。

        同一进程里打标签是批量行为（一次 flush 几十条），而标签列表在整批内
        几乎不变。不缓存的话每条、每个标签都会触发一次（带分页的）全量 GET。
        缓存超过 :attr:`label_cache_ttl` 秒后自动失效重拉（常驻服务长跑时，
        平台侧新建/删除的标签才能被看见）。

        ``refresh=True`` 强制重新拉取并**顺带把写入时刻推到现在**——
        整批打标签前显式刷新一次，可以让「刷新 → 打完这一批」之间不再
        被 TTL 拦腰打断（原来用 ``invalidate`` 清空后，紧接着的一次
        拉取仍会把时间戳记在刷新之前，长批次中间又会过期再拉一遍）。
        """
        key = self._label_key(repo)
        if not refresh and (names := self._label_entry(key)) is not None:
            return set(names)
        async with self._label_guard():
            if not refresh and (names := self._label_entry(key)) is not None:
                return set(names)          # 双检：并发下只拉一次
            fetched = set(await self.fetch_repo_labels(repo))
            self._store_label_entry(key, fetched)
            return set(fetched)

    def invalidate_label_cache(self, repo: RepoRef | None = None) -> None:
        """失效标签缓存（``None`` 表示全部）。"""
        if repo is None:
            self._label_cache.clear()
        else:
            self._label_cache.pop(self._label_key(repo), None)

    async def ensure_label(self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI") -> None:
        """确保标签存在（不存在则创建）。走缓存，不重复拉取标签列表。"""
        await self.ensure_labels(repo, [label], color=color, description=description)

    async def ensure_labels(
        self, repo: RepoRef, labels: Iterable[str], *, color: str = "0e8a16", description: str = "Fissue AI"
    ) -> None:
        """确保一批标签存在：**一次**拉取列表，只创建缺失的那些。

        与「逐个 ``ensure_label``」相比，标签数量不再放大请求数——
        原来 N 个标签要 N 次列表 GET，现在固定 1 次（命中缓存则 0 次）。
        """
        wanted = [str(lbl) for lbl in labels if str(lbl).strip()]
        if not wanted:
            return
        existing = await self.cached_repo_labels(repo)
        if all(lbl in existing for lbl in wanted):
            return
        # 创建阶段串行化：避免并发下同一标签被重复创建（平台会回 422 噪音）。
        async with self._label_guard():
            key = self._label_key(repo)
            # 锁内重读：等锁期间可能已有别的协程建好并回填了缓存（TTL 可能已到点）。
            current = self._label_entry(key) or existing
            created = False
            failed: list[str] = []
            for lbl in wanted:
                if lbl in current:
                    continue
                if await self._create_label(repo, lbl, color=color, description=description):
                    current.add(lbl)
                    created = True
                else:
                    failed.append(lbl)
            if created:
                # 回填并续期：刚 ensure 过的那批标签在 TTL 内无须再查。
                self._store_label_entry(key, current)
            if failed:
                # 失败的标签**绝不能**写进「已存在」缓存：写进去等于接下来整个 TTL
                # 都认为它存在、不再尝试创建，于是随后的每一次打标签都静默失败，
                # 而打标签正是「结论落地」的那一步。
                log.warning(
                    "[%s] %d 个标签未能确保存在（不计入缓存，稍后会重试）：%s",
                    self.platform.value, len(failed), "、".join(failed[:5]),
                )

    async def _create_label(
        self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI"
    ) -> bool:
        """创建单个标签（子类实现），返回**「该标签现在确实存在」**。

        基类无标签能力时视作空操作，返回 True。子类务必不要吞掉失败后返回
        True——调用方正是据此决定要不要写入「已存在」缓存。
        """
        return True

    async def add_labels(self, repo: RepoRef, number: int, labels: list[str], item_type: ItemType = ItemType.ISSUE) -> None:
        """给条目加标签。"""
        raise PlatformError(f"{self.platform.display} 适配器未实现打标签")

    async def comment(self, repo: RepoRef, number: int, body: str, item_type: ItemType = ItemType.ISSUE) -> None:
        """在条目下发表评论。"""
        raise PlatformError(f"{self.platform.display} 适配器未实现评论")

    async def close_item(self, repo: RepoRef, number: int, item_type: ItemType = ItemType.ISSUE) -> None:
        raise PlatformError(f"{self.platform.display} 适配器未实现关闭条目")

    # -- 仓库操作（自动修复用） --------------------------------------------

    async def fork_repo(self, repo: RepoRef) -> RepoRef:
        """Fork 仓库，返回 fork 后的引用。"""
        raise PlatformError(f"{self.platform.display} 适配器未实现 fork")

    async def find_open_pr(self, upstream: RepoRef, *, head: str) -> dict[str, Any] | None:
        """按 head 查**仍开放**的 PR，没有则返回 None。

        这是「同一 head 不重复建 PR」与「已有 PR 就别再推送」的共同依据。默认返回
        None（不确定即当作没有），由各平台覆写；未覆写的平台行为与从前一致。
        """
        return None

    async def create_pr(
        self,
        upstream: RepoRef,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> dict[str, Any]:
        """向上游提 PR，返回 ``{"number":…, "url":…}``。"""
        raise PlatformError(f"{self.platform.display} 适配器未实现创建 PR")

    # -- Git 远端 URL（克隆 / 推送，供自动修复使用） ----------------------

    web_host: str = ""          # 例：github.com
    token_user: str = "oauth2"  # 带 token 的 HTTPS 用户名约定

    def clone_url(self, repo: RepoRef, *, use_token: bool = False) -> str:
        """HTTPS 克隆地址；``use_token`` 为真时内嵌令牌（仅用于本地 git 操作，勿打印）。"""
        if not self.web_host:
            raise PlatformError(f"{self.platform.display} 适配器未设置 web_host")
        if use_token and self.token:
            return f"https://{self.token_user}:{self.token}@{self.web_host}/{repo.owner}/{repo.name}.git"
        return f"https://{self.web_host}/{repo.owner}/{repo.name}.git"

    #: 短于此长度的「令牌」不参与脱敏。真实平台令牌都是二三十字符起步，而把
    #: 一两个字符当令牌做全局替换，会把正常文本打成筛子——例如 token="t" 时
    #: `truststore` 会变成 `***rus***ore`，日志反而读不懂了。
    MIN_REDACT_LEN = 8

    def sanitize(self, text: str) -> str:
        """把日志/报告里可能出现的令牌抹掉。"""
        if not self.token or len(self.token) < self.MIN_REDACT_LEN:
            return text
        if self.token in text:
            return text.replace(self.token, "***")
        return text

    # -- 生命周期 ---------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._headers(),
            follow_redirects=True,
            verify=httpx_verify(),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "PlatformAdapter":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- HTTP 辅助 --------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
        probe: Callable[[], Awaitable[Any]] | None = None,
        return_headers: bool = False,
        expect_text_prefix: tuple[str, ...] | None = None,
    ) -> Any:
        """带重试与限流处理的请求。

        **重试只对「能确定没生效」的失败执行**：

        * 幂等方法（GET/HEAD/PUT/DELETE…）任何可重试失败都重放；
        * 非幂等写方法（POST/PATCH）只在**连接阶段**失败（请求根本没发出去、
          例如连不上或建连超时）才重放。读超时与服务端 5xx 都可能「上游其实
          已经执行成功了，只是响应没回来」，此时先调用 ``probe`` 确认是否已
          生效：已生效就直接返回其结果，否则抛错——**绝不再发一次**。

        否则，一次网关 502 或者慢 POST 的读超时，就会在真实仓库上多出一个
        重复的 PR 或重复的评论，公开且撤不回来。

        ``return_headers=True`` 时返回 ``(载荷, 响应头)``：`Link` 头只存在于响应里，
        分页需要读它。

        ``expect_json=False`` 时可用 ``expect_text_prefix`` 做形态校验：平台在限流或
        登录失效时可能回 200 + 一个 HTML 页，不校验就会被当成 diff 一路带到下游
        （`git apply` 阶段）才炸，错误现场离病根很远。
        """
        last_exc: Exception | None = None
        idempotent = method.upper() in _IDEMPOTENT_METHODS
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self.client.request(
                    method, url, params=params, json=json, data=data, headers=headers
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                # TLS/证书问题重试没有意义（证书不会自己变好），立即失败并给出可操作提示，
                # 否则会白等 4 轮退避（约 8 秒）才报同一个错。
                if is_ssl_error(exc):
                    raise PlatformError(
                        self.sanitize(
                            f"{self.platform.display} 连接失败（{method} {url}）：{exc}\n{ssl_help_text()}"
                        )
                    ) from exc
                if not idempotent and not self._is_connect_phase_error(exc):
                    return await self._settle_ambiguous_write(
                        method, url, exc, probe=probe, platform=self.platform
                    )
                log.warning(
                    "[%s] 请求异常（第 %d 次）：%s %s -> %s",
                    self.platform.value, attempt, method, url, self.sanitize(str(exc)),
                )
                await asyncio.sleep(self._backoff(attempt))
                continue

            if self._is_rate_limited(resp):
                retry_after = _parse_retry_after(resp, max_seconds=self.max_retry_after)
                if attempt >= self.max_retries:
                    raise RateLimitError(
                        self.sanitize(f"{self.platform.display} 触发限流：{url}"), retry_after
                    )
                # 限流是服务端**明确拒绝**了这次请求，写操作没有被执行 → 重放安全。
                log.warning(
                    "[%s] 限流，%.1fs 后重试（上限 %.0fs）",
                    self.platform.value, retry_after, self.max_retry_after,
                )
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code in (500, 502, 503, 504):
                last_exc = PlatformError(f"{self.platform.display} 服务端错误 {resp.status_code}")
                if not idempotent:
                    return await self._settle_ambiguous_write(
                        method, url, last_exc, probe=probe, platform=self.platform
                    )
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code >= 400:
                raise PlatformError(
                    self.sanitize(
                        f"{self.platform.display} API 错误 {resp.status_code}："
                        f"{method} {url} -> {resp.text[:500]}"
                    )
                )

            if not expect_json:
                payload: Any = resp.text
                # 形态校验：空正文不算异常（例如「这个 PR 没有改动」），
                # 但非空却不像预期文本，就是平台给了个别的东西（多半是登录页/限流页）。
                if (
                    expect_text_prefix
                    and payload.strip()
                    and not payload.lstrip().startswith(expect_text_prefix)
                ):
                    raise PlatformError(
                        self.sanitize(
                            f"{self.platform.display} 返回的不是预期文本（{method} {url}）："
                            f"期望以 {expect_text_prefix[0]!r} 开头，实际以 {payload[:80]!r} 开头"
                        )
                    )
            elif not resp.content:
                payload = None
            else:
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise PlatformError(
                        self.sanitize(f"{self.platform.display} 返回非 JSON：{resp.text[:300]}")
                    ) from exc
            # `Link` 头只存在于响应里，分页要用它，所以允许调用方连头一起拿走。
            return (payload, resp.headers) if return_headers else payload

        raise PlatformError(
            self.sanitize(f"{self.platform.display} 请求最终失败：{method} {url}（{last_exc}）")
        )

    @staticmethod
    def _is_connect_phase_error(exc: BaseException) -> bool:
        """请求是否**根本没送到服务端**（只有这类写请求失败可以安全重放）。

        连不上 / 建连超时 / 连接池取不到连接，都发生在请求体发出之前，
        因此上游不可能已经执行过。
        """
        return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        """这次响应算不算「被限流」。

        不能只看 429：GitHub/Gitee 都会用 **403 + 正文说明**表示限流，而正文
        可能被本地化；配额用尽时 GitHub 还会给出 `X-RateLimit-Remaining: 0`。
        少认一种信号，就会把它当成普通硬错误——既不退避也不重试，直接抛错。
        """
        if resp.status_code == 429:
            return True
        if resp.status_code != 403:
            return False
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        if resp.headers.get("retry-after"):
            return True
        text = (resp.text or "").lower()
        return "rate limit" in text or "rate_limit" in text or "限流" in text

    @staticmethod
    def _is_already_exists(exc: BaseException) -> bool:
        """平台是否明确告知「这东西已经存在」。

        `_request` 的错误文本统一带上了状态码，409/422 都表示已存在。这类
        「失败」其实等价于成功（标签确实在那儿），不该被记成创建失败。
        """
        text = str(exc)
        return "API 错误 409" in text or "API 错误 422" in text

    async def _settle_ambiguous_write(
        self,
        method: str,
        url: str,
        exc: BaseException,
        *,
        probe: Callable[[], Awaitable[Any]] | None,
        platform: Platform,
    ) -> Any:
        """写请求失败且**可能已经生效**时的收尾：先查，再决定。

        ``probe`` 返回非 None 表示「这次写其实已经生效了」，直接把它当作响应
        返回；返回 None 或压根没给 probe，则抛错让人看到——宁可报错，也不
        盲目重发一个可能已经生效的写请求。
        """
        if probe is not None:
            try:
                existing = await probe()
            except Exception as probe_exc:      # 探测本身失败 → 当作没探测到
                log.warning(
                    "[%s] 写请求幂等探测失败（%s）：%s",
                    platform.value, method, self.sanitize(str(probe_exc)),
                )
                existing = None
            if existing is not None:
                log.info(
                    "[%s] 写请求 %s 未收到响应（%s），但探测到已生效，按成功返回（不重发）",
                    platform.value, method, self.sanitize(str(exc)),
                )
                return existing
        raise PlatformError(
            self.sanitize(
                f"{self.platform.display} 写请求失败且无法确认是否已生效（{method} {url}）：{exc}。"
                "为避免重复创建，本次不再重试；请到平台确认后手工处理。"
            )
        ) from exc

    async def _get(self, url: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        # 注意：**空 dict 不能往外传**。httpx 收到 `params={}` 时会用它覆盖 URL 上
        # 原有的查询串（`Request("GET", "…/x?page=2", params={}).url` → `…/x`），
        # 于是「把完整 URL 直接拿来再请求」的用法会静默丢参数。
        return await self._request("GET", url, params=clean or None)

    async def _get_with_headers(self, url: str, **params: Any) -> tuple[Any, httpx.Headers]:
        """GET 一次，连响应头一起返回（``Link`` 头分页要用）。"""
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", url, params=clean or None, return_headers=True)

    async def _paginate(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        style: str = "page",
        max_pages: int = 200,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """通用分页。

        :param style: ``page`` 与 ``offset`` 都是 page/per_page（GitHub、Gitee、
            AtomGit、GitLab 均支持，故走同一条分支）；``link`` 则跟随响应里的
            `Link` 头（GitHub 风格）。
        """
        base_params = dict(params or {})
        page = 1
        next_url: str | None = None
        while page <= max_pages:
            if style == "link":
                # GitHub 风格：页码不在请求参数里，而在响应的 `Link` 头里
                # （`<https://…&page=2>; rel="next"`）。所以第一页不带 page，
                # 之后直接沿用 next 给出的**完整 URL**——它已带全查询参数。
                payload, resp_headers = await self._get_with_headers(
                    next_url or url,
                    **({} if next_url else {**base_params, "per_page": self.per_page}),
                )
            else:
                payload = await self._get(
                    url, **{**base_params, "page": page, "per_page": self.per_page}
                )
            if payload is None:
                return
            batch = payload if isinstance(payload, list) else payload.get("items", [])
            if not isinstance(batch, list) or not batch:
                return
            yield batch

            if style == "link":
                next_url = _next_link(resp_headers)
                if not next_url:
                    return          # 没有 rel="next" → 这就是最后一页
            elif len(batch) < self.per_page:
                return              # 不满一页 → 到底了
            page += 1

    @staticmethod
    def _backoff(attempt: int) -> float:
        """指数退避 + 抖动。"""
        return min(2 ** (attempt - 1) * 0.5, 16.0) + random.uniform(0, 0.4)


# ---------------------------------------------------------------------------
# 共享工具
# ---------------------------------------------------------------------------


def _next_link(headers: httpx.Headers) -> str | None:
    """从 `Link` 响应头里取出 ``rel="next"`` 的 URL（GitHub 风格分页）。

    形如::

        <https://api.github.com/repos/o/r/issues?page=2>; rel="next",
        <https://api.github.com/repos/o/r/issues?page=9>; rel="last"

    没有 next（即最后一页）时返回 None。
    """
    raw = headers.get("link") or ""
    for part in raw.split(","):
        segments = [s.strip() for s in part.split(";")]
        if len(segments) < 2:
            continue
        target = segments[0]
        if not (target.startswith("<") and target.endswith(">")):
            continue
        if any(s in ('rel="next"', "rel=next") for s in segments[1:]):
            return target[1:-1]
    return None


def _parse_retry_after(resp: httpx.Response, *, max_seconds: float | None = None) -> float:
    """解析「过多久再来」，并做上限封顶。

    三种形态都要认，否则就会在仍被限流时过早重试，或干脆卡住：

    * `Retry-After: 30`（秒）；
    * `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`（HTTP-date）——此前解析不了，
      会退化成固定 5 秒；
    * `X-RateLimit-Reset`（unix 时间戳）——**唯一**可能算出超长等待的分支，
      GitHub 的 reset 常常在一小时之后，照等会让流水线表现为「莫名卡住」。
    """
    raw = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset")
    seconds: float | None = None
    if raw:
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            seconds = _http_date_delay(text)
        else:
            seconds = (
                value - datetime.now(timezone.utc).timestamp()
                if value > 1_000_000_000     # unix 时间戳
                else value
            )
    if seconds is None:
        seconds = 5.0
    seconds = max(1.0, seconds)
    if max_seconds is not None:
        seconds = min(seconds, max(1.0, float(max_seconds)))
    return seconds


def _http_date_delay(text: str) -> float | None:
    """HTTP-date 形式的 `Retry-After` → 还需要等多少秒；解析不了返回 None。"""
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp() - datetime.now(timezone.utc).timestamp()


def parse_dt(value: Any) -> datetime | None:
    """宽松解析时间字符串。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    # 常见 ISO 8601，含 Z 结尾
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def guess_author_kind(
    username: str,
    *,
    body: str = "",
    labels: Iterable[str] = (),
    maintainers: Iterable[str] = (),
    bot_suffixes: Iterable[str] = ("[bot]", "-bot", "_bot", "dependabot", "renovate"),
) -> AuthorKind:
    """推断提交者身份。

    PR 流程要求先分辨「开发者/社区提交」还是「AI 提交」（Q1），
    这里给出基于标签 / 用户名 / 内容的启发式判断，AI 复核在提示词层再做一次。
    """
    name = (username or "").lower()
    if not name:
        return AuthorKind.UNKNOWN
    label_set = {str(l).lower() for l in labels}
    if any(k in label_set for k in ("ai-generated", "ai", "generated-by-ai", "fissue")):
        return AuthorKind.AI_AGENT
    body_lower = (body or "")[:2000].lower()
    if "generated by" in body_lower and ("ai" in body_lower or "copilot" in body_lower or "claude" in body_lower):
        return AuthorKind.AI_AGENT
    if any(s in name for s in bot_suffixes):
        return AuthorKind.BOT
    if name in {m.lower() for m in maintainers}:
        return AuthorKind.MAINTAINER
    return AuthorKind.COMMUNITY


def extract_linked_issues(*texts: str) -> list[int]:
    """从标题/正文中解析 ``#123`` / ``fixes #123`` 形式的关联 Issue 号。"""
    import re

    found: list[int] = []
    pattern = re.compile(r"(?:#|/issues/|/pull/|/pulls/|/merge_requests/)(\d{1,8})")
    for text in texts:
        if not text:
            continue
        for m in pattern.finditer(text):
            num = int(m.group(1))
            if num not in found:
                found.append(num)
    return found[:20]


def since_default(days: int | None) -> datetime | None:
    """``since_days`` → 起始时间；<=0 表示不限。"""
    if days is None or days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)
