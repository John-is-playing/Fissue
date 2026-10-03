"""GitHub 适配器（REST v3）。

文档：https://docs.github.com/en/rest
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Any

from ..errors import PlatformError
from ..logging_setup import get_logger
from ..models import AuthorKind, Comment, FileChange, ItemType, Platform, RawItem, RepoRef
from .base import (
    PlatformAdapter,
    extract_linked_issues,
    guess_author_kind,
    parse_dt,
)

log = get_logger(__name__)

API_VERSION = "2022-11-28"


class GitHubAdapter(PlatformAdapter):
    platform = Platform.GITHUB
    default_api_base = "https://api.github.com"
    web_host = "github.com"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "Fissue/0.1 (+https://github.com/)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # -- 仓库信息 ---------------------------------------------------------

    async def get_default_branch(self, repo: RepoRef) -> str:
        data = await self._get(f"{self.api_base}/repos/{repo.owner}/{repo.name}")
        return (data or {}).get("default_branch") or "main"

    async def whoami(self) -> str:
        data = await self._get(f"{self.api_base}/user")
        return (data or {}).get("login", "")

    async def fetch_repo_labels(self, repo: RepoRef) -> list[str]:
        names: list[str] = []
        async for batch in self._paginate(f"{self.api_base}/repos/{repo.owner}/{repo.name}/labels"):
            names.extend(str(x.get("name", "")) for x in batch if x.get("name"))
        return names

    async def ensure_label(self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI") -> None:
        existing = set(await self.fetch_repo_labels(repo))
        if label in existing:
            return
        try:
            await self._request(
                "POST",
                f"{self.api_base}/repos/{repo.owner}/{repo.name}/labels",
                json={"name": label, "color": color, "description": description},
            )
            log.info("[github] 已创建标签 %s", label)
        except PlatformError as exc:
            log.warning("[github] 创建标签失败（忽略）：%s", exc)

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
        # /issues 会同时返回 issue 与 PR（PR 带 pull_request 字段），一次拉全量最省配额
        url = f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues"
        params: dict[str, Any] = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
        }
        if since is not None:
            params["since"] = since.astimezone(tz=None).isoformat()

        count = 0
        async for batch in self._paginate(url, params=params):
            for payload in batch:
                is_pr = "pull_request" in payload
                item_type = ItemType.PR if is_pr else ItemType.ISSUE
                if item_type not in wanted:
                    continue
                try:
                    item = await self._build_item(repo, payload, item_type)
                except PlatformError as exc:
                    log.warning("[github] 构造条目失败 #%s：%s", payload.get("number"), exc)
                    continue
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return

    async def _build_item(self, repo: RepoRef, payload: dict[str, Any], item_type: ItemType) -> RawItem:
        number = int(payload["number"])
        title = payload.get("title") or ""
        body = payload.get("body") or ""
        labels = [l.get("name", "") for l in payload.get("labels", []) if isinstance(l, dict)]
        author = (payload.get("user") or {}).get("login", "")

        comments = await self._fetch_comments(repo, number, limit=100)

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
            comments=comments,
            created_at=parse_dt(payload.get("created_at")),
            updated_at=parse_dt(payload.get("updated_at")),
            closed_at=parse_dt(payload.get("closed_at")),
            is_draft=bool(payload.get("draft")),
            raw={"html_url": payload.get("html_url")},
        )

        if item_type is ItemType.PR:
            await self._enrich_pr(repo, number, raw)
        else:
            raw.linked_issues = extract_linked_issues(title, body)

        raw.author_kind = guess_author_kind(
            author,
            body=body,
            labels=labels,
            maintainers=(repo.owner,),
        )
        return raw

    async def _enrich_pr(self, repo: RepoRef, number: int, raw: RawItem) -> None:
        """补 PR 专有字段：分支、可合并性、变更统计、diff 摘要。"""
        pr = await self._get(f"{self.api_base}/repos/{repo.owner}/{repo.name}/pulls/{number}")
        if not pr:
            return
        raw.base_branch = (pr.get("base") or {}).get("ref")
        raw.head_branch = (pr.get("head") or {}).get("ref")
        head_repo_info = pr.get("head") or {}
        raw.head_repo = (head_repo_info.get("repo") or {}).get("full_name") if head_repo_info.get("repo") else None
        raw.merged = bool(pr.get("merged_at"))
        raw.mergeable = pr.get("mergeable")
        raw.is_draft = bool(pr.get("draft"))
        raw.additions = int(pr.get("additions") or 0)
        raw.deletions = int(pr.get("deletions") or 0)
        raw.changed_files = int(pr.get("changed_files") or 0)
        # 关联 Issue：优先用 API 的 timeline 提取（此处用正文正则兜底，省一次请求）
        raw.linked_issues = extract_linked_issues(pr.get("title") or "", pr.get("body") or "")

    async def fetch_files(self, repo: RepoRef, number: int, *, with_patch: bool = False, max_files: int = 100) -> list[FileChange]:
        """PR 变更文件列表；``with_patch`` 为真时带 patch 内容（评代码质量用）。"""
        files: list[FileChange] = []
        async for batch in self._paginate(
            f"{self.api_base}/repos/{repo.owner}/{repo.name}/pulls/{number}/files",
            max_pages=10,
        ):
            for f in batch:
                files.append(
                    FileChange(
                        path=f.get("filename", ""),
                        status=f.get("status", "modified"),
                        additions=int(f.get("additions") or 0),
                        deletions=int(f.get("deletions") or 0),
                        patch=f.get("patch") if with_patch else None,
                    )
                )
                if len(files) >= max_files:
                    return files
        return files

    async def fetch_diff(self, repo: RepoRef, number: int) -> str:
        headers = {**self._headers(), "Accept": "application/vnd.github.v3.diff"}
        return await self._request(
            "GET",
            f"{self.api_base}/repos/{repo.owner}/{repo.name}/pulls/{number}",
            headers=headers,
            expect_json=False,
        )

    async def _fetch_comments(self, repo: RepoRef, number: int, *, limit: int = 100) -> list[Comment]:
        comments: list[Comment] = []
        try:
            data = await self._get(
                f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
                per_page=min(limit, self.per_page),
            )
        except PlatformError as exc:
            log.debug("[github] 拉评论失败 #%s：%s", number, exc)
            return comments
        for c in data or []:
            author = (c.get("user") or {}).get("login", "")
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

    # -- 写操作 -----------------------------------------------------------

    async def add_labels(self, repo: RepoRef, number: int, labels: list[str], item_type: ItemType = ItemType.ISSUE) -> None:
        if not labels:
            return
        await self._request(
            "POST",
            f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues/{number}/labels",
            json={"labels": labels},
        )

    async def comment(self, repo: RepoRef, number: int, body: str, item_type: ItemType = ItemType.ISSUE) -> None:
        await self._request(
            "POST",
            f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            json={"body": body},
        )

    async def close_item(self, repo: RepoRef, number: int, item_type: ItemType = ItemType.ISSUE) -> None:
        await self._request(
            "PATCH",
            f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues/{number}",
            json={"state": "closed"},
        )

    async def fork_repo(self, repo: RepoRef) -> RepoRef:
        data = await self._request(
            "POST", f"{self.api_base}/repos/{repo.owner}/{repo.name}/forks", json={}
        )
        full_name = (data or {}).get("full_name") or ""
        owner, _, name = full_name.partition("/")
        if not owner:
            raise PlatformError(f"fork 返回异常：{data}")
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
        data = await self._request(
            "POST",
            f"{self.api_base}/repos/{upstream.owner}/{upstream.name}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        return {"number": (data or {}).get("number"), "url": (data or {}).get("html_url")}

    def item_url(self, repo: RepoRef, number: int, item_type: ItemType) -> str:
        seg = "issues" if item_type is ItemType.ISSUE else "pull"
        return f"https://github.com/{repo.owner}/{repo.name}/{seg}/{number}"
