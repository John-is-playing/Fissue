"""v5 风格 API 的共享适配器（Gitee / AtomGit）。

Gitee 开放 API 与 AtomGit OpenAPI 的路径结构一致（``/repos/:owner/:repo/...``），
差异仅在域名、鉴权头与少量字段命名，因此抽成共同基类，子类只覆写这几处。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Any

from ..errors import PlatformError
from ..logging_setup import get_logger
from ..models import Comment, FileChange, ItemType, Platform, RawItem, RepoRef
from .base import PlatformAdapter, extract_linked_issues, guess_author_kind, parse_dt

log = get_logger(__name__)


def _label_names(labels: Any) -> list[str]:
    """v5 的 labels 可能是字符串数组或对象数组。"""
    out: list[str] = []
    for l in labels or []:
        if isinstance(l, dict):
            name = l.get("name") or l.get("title") or ""
        else:
            name = str(l)
        if name:
            out.append(name)
    return out


def _login(user: Any) -> str:
    if isinstance(user, dict):
        return user.get("login") or user.get("username") or user.get("name") or ""
    return str(user or "")


class V5Adapter(PlatformAdapter):
    """Gitee / AtomGit 共用的 v5 适配器。"""

    api_version_header: str | None = None      # AtomGit 需要 X-Api-Version
    use_query_token: bool = False              # Gitee 支持 access_token 查询参数

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Fissue/0.1",
        }
        if self.api_version_header:
            headers["X-Api-Version"] = self.api_version_header
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _auth_params(self) -> dict[str, str]:
        if self.use_query_token and self.token:
            return {"access_token": self.token}
        return {}

    async def _get(self, url: str, **params: Any) -> Any:  # type: ignore[override]
        merged = {**self._auth_params(), **{k: v for k, v in params.items() if v is not None}}
        return await self._request("GET", url, params=merged)

    async def _post(self, url: str, payload: dict[str, Any]) -> Any:
        params = self._auth_params()
        body = {**payload, **params} if params else payload
        return await self._request("POST", url, json=body, params=params or None)

    async def _patch(self, url: str, payload: dict[str, Any]) -> Any:
        params = self._auth_params()
        body = {**payload, **params} if params else payload
        return await self._request("PATCH", url, json=body, params=params or None)

    # -- URL 便捷 ---------------------------------------------------------

    def _repo_api(self, repo: RepoRef) -> str:
        return f"{self.api_base}/repos/{repo.owner}/{repo.name}"

    # -- 仓库信息 ---------------------------------------------------------

    async def get_default_branch(self, repo: RepoRef) -> str:
        data = await self._get(self._repo_api(repo))
        return (data or {}).get("default_branch") or "master"

    async def whoami(self) -> str:
        data = await self._get(f"{self.api_base}/user")
        return _login(data)

    async def fetch_repo_labels(self, repo: RepoRef) -> list[str]:
        names: list[str] = []
        async for batch in self._paginate(f"{self._repo_api(repo)}/labels", style="page"):
            names.extend(_label_names(batch))
        return names

    async def ensure_label(self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI") -> None:
        existing = set(await self.fetch_repo_labels(repo))
        if label in existing:
            return
        try:
            await self._post(f"{self._repo_api(repo)}/labels", {"name": label, "color": color.replace("#", "")})
            log.info("[%s] 已创建标签 %s", self.platform.value, label)
        except PlatformError as exc:
            log.warning("[%s] 创建标签失败（忽略）：%s", self.platform.value, exc)

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

        # Issue
        if ItemType.ISSUE in wanted:
            params: dict[str, Any] = {"state": state, "sort": "updated", "direction": "desc"}
            if since is not None:
                params["since"] = since.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
            async for batch in self._paginate(f"{self._repo_api(repo)}/issues", params=params):
                for payload in batch:
                    # v5 的 issues 列表可能混入 PR（带 pull_request 字段），按类型分流
                    is_pr = bool(payload.get("pull_request"))
                    if is_pr and ItemType.PR not in wanted:
                        continue
                    item_type = ItemType.PR if is_pr else ItemType.ISSUE
                    if is_pr:
                        payload = await self._merge_pr_detail(repo, payload)
                    try:
                        yield await self._build_item(repo, payload, item_type)
                    except PlatformError as exc:
                        log.warning("[%s] 构造条目失败 #%s：%s", self.platform.value, payload.get("number"), exc)
                        continue
                    count += 1
                    if limit is not None and count >= limit:
                        return

        # Pull Request
        if ItemType.PR in wanted:
            params = {"state": state, "sort": "updated", "direction": "desc"}
            async for batch in self._paginate(f"{self._repo_api(repo)}/pulls", params=params):
                for payload in batch:
                    number = payload.get("number") or payload.get("id")
                    if number is None:
                        continue
                    try:
                        detail = await self._get(f"{self._repo_api(repo)}/pulls/{number}") or payload
                        yield await self._build_item(repo, detail, ItemType.PR)
                    except PlatformError as exc:
                        log.warning("[%s] 构造 PR 失败 #%s：%s", self.platform.value, number, exc)
                        continue
                    count += 1
                    if limit is not None and count >= limit:
                        return

    async def _merge_pr_detail(self, repo: RepoRef, payload: dict[str, Any]) -> dict[str, Any]:
        number = payload.get("number")
        if number is None:
            return payload
        try:
            detail = await self._get(f"{self._repo_api(repo)}/pulls/{number}")
            if isinstance(detail, dict):
                merged = {**payload, **detail}
                return merged
        except PlatformError as exc:
            log.debug("[%s] 拉 PR 详情失败 #%s：%s", self.platform.value, number, exc)
        return payload

    async def _build_item(self, repo: RepoRef, payload: dict[str, Any], item_type: ItemType) -> RawItem:
        number = int(payload.get("number") or payload.get("id") or 0)
        title = payload.get("title") or ""
        body = payload.get("body") or ""
        labels = _label_names(payload.get("labels"))
        author = _login(payload.get("user"))

        raw = RawItem(
            platform=self.platform,
            repo=repo.slug,
            number=number,
            item_type=item_type,
            title=title,
            body=body,
            state=payload.get("state") or "open",
            labels=labels,
            author=author,
            created_at=parse_dt(payload.get("created_at")),
            updated_at=parse_dt(payload.get("updated_at")),
            closed_at=parse_dt(payload.get("closed_at") or payload.get("finished_at")),
            is_draft=bool(payload.get("draft")),
            raw={"html_url": payload.get("html_url")},
        )

        if item_type is ItemType.PR:
            head = payload.get("head") or {}
            base = payload.get("base") or {}
            raw.base_branch = base.get("ref") if isinstance(base, dict) else None
            raw.head_branch = head.get("ref") if isinstance(head, dict) else None
            head_repo = head.get("repo") if isinstance(head, dict) else None
            if isinstance(head_repo, dict):
                raw.head_repo = head_repo.get("full_name") or head_repo.get("path_with_namespace")
            raw.merged = bool(payload.get("merged_at"))
            raw.mergeable = payload.get("mergeable")
            raw.additions = int(payload.get("additions") or 0)
            raw.deletions = int(payload.get("deletions") or 0)
            raw.changed_files = int(payload.get("changed_files") or len(payload.get("files") or []))
            # 文件清单落库：验证阶段要据此判定「是否只改了文档」
            # （无代码变更 → 明确不建议合并），而验证阶段必须离线可跑，不能在那儿现拉。
            try:
                raw.files = await self.fetch_files(repo, number)
            except Exception as exc:      # 取不到不影响主流程：验证阶段会保守放行
                log.warning("[%s] 拉取 PR #%s 文件清单失败：%s", self.platform.value, number, exc)
            raw.linked_issues = extract_linked_issues(title, body)
            # v5 提供 PR 关联 Issue 的专用端点
            try:
                linked = await self._get(f"{self._repo_api(repo)}/pulls/{number}/issues")
                if isinstance(linked, list):
                    nums = {int(x.get("number")) for x in linked if isinstance(x, dict) and x.get("number")}
                    raw.linked_issues = sorted(nums | set(raw.linked_issues))
            except PlatformError:
                pass
        else:
            raw.linked_issues = extract_linked_issues(title, body)

        raw.comments = await self._fetch_comments(repo, number, item_type=item_type, limit=100)
        raw.author_kind = guess_author_kind(
            author, body=body, labels=labels, maintainers=(repo.owner,)
        )
        return raw

    async def _fetch_comments(
        self, repo: RepoRef, number: int, *, item_type: ItemType = ItemType.ISSUE, limit: int = 100
    ) -> list[Comment]:
        # v5：Issue 与 PR 的评论都走 issues 端点
        url = f"{self._repo_api(repo)}/issues/{number}/comments"
        comments: list[Comment] = []
        try:
            data = await self._get(url, per_page=min(limit, self.per_page))
        except PlatformError as exc:
            log.debug("[%s] 拉评论失败 #%s：%s", self.platform.value, number, exc)
            return comments
        for c in data or []:
            if not isinstance(c, dict):
                continue
            author = _login(c.get("user"))
            comments.append(
                Comment(
                    author=author,
                    author_kind=guess_author_kind(author, body=c.get("body") or ""),
                    body=c.get("body") or "",
                    created_at=parse_dt(c.get("created_at")),
                )
            )
            if len(comments) >= limit:
                break
        return comments

    async def fetch_files(
        self, repo: RepoRef, number: int, *, with_patch: bool = False, max_files: int = 100
    ) -> list[FileChange]:
        files: list[FileChange] = []
        try:
            data = await self._get(f"{self._repo_api(repo)}/pulls/{number}/files")
        except PlatformError as exc:
            log.debug("[%s] 拉 PR 文件失败 #%s：%s", self.platform.value, number, exc)
            return files
        for f in data or []:
            if not isinstance(f, dict):
                continue
            files.append(
                FileChange(
                    path=f.get("filename") or f.get("new_path") or "",
                    status=f.get("status") or "modified",
                    additions=int(f.get("additions") or 0),
                    deletions=int(f.get("deletions") or 0),
                    patch=f.get("patch") if with_patch else None,
                )
            )
            if len(files) >= max_files:
                break
        return files

    async def fetch_diff(self, repo: RepoRef, number: int) -> str:
        """v5 支持在 PR URL 后加 ``.diff`` 直接取 diff 文本。"""
        url = f"{self._repo_api(repo)}/pulls/{number}.diff"
        return await self._request("GET", url, params=self._auth_params() or None, expect_json=False)

    # -- 写操作 -----------------------------------------------------------

    async def add_labels(self, repo: RepoRef, number: int, labels: list[str], item_type: ItemType = ItemType.ISSUE) -> None:
        if not labels:
            return
        # v5：Issue 与 PR 的标签都通过 issues 端点操作
        await self._post(f"{self._repo_api(repo)}/issues/{number}/labels", {"labels": labels})

    async def comment(self, repo: RepoRef, number: int, body: str, item_type: ItemType = ItemType.ISSUE) -> None:
        await self._post(f"{self._repo_api(repo)}/issues/{number}/comments", {"body": body})

    async def close_item(self, repo: RepoRef, number: int, item_type: ItemType = ItemType.ISSUE) -> None:
        await self._patch(f"{self._repo_api(repo)}/issues/{number}", {"state": "closed"})

    async def fork_repo(self, repo: RepoRef) -> RepoRef:
        data = await self._post(f"{self._repo_api(repo)}/forks", {})
        full_name = ""
        if isinstance(data, dict):
            full_name = data.get("full_name") or data.get("path_with_namespace") or ""
            if not full_name and isinstance(data.get("namespace"), dict):
                ns = data["namespace"].get("path") or data["namespace"].get("name")
                if ns and data.get("path"):
                    full_name = f"{ns}/{data['path']}"
        owner, _, name = full_name.partition("/")
        if not owner:
            # v5 的 fork 接口可能异步返回，退化为按当前用户推断
            me = await self.whoami()
            if not me:
                raise PlatformError(f"fork 失败且无法推断结果：{data}")
            return RepoRef(platform=self.platform, owner=me, name=repo.name)
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
        data = await self._post(
            f"{self._repo_api(upstream)}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        if not isinstance(data, dict):
            raise PlatformError(f"创建 PR 返回异常：{data}")
        return {
            "number": data.get("number") or data.get("id"),
            "url": data.get("html_url") or data.get("url"),
        }

    def item_url(self, repo: RepoRef, number: int, item_type: ItemType) -> str:
        seg = self.issue_segment if item_type is ItemType.ISSUE else self.pr_segment
        return f"https://{self.web_host}/{repo.owner}/{repo.name}/{seg}/{number}"

    issue_segment: str = "issues"
    pr_segment: str = "pulls"
