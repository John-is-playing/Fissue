"""GitHub 适配器（REST v3）。

文档：https://docs.github.com/en/rest
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Any

from ..errors import PlatformError, RateLimitError
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

    async def _create_label(
        self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI"
    ) -> bool:
        """创建单个标签，返回「该标签现在确实存在」。"""
        try:
            await self._request(
                "POST",
                f"{self.api_base}/repos/{repo.owner}/{repo.name}/labels",
                json={"name": label, "color": color, "description": description},
            )
            log.info("[github] 已创建标签 %s", label)
            return True
        except RateLimitError:
            raise
        except PlatformError as exc:
            if self._is_already_exists(exc):
                log.info("[github] 标签 %s 已存在", label)
                return True
            log.warning("[github] 创建标签失败（不计入缓存，稍后会重试）：%s", exc)
            return False

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
                except RateLimitError:
                    # 限流绝不能吞：吞掉就等价于「这条条目不存在」，而游标照推，
                    # 于是它再也不会被增量抓取碰到（除非 --full）。
                    raise
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

        comments, comments_note = await self._fetch_comments(repo, number, limit=100)

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
        if comments_note:
            raw.mark_degraded(comments_note)

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
        # 变更文件清单：验证阶段要据此判定「是否只改了文档」（无代码变更 → 明确
        # 不建议合并）。必须在**抓取阶段**取，验证阶段只读库里的结果，否则会在
        # 无网络的环境（单测）里发请求而卡住。
        try:
            raw.files = await self.fetch_files(repo, number)
        except RateLimitError:
            raise
        except Exception as exc:      # 取不到不影响主流程：验证阶段会保守放行
            log.warning("拉取 PR #%s 文件清单失败（记为数据残缺）：%s", number, exc)
            raw.mark_degraded(f"files_unavailable#{number}")
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
            # 限流或登录失效时 GitHub 可能回 200 + 一个 HTML 页；不校验的话
            # 它会被当成 diff 一路带到 `git apply` 才炸。
            expect_text_prefix=("diff --git",),
        )

    async def _fetch_comments(
        self, repo: RepoRef, number: int, *, limit: int = 100
    ) -> tuple[list[Comment], str]:
        """拉评论，返回 ``(评论, 残缺原因)``。

        原因非空表示这条数据的评论没拉全，调用方**必须**据此标记条目残缺。

        **限流一律向上抛**（不被 ``except PlatformError`` 吞掉）：吞掉的话条目会
        以「零评论」入库，评测基于残缺数据下结论、结论又被回写到平台，而增量
        抓取永远不会再碰这条——错误就此固化。

        分页：此前只取一页（``per_page`` 上限 50），``limit=100`` 永远达不到，
        高讨论量条目最关键的讨论被静默截断。
        """
        url = f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues/{number}/comments"
        comments: list[Comment] = []
        truncated = False
        try:
            max_pages = max(1, (limit + self.per_page - 1) // self.per_page)
            async for batch in self._paginate(url, max_pages=max_pages):
                for c in batch:
                    if not isinstance(c, dict):
                        continue
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
                        truncated = True
                        break
                if truncated:
                    break
        except RateLimitError:
            raise
        except PlatformError as exc:
            log.warning("[github] 拉评论失败 #%s（本条记为数据残缺）：%s", number, exc)
            return comments, f"comments_unavailable#{number}"
        return comments, (f"comments_may_be_truncated#{number}" if truncated else "")

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
        async def _probe() -> Any:
            """已存在一条逐字相同的评论 → 说明这次写其实已经生效了。"""
            data = await self._get(
                f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
                per_page=100,
            )
            for c in data or []:
                if isinstance(c, dict) and (c.get("body") or "") == body:
                    return c
            return None

        await self._request(
            "POST",
            f"{self.api_base}/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            json={"body": body},
            probe=_probe,
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

    async def find_open_pr(self, upstream: RepoRef, *, head: str) -> dict[str, Any] | None:
        """按 head 查已有 PR（含已关闭）。

        两个用途：创建 PR 前先去重；**已有 PR 时不再推送分支**（见
        ``PRCreator.submit``）。
        """
        head_ref = head if ":" in head else f"{upstream.owner}:{head}"
        data = await self._get(
            f"{self.api_base}/repos/{upstream.owner}/{upstream.name}/pulls",
            head=head_ref,
            state="all",
            per_page=20,
        )
        for pr in data or []:
            if isinstance(pr, dict):
                return {"number": pr.get("number"), "url": pr.get("html_url")}
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
        async def _probe() -> Any:
            """同一个 head 上已经有 PR → 返回它，绝不重复创建。"""
            return await self.find_open_pr(upstream, head=head)

        data = await self._request(
            "POST",
            f"{self.api_base}/repos/{upstream.owner}/{upstream.name}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
            probe=_probe,
        )
        return {"number": (data or {}).get("number"), "url": (data or {}).get("html_url")}

    def item_url(self, repo: RepoRef, number: int, item_type: ItemType) -> str:
        seg = "issues" if item_type is ItemType.ISSUE else "pull"
        return f"https://github.com/{repo.owner}/{repo.name}/{seg}/{number}"
