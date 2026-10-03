"""GitLab 适配器（REST v4，支持自建实例）。

文档：https://docs.gitlab.com/ee/api/
要点：项目用 URL 编码的 ``owner/name`` 作为 project id；
Issue 与 MR 走不同端点；评论叫 notes；合并请求叫 merge request。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..errors import PlatformError
from ..logging_setup import get_logger
from ..models import AuthorKind, Comment, FileChange, ItemType, Platform, RawItem, RepoRef
from .base import PlatformAdapter, extract_linked_issues, guess_author_kind, parse_dt

log = get_logger(__name__)

DEFAULT_WEB_HOST = "gitlab.com"


class GitLabAdapter(PlatformAdapter):
    platform = Platform.GITLAB
    default_api_base = "https://gitlab.com/api/v4"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # 自建实例：从 api_base 反推 web 主机（去掉 /api/v4 后缀）
        api = self.api_base
        host = api
        for suffix in ("/api/v4", "/api"):
            if host.endswith(suffix):
                host = host[: -len(suffix)]
                break
        self.web_host = host.replace("https://", "").replace("http://", "").rstrip("/") or DEFAULT_WEB_HOST
        self.scheme = "http://" if api.startswith("http://") else "https://"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "Fissue/0.1"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        return headers

    @staticmethod
    def _pid(repo: RepoRef) -> str:
        """项目 ID：URL 编码的 owner/name。"""
        return quote(f"{repo.owner}/{repo.name}", safe="")

    def _project(self, repo: RepoRef) -> str:
        return f"{self.api_base}/projects/{self._pid(repo)}"

    # -- 仓库信息 ---------------------------------------------------------

    async def get_default_branch(self, repo: RepoRef) -> str:
        data = await self._get(self._project(repo))
        return (data or {}).get("default_branch") or "main"

    async def whoami(self) -> str:
        data = await self._get(f"{self.api_base}/user")
        return (data or {}).get("username", "")

    async def fetch_repo_labels(self, repo: RepoRef) -> list[str]:
        names: list[str] = []
        async for batch in self._paginate(f"{self._project(repo)}/labels"):
            names.extend(str(x.get("name", "")) for x in batch if x.get("name"))
        return names

    async def ensure_label(self, repo: RepoRef, label: str, *, color: str = "#0e8a16", description: str = "Fissue AI") -> None:
        existing = set(await self.fetch_repo_labels(repo))
        if label in existing:
            return
        try:
            await self._request(
                "POST",
                f"{self._project(repo)}/labels",
                json={"name": label, "color": color if color.startswith("#") else f"#{color}"},
            )
            log.info("[gitlab] 已创建标签 %s", label)
        except PlatformError as exc:
            log.warning("[gitlab] 创建标签失败（忽略）：%s", exc)

    # -- 抓取 -------------------------------------------------------------

    async def fetch_items(
        self,
        repo: RepoRef,
        *,
        item_types: Iterable[ItemType] = (ItemType.ISSUE, ItemType.PR),
        since: datetime | None = None,
        limit: int | None = None,
        state: str = "all",
    ) -> AsyncIterator[RawItem]:
        wanted = set(item_types)
        count = 0
        state_param = {"all": "all", "open": "opened", "closed": "closed"}.get(state, state)

        if ItemType.ISSUE in wanted:
            params: dict[str, Any] = {
                "state": state_param,
                "order_by": "updated_at",
                "sort": "desc",
                "scope": "all",
            }
            if since is not None:
                params["updated_after"] = since.astimezone().isoformat()
            async for batch in self._paginate(f"{self._project(repo)}/issues", params=params):
                for payload in batch:
                    try:
                        yield await self._build_item(repo, payload, ItemType.ISSUE)
                    except PlatformError as exc:
                        log.warning("[gitlab] 构造 Issue 失败 #%s：%s", payload.get("iid"), exc)
                        continue
                    count += 1
                    if limit is not None and count >= limit:
                        return

        if ItemType.PR in wanted:
            params = {
                "state": state_param,
                "order_by": "updated_at",
                "sort": "desc",
                "scope": "all",
            }
            if since is not None:
                params["updated_after"] = since.astimezone().isoformat()
            async for batch in self._paginate(f"{self._project(repo)}/merge_requests", params=params):
                for payload in batch:
                    try:
                        yield await self._build_item(repo, payload, ItemType.PR)
                    except PlatformError as exc:
                        log.warning("[gitlab] 构造 MR 失败 #%s：%s", payload.get("iid"), exc)
                        continue
                    count += 1
                    if limit is not None and count >= limit:
                        return

    async def _build_item(self, repo: RepoRef, payload: dict[str, Any], item_type: ItemType) -> RawItem:
        number = int(payload.get("iid") or payload.get("id") or 0)
        title = payload.get("title") or ""
        body = payload.get("description") or ""
        labels = [str(l) for l in (payload.get("labels") or [])]
        author = (payload.get("author") or {}).get("username", "")

        raw = RawItem(
            platform=self.platform,
            repo=repo.slug,
            number=number,
            item_type=item_type,
            title=title,
            body=body,
            state=_norm_state(payload.get("state")),
            labels=labels,
            author=author,
            created_at=parse_dt(payload.get("created_at")),
            updated_at=parse_dt(payload.get("updated_at")),
            closed_at=parse_dt(payload.get("closed_at") or payload.get("merged_at")),
            is_draft=bool(payload.get("draft") or payload.get("work_in_progress")),
            raw={"html_url": payload.get("web_url")},
        )

        if item_type is ItemType.PR:
            raw.base_branch = payload.get("target_branch")
            raw.head_branch = payload.get("source_branch")
            source_project_id = payload.get("source_project_id")
            if source_project_id:
                try:
                    sp = await self._get(f"{self.api_base}/projects/{source_project_id}")
                    if isinstance(sp, dict):
                        raw.head_repo = sp.get("path_with_namespace")
                except PlatformError:
                    pass
            raw.merged = bool(payload.get("merged_at")) or payload.get("state") == "merged"
            raw.mergeable = payload.get("merge_status") in ("can_be_merged", "mergeable")
            # 变更统计：changes 端点一次性给出
            try:
                changes = await self._get(f"{self._project(repo)}/merge_requests/{number}/changes")
                if isinstance(changes, dict):
                    raw.additions = int(changes.get("additions") or 0)
                    raw.deletions = int(changes.get("deletions") or 0)
                    raw.changed_files = len(changes.get("changes") or [])
            except PlatformError:
                pass
            closable = payload.get("closes_issue_iid")
            raw.linked_issues = extract_linked_issues(title, body)
            if closable:
                try:
                    nums = {int(closable)}
                    raw.linked_issues = sorted(nums | set(raw.linked_issues))
                except (TypeError, ValueError):
                    pass
        else:
            raw.linked_issues = extract_linked_issues(title, body)

        raw.comments = await self._fetch_notes(repo, number, item_type=item_type)
        raw.author_kind = guess_author_kind(
            author, body=body, labels=labels, maintainers=(repo.owner,)
        )
        return raw

    async def _fetch_notes(
        self, repo: RepoRef, number: int, *, item_type: ItemType = ItemType.ISSUE, limit: int = 100
    ) -> list[Comment]:
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        comments: list[Comment] = []
        try:
            data = await self._get(
                f"{self._project(repo)}/{segment}/{number}/notes",
                sort="asc",
                per_page=min(limit, self.per_page),
            )
        except PlatformError as exc:
            log.debug("[gitlab] 拉评论失败 #%s：%s", number, exc)
            return comments
        for n in data or []:
            if not isinstance(n, dict) or n.get("system"):
                continue  # system 是系统事件（如"关闭了 issue"），跳过
            author = (n.get("author") or {}).get("username", "")
            comments.append(
                Comment(
                    author=author,
                    author_kind=guess_author_kind(author, body=n.get("body") or ""),
                    body=n.get("body") or "",
                    created_at=parse_dt(n.get("created_at")),
                )
            )
            if len(comments) >= limit:
                break
        return comments

    async def fetch_files(
        self, repo: RepoRef, number: int, *, with_patch: bool = False, max_files: int = 100
    ) -> list[FileChange]:
        try:
            data = await self._get(f"{self._project(repo)}/merge_requests/{number}/changes")
        except PlatformError as exc:
            log.debug("[gitlab] 拉 MR 变更失败 #%s：%s", number, exc)
            return []
        files: list[FileChange] = []
        for c in (data or {}).get("changes", [])[:max_files]:
            if not isinstance(c, dict):
                continue
            files.append(
                FileChange(
                    path=c.get("new_path") or c.get("old_path") or "",
                    status="removed" if c.get("deleted_file") else ("added" if c.get("new_file") else "modified"),
                    patch=c.get("diff") if with_patch else None,
                )
            )
        return files

    async def fetch_diff(self, repo: RepoRef, number: int) -> str:
        """拼接 MR 各文件的 diff 文本。"""
        try:
            data = await self._get(f"{self._project(repo)}/merge_requests/{number}/changes")
        except PlatformError as exc:
            raise PlatformError(f"拉取 GitLab MR diff 失败 #{number}：{exc}") from exc
        parts: list[str] = []
        for c in (data or {}).get("changes", []):
            old = c.get("old_path") or ""
            new = c.get("new_path") or ""
            parts.append(f"--- a/{old}\n+++ b/{new}\n{c.get('diff', '')}")
        return "\n".join(parts)

    # -- 写操作 -----------------------------------------------------------

    async def add_labels(self, repo: RepoRef, number: int, labels: list[str], item_type: ItemType = ItemType.ISSUE) -> None:
        if not labels:
            return
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        await self._request(
            "PUT",
            f"{self._project(repo)}/{segment}/{number}",
            json={"add_labels": ",".join(labels)},
        )

    async def comment(self, repo: RepoRef, number: int, body: str, item_type: ItemType = ItemType.ISSUE) -> None:
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        await self._request(
            "POST",
            f"{self._project(repo)}/{segment}/{number}/notes",
            json={"body": body},
        )

    async def close_item(self, repo: RepoRef, number: int, item_type: ItemType = ItemType.ISSUE) -> None:
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        event = "close" if item_type is ItemType.ISSUE else "close"
        await self._request(
            "PUT",
            f"{self._project(repo)}/{segment}/{number}",
            json={"state_event": event},
        )

    async def fork_repo(self, repo: RepoRef) -> RepoRef:
        data = await self._request("POST", f"{self._project(repo)}/fork", json={})
        path = (data or {}).get("path_with_namespace") or ""
        owner, _, name = path.partition("/")
        if not owner:
            me = await self.whoami()
            if not me:
                raise PlatformError(f"GitLab fork 返回异常：{data}")
            return RepoRef(platform=self.platform, owner=repo.owner, name=repo.name) if not me else RepoRef(
                platform=self.platform, owner=me, name=repo.name
            )
        return RepoRef(platform=self.platform, owner=owner, name=name or repo.name)

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
        """创建 MR。

        ``head`` 支持两种形式：``branch``（同仓库）或 ``namespace/repo:branch``（fork 来的）。
        后者会在 fork 项目上创建 MR 并指向上游。
        """
        source_repo = upstream
        source_branch = head
        if ":" in head:
            path, _, source_branch = head.partition(":")
            owner, _, name = path.partition("/")
            if owner and name:
                source_repo = RepoRef(platform=self.platform, owner=owner, name=name)

        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": base,
            "title": ("Draft: " + title) if draft else title,
            "description": body,
            "remove_source_branch": False,
        }
        if source_repo.slug != upstream.slug:
            target = await self._get(self._project(upstream))
            if isinstance(target, dict) and target.get("id"):
                payload["target_project_id"] = target["id"]

        data = await self._request(
            "POST", f"{self._project(source_repo)}/merge_requests", json=payload
        )
        if not isinstance(data, dict):
            raise PlatformError(f"创建 MR 返回异常：{data}")
        return {"number": data.get("iid") or data.get("id"), "url": data.get("web_url")}

    def item_url(self, repo: RepoRef, number: int, item_type: ItemType) -> str:
        seg = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        return f"{self.scheme}{self.web_host}/{repo.owner}/{repo.name}/-/{seg}/{number}"

    def clone_url(self, repo: RepoRef, *, use_token: bool = False) -> str:
        if use_token and self.token:
            return f"{self.scheme}oauth2:{self.token}@{self.web_host}/{repo.owner}/{repo.name}.git"
        return f"{self.scheme}{self.web_host}/{repo.owner}/{repo.name}.git"


def _norm_state(state: Any) -> str:
    """GitLab 的 opened → 统一为 open；merged 保留。"""
    s = str(state or "open").lower()
    return {"opened": "open"}.get(s, s)
