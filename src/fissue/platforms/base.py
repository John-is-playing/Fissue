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
from typing import Any

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
    ) -> None:
        self.token = token
        self.api_base = (api_base or self.default_api_base).rstrip("/")
        self.per_page = per_page
        self.max_retries = max_retries
        self.timeout = timeout or DEFAULT_TIMEOUT
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
            for lbl in wanted:
                if lbl in current:
                    continue
                await self._create_label(repo, lbl, color=color, description=description)
                current.add(lbl)
                created = True
            if created:
                # 回填并续期：刚 ensure 过的那批标签在 TTL 内无须再查。
                self._store_label_entry(key, current)

    async def _create_label(
        self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI"
    ) -> None:
        """创建单个标签（子类实现）。基类无标签能力时为空操作。"""
        return None

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

    def sanitize(self, text: str) -> str:
        """把日志/报告里可能出现的令牌抹掉。"""
        if self.token and self.token in text:
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
    ) -> Any:
        """带重试与限流处理的请求。"""
        last_exc: Exception | None = None
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
                        f"{self.platform.display} 连接失败（{method} {url}）：{exc}\n{ssl_help_text()}"
                    ) from exc
                log.warning("[%s] 请求异常（第 %d 次）：%s %s -> %s", self.platform.value, attempt, method, url, exc)
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code == 429 or (
                resp.status_code == 403 and "rate limit" in resp.text.lower()
            ):
                retry_after = _parse_retry_after(resp)
                if attempt >= self.max_retries:
                    raise RateLimitError(f"{self.platform.display} 触发限流：{url}", retry_after)
                log.warning("[%s] 限流，%.1fs 后重试", self.platform.value, retry_after)
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code in (500, 502, 503, 504):
                last_exc = PlatformError(f"{self.platform.display} 服务端错误 {resp.status_code}")
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code >= 400:
                raise PlatformError(
                    f"{self.platform.display} API 错误 {resp.status_code}：{method} {url} -> {resp.text[:500]}"
                )

            if not expect_json:
                return resp.text
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise PlatformError(f"{self.platform.display} 返回非 JSON：{resp.text[:300]}") from exc

        raise PlatformError(f"{self.platform.display} 请求最终失败：{method} {url}（{last_exc}）")

    async def _get(self, url: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", url, params=clean)

    async def _paginate(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        style: str = "page",
        max_pages: int = 200,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """通用分页。

        :param style: ``page``（page/per_page，GitHub/Gitee/AtomGit）、
            ``offset``（GitLab 的 page/per_page 亦可用）、
            ``link``（跟随 Link 头，GitHub 风格）。
        """
        base_params = dict(params or {})
        page = 1
        while page <= max_pages:
            if style == "page":
                payload = await self._get(url, **{**base_params, "page": page, "per_page": self.per_page})
            elif style == "offset":
                payload = await self._get(
                    url, **{**base_params, "page": page, "per_page": self.per_page}
                )
            else:  # link
                payload = await self._get(url, **{**base_params, "per_page": self.per_page})
            if payload is None:
                return
            batch = payload if isinstance(payload, list) else payload.get("items", [])
            if not isinstance(batch, list) or not batch:
                return
            yield batch
            if len(batch) < self.per_page:
                return
            page += 1

    @staticmethod
    def _backoff(attempt: int) -> float:
        """指数退避 + 抖动。"""
        return min(2 ** (attempt - 1) * 0.5, 16.0) + random.uniform(0, 0.4)


# ---------------------------------------------------------------------------
# 共享工具
# ---------------------------------------------------------------------------


def _parse_retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset")
    if raw:
        try:
            value = float(raw)
            # GitHub 的 reset 是 unix 时间戳
            if value > 1_000_000_000:
                return max(1.0, value - datetime.now(timezone.utc).timestamp())
            return max(1.0, min(value, 60.0))
        except ValueError:
            pass
    return 5.0


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
