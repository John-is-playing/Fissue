"""适配器基类与统一接口。

所有平台适配器都实现 :class:`PlatformAdapter`，把各自平台的 API 差异
（字段名、分页方式、URL 规则、鉴权头）封装在内部，向上只暴露统一模型
（:class:`~fissue.models.RawItem`）。
"""

from __future__ import annotations

import asyncio
import random
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
    ) -> None:
        self.token = token
        self.api_base = (api_base or self.default_api_base).rstrip("/")
        self.per_page = per_page
        self.max_retries = max_retries
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = None

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

    async def ensure_label(self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI") -> None:
        """确保标签存在（不存在则创建）。"""
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
