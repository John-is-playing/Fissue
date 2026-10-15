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

from ..errors import PlatformError, RateLimitError
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
            # 用 Authorization 而不是 PRIVATE-TOKEN：httpx 在跨域重定向时**只**会
            # 剥掉 Authorization，自定义头会原样转发给跳转目标——自建实例挂在
            # 反代/SSO 后面返回一个指向他域的 3xx，令牌就明文发出去了。
            # GitLab 官方文档对个人/项目/群组访问令牌同时支持这两种传法，
            # 换成 Authorization 即免费拿到这层保护（其余三家适配器本来就这样）。
            headers["Authorization"] = f"Bearer {self.token}"
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

    async def _create_label(
        self, repo: RepoRef, label: str, *, color: str = "#0e8a16", description: str = "Fissue AI"
    ) -> bool:
        """创建单个标签，返回「该标签现在确实存在」。"""
        try:
            await self._request(
                "POST",
                f"{self._project(repo)}/labels",
                json={"name": label, "color": color if color.startswith("#") else f"#{color}"},
            )
            log.info("[gitlab] 已创建标签 %s", label)
            return True
        except RateLimitError:
            raise
        except PlatformError as exc:
            if self._is_already_exists(exc):
                log.info("[gitlab] 标签 %s 已存在", label)
                return True
            log.warning("[gitlab] 创建标签失败（不计入缓存，稍后会重试）：%s", exc)
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
        self.skipped_build_failures = 0     # 本轮重新计数（调用方读它判「数据不完整」）
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
                    except RateLimitError:
                        raise
                    except Exception as exc:
                        # 单条数据本身的问题不该拖垮整批（原先只接 PlatformError）。
                        self._note_build_failure("Issue", payload.get("iid"), exc)
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
                    except RateLimitError:
                        raise
                    except Exception as exc:
                        # 同 Issue 侧：单条数据的问题不该拖垮整批。
                        self._note_build_failure("MR", payload.get("iid"), exc)
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
                except RateLimitError:
                    raise
                except PlatformError as exc:
                    log.warning("[gitlab] 拉源项目信息失败 %s（记为数据残缺）：%s", source_project_id, exc)
                    raw.mark_degraded(f"head_repo_unavailable#{number}")
            raw.merged = bool(payload.get("merged_at")) or payload.get("state") == "merged"
            raw.mergeable = payload.get("merge_status") in ("can_be_merged", "mergeable")
            # 变更统计：changes 端点一次性给出。顺带把文件清单落库——
            # 验证阶段要据此判定「是否只改了文档」（无代码变更 → 明确不建议合并），
            # 而验证阶段必须离线可跑，不能在那儿现拉。
            try:
                changes = await self._get(f"{self._project(repo)}/merge_requests/{number}/changes")
                if isinstance(changes, dict):
                    entries = [c for c in (changes.get("changes") or []) if isinstance(c, dict)]
                    # GitLab 的 /changes **不返回** additions/deletions 合计（只有
                    # changes_count 这个字符串，`additions`/`deletions`/`diff_stats`
                    # 一律为 None），所以这两个数只能从 diff 文本里数。
                    # 实测：不数的话它们恒为 0，而 render_item 会把 MR 渲染成
                    # 「+0/-0，10 个文件」——评测据此判 pr_quality 就被喂了错信息。
                    # `+++`/`---` 是文件头不是内容行，要排除——但**必须带上那个空格**：
                    # 只判 `startswith("+++")` 会把内容行 `++i`、`--count` 一起误剔除，
                    # 让 additions/deletions 系统性低估，而它们正是 pr_quality 的输入。
                    # （真实文件头永远是 `+++ <路径>` / `--- <路径>`，哪怕是 /dev/null。）
                    # `too_large` 的 diff 平台不给文本，那种情况下这两个数是**下限**。
                    plus = minus = 0
                    for entry in entries:
                        for line in str(entry.get("diff") or "").splitlines():
                            if line.startswith("+") and not line.startswith("+++ "):
                                plus += 1
                            elif line.startswith("-") and not line.startswith("--- "):
                                minus += 1
                    raw.additions = int(changes.get("additions") or plus)
                    raw.deletions = int(changes.get("deletions") or minus)
                    raw.changed_files = len(entries)
                    raw.files = [
                        FileChange(
                            path=str(c.get("new_path") or c.get("old_path") or ""),
                            # 与 fetch_files 保持同一套状态口径
                            status=(
                                "removed" if c.get("deleted_file")
                                else "added" if c.get("new_file")
                                else "modified"
                            ),
                        )
                        for c in (changes.get("changes") or [])
                        if isinstance(c, dict) and (c.get("new_path") or c.get("old_path"))
                    ]
            except RateLimitError:
                raise
            except PlatformError as exc:
                log.warning("[gitlab] 拉 MR 变更失败 #%s（记为数据残缺）：%s", number, exc)
                raw.mark_degraded(f"files_unavailable#{number}")
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

        raw.comments, comments_note = await self._fetch_notes(repo, number, item_type=item_type)
        if comments_note:
            raw.mark_degraded(comments_note)
        raw.author_kind = guess_author_kind(
            author, body=body, labels=labels, maintainers=(repo.owner,)
        )
        return raw

    async def _fetch_notes(
        self, repo: RepoRef, number: int | str, *, item_type: ItemType = ItemType.ISSUE, limit: int = 100
    ) -> tuple[list[Comment], str]:
        """拉 MR/Issue 评论，返回 ``(评论, 残缺原因)``；原因非空表示数据不完整。

        限流向上抛（理由见 ``GithubAdapter._fetch_comments``）；并补上分页——
        此前只取一页，``limit=100`` 形同虚设。
        """
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        comments: list[Comment] = []
        truncated = False
        try:
            max_pages = max(1, (limit + self.per_page - 1) // self.per_page)
            async for batch in self._paginate(
                f"{self._project(repo)}/{segment}/{number}/notes",
                params={"sort": "asc"},
                max_pages=max_pages,
            ):
                for n in batch:
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
                        truncated = True
                        break
                if truncated:
                    break
        except RateLimitError:
            raise
        except PlatformError as exc:
            log.warning("[gitlab] 拉评论失败 #%s（本条记为数据残缺）：%s", number, exc)
            return comments, f"comments_unavailable#{number}"
        return comments, (f"comments_may_be_truncated#{number}" if truncated else "")

    async def fetch_files(
        self, repo: RepoRef, number: int | str, *, with_patch: bool = False, max_files: int = 100
    ) -> list[FileChange]:
        try:
            data = await self._get(f"{self._project(repo)}/merge_requests/{number}/changes")
        except RateLimitError:
            raise
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

    async def fetch_diff(self, repo: RepoRef, number: int | str) -> str:
        """拼接 MR 各文件的 diff 文本。"""
        try:
            data = await self._get(f"{self._project(repo)}/merge_requests/{number}/changes")
        except RateLimitError:
            raise
        except PlatformError as exc:
            raise PlatformError(f"拉取 GitLab MR diff 失败 #{number}：{exc}") from exc
        parts: list[str] = []
        for c in (data or {}).get("changes", []):
            old = c.get("old_path") or ""
            new = c.get("new_path") or ""
            parts.append(f"--- a/{old}\n+++ b/{new}\n{c.get('diff', '')}")
        return "\n".join(parts)

    # -- 写操作 -----------------------------------------------------------

    async def add_labels(self, repo: RepoRef, number: int | str, labels: list[str], item_type: ItemType = ItemType.ISSUE) -> None:
        if not labels:
            return
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        await self._request(
            "PUT",
            f"{self._project(repo)}/{segment}/{number}",
            json={"add_labels": ",".join(labels)},
        )

    async def comment(self, repo: RepoRef, number: int | str, body: str, item_type: ItemType = ItemType.ISSUE) -> None:
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        notes_url = f"{self._project(repo)}/{segment}/{number}/notes"

        async def _probe() -> Any:
            """已存在逐字相同的评论 → 说明这次写其实已经生效了。"""
            data = await self._get(notes_url, sort="desc", per_page=100)
            for n in data or []:
                if isinstance(n, dict) and (n.get("body") or "") == body:
                    return n
            return None

        await self._request("POST", notes_url, json={"body": body}, probe=_probe)

    async def close_item(self, repo: RepoRef, number: int | str, item_type: ItemType = ItemType.ISSUE) -> None:
        segment = "issues" if item_type is ItemType.ISSUE else "merge_requests"
        event = "close" if item_type is ItemType.ISSUE else "close"
        await self._request(
            "PUT",
            f"{self._project(repo)}/{segment}/{number}",
            json={"state_event": event},
        )

    async def fork_repo(self, repo: RepoRef) -> RepoRef:
        # 项目**已经在自己名下**时不要再 fork：GitLab 会返回 409
        # 「Project namespace name已经被使用」（实测：把自己的 Private 仓库当靶子，
        # 模型两轮就把 bug 修好了，却死在 fork 这一步）。
        # 注意这与 GitHub 的行为不同——GitHub 对自己的仓库 fork 会**返回它自己**，
        # 所以那条路径一直没暴露这个问题。返回仓库自身即可：`create_pr` 支持
        # 「同仓库 + 分支」的 head 形式。
        me = await self.whoami()
        if me and me.lower() == repo.owner.lower():
            return RepoRef(platform=self.platform, owner=repo.owner, name=repo.name)
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

        async def _probe() -> Any:
            """上游已存在同一个 source_branch 的 MR → 返回它，绝不重复创建。"""
            found = await self._get(
                f"{self._project(upstream)}/merge_requests",
                source_branch=source_branch,
                state="all",
                per_page=20,
            )
            for mr in found or []:
                if isinstance(mr, dict):
                    return {"number": mr.get("iid") or mr.get("id"), "url": mr.get("web_url")}
            return None

        data = await self._request(
            "POST", f"{self._project(source_repo)}/merge_requests", json=payload, probe=_probe
        )
        if not isinstance(data, dict):
            raise PlatformError(f"创建 MR 返回异常：{data}")
        return {"number": data.get("iid") or data.get("id"), "url": data.get("web_url")}

    def item_url(self, repo: RepoRef, number: int | str, item_type: ItemType) -> str:
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
