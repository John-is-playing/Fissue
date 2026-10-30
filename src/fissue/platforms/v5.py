"""v5 风格 API 的共享适配器（Gitee / AtomGit）。

Gitee 开放 API 与 AtomGit OpenAPI 的路径结构一致（``/repos/:owner/:repo/...``），
差异仅在域名、鉴权头与少量字段命名，因此抽成共同基类，子类只覆写这几处。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any

from ..errors import PlatformError, RateLimitError
from ..logging_setup import get_logger
from ..models import Comment, FileChange, ItemType, RawItem, RepoRef
from .base import PlatformAdapter, extract_linked_issues, guess_author_kind, parse_dt

log = get_logger(__name__)


def _label_names(labels: Any) -> list[str]:
    """v5 的 labels 可能是字符串数组、对象数组，或**逗号分隔的单个字符串**。

    最后那种形态必须单独处理：直接把它当可迭代对象，会按**字符**拆成
    `["b", "u", "g"]`——而 `_label_names` 同时用于「读回现值」与「判定标签」，
    拆错就会拿着垃圾标签去做并集写回（等于给仓库打上一堆单字母标签）。
    v5 的打标签接口本身就用 `label1,label2` 这种字符串形状，读写两侧必须同口径。
    """
    if isinstance(labels, str):
        labels = labels.split(",")
    out: list[str] = []
    for label in labels or []:
        # 对象数组取 name/title，字符串数组直接 str——写成一行三元，省掉 if/else 分支
        name = (
            (label.get("name") or label.get("title") or "")
            if isinstance(label, dict)
            else str(label)
        )
        name = name.strip()
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
        # 空 dict 不能往外传：httpx 会用它覆盖 URL 原有的查询串（见 base._get）
        return await self._request("GET", url, params=merged or None)

    async def _post(
        self, url: str, payload: dict[str, Any], *, probe: Callable[[], Awaitable[Any]] | None = None
    ) -> Any:
        params = self._auth_params()
        body = {**payload, **params} if params else payload
        return await self._request("POST", url, json=body, params=params or None, probe=probe)

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

    async def _create_label(
        self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI"
    ) -> bool:
        """创建单个标签，返回「该标签现在确实存在」。"""
        try:
            await self._post(f"{self._repo_api(repo)}/labels", {"name": label, "color": color.replace("#", "")})
            log.info("[%s] 已创建标签 %s", self.platform.value, label)
            return True
        except RateLimitError:
            raise
        except PlatformError as exc:
            if self._is_already_exists(exc):
                log.info("[%s] 标签 %s 已存在", self.platform.value, label)
                return True
            log.warning("[%s] 创建标签失败（不计入缓存，稍后会重试）：%s", self.platform.value, exc)
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
        self.pagination_truncated = 0       # 同上：分页到顶也算「数据不完整」
        count = 0

        # Issue
        if ItemType.ISSUE in wanted:
            params: dict[str, Any] = {"state": state, "sort": "updated", "direction": "desc"}
            if since is not None:
                # AtomGit（Spring/Java 侧）只认严格 ISO 8601：`yyyy-MM-dd'T'HH:mm:ssXXX`，
                # 即偏移必须带冒号（`+08:00`）。`%z` 产出 `+0800` 会被 400 拒掉（2026-10-20
                # 树莓派实测：PARAMETER_ERROR，整轮 AtomGit 抓取因此中止、游标不推进）。
                # isoformat 对 Gitee 同样合法（更标准的写法）。
                params["since"] = since.astimezone().isoformat(timespec="seconds")
            async for batch in self._paginate(f"{self._repo_api(repo)}/issues", params=params):
                for payload in batch:
                    # v5 的 issues 列表可能混入 PR（带 pull_request 字段），按类型分流
                    is_pr = bool(payload.get("pull_request"))
                    if is_pr and ItemType.PR not in wanted:
                        continue
                    item_type = ItemType.PR if is_pr else ItemType.ISSUE
                    detail_note = ""
                    if is_pr:
                        payload, detail_note = await self._merge_pr_detail(repo, payload)
                    try:
                        item = await self._build_item(repo, payload, item_type)
                        if detail_note:
                            item.mark_degraded(detail_note)
                        yield item
                    except RateLimitError:
                        raise
                    except Exception as exc:
                        # 单条数据本身的问题不该拖垮整批——实测 Gitee 的 Issue 编号是
                        # 字母编码，`int('IKM265')` 抛的是 ValueError，它漏过原先只接
                        # PlatformError 的处理器，**中止了整个仓库的抓取**。
                        # 跳过它并计数，由调用方判本轮数据不完整、不推进游标。
                        self._note_build_failure("条目", payload.get("number"), exc)
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
                    except RateLimitError:
                        raise
                    except Exception as exc:
                        # 同 Issue 侧：单条数据的问题不该拖垮整批（只接 PlatformError 会漏掉）。
                        self._note_build_failure("PR", number, exc)
                        continue
                    count += 1
                    if limit is not None and count >= limit:
                        return

    async def _merge_pr_detail(
        self, repo: RepoRef, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """把 PR 详情合并进列表载荷；返回 ``(载荷, 残缺原因)``。

        详情拉不到时条目仍然可用（缺的是分支名、变更统计等字段），但必须记为
        残缺——验证阶段据此判断「是否只改了文档」时会用到这些字段。
        """
        number = payload.get("number")
        if number is None:
            return payload, ""
        try:
            detail = await self._get(f"{self._repo_api(repo)}/pulls/{number}")
            if isinstance(detail, dict):
                merged = {**payload, **detail}
                return merged, ""
        except RateLimitError:
            raise
        except PlatformError as exc:
            log.warning("[%s] 拉 PR 详情失败 #%s（记为数据残缺）：%s", self.platform.value, number, exc)
            return payload, f"pr_detail_unavailable#{number}"
        return payload, ""

    async def _build_item(self, repo: RepoRef, payload: dict[str, Any], item_type: ItemType) -> RawItem:
        # 编号**保持平台原样并统一成字符串，不转 int**：Gitee 的 Issue 编号是字母编码
        # （`IKKIOZ`），强转会在整批抓取里抛 ValueError 并**中止整轮**（实测：一条都没
        # 落地）。数字平台（GitHub / AtomGit，以及 Gitee 的 PR）转字符串后行为不变。
        number = str(payload.get("number") or payload.get("id") or 0)
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
            except RateLimitError:
                raise
            except Exception as exc:      # 取不到不影响主流程：验证阶段会保守放行
                log.warning(
                    "[%s] 拉取 PR #%s 文件清单失败（记为数据残缺）：%s",
                    self.platform.value, number, exc,
                )
                raw.mark_degraded(f"files_unavailable#{number}")
            # v5 平台（Gitee / AtomGit）的 PR 对象**不提供** additions / deletions /
            # changed_files——实测 AtomGit：#943 的 payload 里这三个字段根本不存在，
            # 逐文件的行数只在 `/pulls/{n}/files` 里给。于是上面取到的全是 0，而
            # `render_item` 会把 PR 渲染成「+0/-0，0 个文件」，**评测环节据此判
            # pr_quality 就被喂了错信息**。所以文件清单到手后用求和补齐。
            # 只在平台没给时才补（不覆盖平台的真值）；注意 `fetch_files` 有条数上限，
            # 超大 MR 的总量会偏低，故对外应作「至少这么多」理解。
            if raw.files:
                if not payload.get("additions"):
                    raw.additions = sum(f.additions for f in raw.files)
                if not payload.get("deletions"):
                    raw.deletions = sum(f.deletions for f in raw.files)
                if not payload.get("changed_files"):
                    raw.changed_files = len(raw.files)
            raw.linked_issues = extract_linked_issues(title, body)
            # v5 提供 PR 关联 Issue 的专用端点
            try:
                linked = await self._get(f"{self._repo_api(repo)}/pulls/{number}/issues")
                if isinstance(linked, list):
                    # 同样不做 int()：Gitee 的关联 Issue 给的是编码。
                    # 排序键用 str：集合里可能既有数字（GitHub/AtomGit）又有编码（Gitee），
                    # 直接 sorted 会在混类型上抛 TypeError。
                    nums: set[int | str] = {
                        v
                        for x in linked
                        if isinstance(x, dict) and (v := x.get("number")) is not None
                    }
                    raw.linked_issues = sorted(nums | set(raw.linked_issues), key=str)
            except RateLimitError:
                raise
            except PlatformError as exc:
                log.warning("[%s] 拉 PR 关联 Issue 失败 #%s（记为数据残缺）：%s", self.platform.value, number, exc)
                raw.mark_degraded(f"linked_issues_unavailable#{number}")
        else:
            raw.linked_issues = extract_linked_issues(title, body)

        raw.comments, comments_note = await self._fetch_comments(
            repo, number, item_type=item_type, limit=100
        )
        if comments_note:
            raw.mark_degraded(comments_note)
        raw.author_kind = guess_author_kind(
            author, body=body, labels=labels, maintainers=(repo.owner,)
        )
        return raw

    async def _fetch_comments(
        self, repo: RepoRef, number: int | str, *, item_type: ItemType = ItemType.ISSUE, limit: int = 100
    ) -> tuple[list[Comment], str]:
        """拉评论，返回 ``(评论, 残缺原因)``；原因非空表示数据不完整。

        限流向上抛（理由见 ``GithubAdapter._fetch_comments``）；并补上分页——
        此前只取一页，``limit=100`` 形同虚设。
        """
        # v5：Issue 与 PR 的评论都走 issues 端点
        url = f"{self._repo_api(repo)}/issues/{number}/comments"
        comments: list[Comment] = []
        truncated = False
        try:
            max_pages = max(1, (limit + self.per_page - 1) // self.per_page)
            async for batch in self._paginate(url, max_pages=max_pages):
                for c in batch:
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
                        truncated = True
                        break
                if truncated:
                    break
        except RateLimitError:
            raise
        except PlatformError as exc:
            log.warning("[%s] 拉评论失败 #%s（本条记为数据残缺）：%s", self.platform.value, number, exc)
            return comments, f"comments_unavailable#{number}"
        return comments, (f"comments_may_be_truncated#{number}" if truncated else "")

    async def fetch_files(
        self, repo: RepoRef, number: int | str, *, with_patch: bool = False, max_files: int = 100
    ) -> list[FileChange]:
        files: list[FileChange] = []
        try:
            data = await self._get(f"{self._repo_api(repo)}/pulls/{number}/files")
        except RateLimitError:
            raise
        except PlatformError as exc:
            # 不在这里返回空列表：调用方（_build_item）需要知道「拉失败了」才能
            # 把条目标成残缺，否则「没有文件」与「没拉到文件」无法区分。
            log.warning("[%s] 拉 PR 文件失败 #%s：%s", self.platform.value, number, exc)
            raise
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

    async def fetch_diff(self, repo: RepoRef, number: int | str) -> str:
        """v5 支持在 PR URL 后加 ``.diff`` 直接取 diff 文本。"""
        url = f"{self._repo_api(repo)}/pulls/{number}.diff"
        return await self._request(
            "GET",
            url,
            params=self._auth_params() or None,
            expect_json=False,
            # 同 GitHub：200 + HTML 登录页/限流页不能当成 diff 放行。
            expect_text_prefix=("diff --git",),
        )

    # -- 写操作 -----------------------------------------------------------

    #: 该平台的「条目级打标签」端点（``POST /issues/{n}/labels``）是否**确认可用**。
    #:
    #: v5 系上这条端点**实测不可靠**：AtomGit 上 JSON 与表单两种体一律
    #: ``Request body parsing error``，官方文档页也取不到（404）。默认仍先试直连（对文档里
    #: 正常支持的平台保持原行为），**失败时自动落到并集写法**（见 :meth:`_patch_labels_union`）；
    #: 已经确认不可用的平台直接把这里置 ``False``，一次都不浪费。
    item_label_endpoint_usable: bool = True

    #: ``POST /issues/{n}/labels`` 的请求体形状。
    #:
    #: * ``"dict"``（默认）—— ``{"labels": ["a", "b"]}``；
    #: * ``"list"`` —— 裸数组 ``["a", "b"]``。
    #:
    #: 这个区分是真写逼出来的：Gitee 的同一端点对 ``{"labels": [...]}`` 回
    #: ``400 Problems parsing JSON``，换成裸数组立刻 ``201``（2026-10-30 在私有靶子上实测，
    #: 见 BENCHMARK.md §4.16）。形状猜错时它**不是报「字段不认识」而是报「JSON 解析失败」**，
    #: 于是很容易被误读成「平台不支持条目级写」——这个误判在本项目里活了半个月。
    item_label_body: str = "dict"

    async def _patch_labels_union(
        self, repo: RepoRef, number: int | str, labels: list[str], item_type: ItemType
    ) -> None:
        """给条目打标签：``PATCH`` 条目自身，``labels`` 传**逗号分隔的并集**（读改写）。

        为什么先读再写：``PATCH /issues/{n}`` 的 ``labels`` 语义**没有被验证过**。按 GitHub 的
        同类接口它是**替换**语义——只发新增标签就会把维护者原有的标签一并抹掉，而这件事只在真写
        的时候才暴露（读接口看不出任何异常）。取回现值、并进去再发：替换语义下正确，追加语义下
        也正确（并集里本来就有现值，重复追加是 no-op）。

        读不回就**不写**：宁可这次打标签失败（调用方会记进 ``label_errors``，看得见），也不冒
        「抹掉维护者标签」的风险。
        """
        segment = "issues" if item_type is ItemType.ISSUE else self.pr_segment
        url = f"{self._repo_api(repo)}/{segment}/{number}"
        data = await self._get(url)
        if not isinstance(data, dict):
            raise PlatformError(
                f"打标签前读不回条目当前标签（{url}）：拿到 {type(data).__name__}。"
                "v5 的 labels 可能是**替换**语义，读不到就不写——免得抹掉维护者原有的标签。"
            )
        merged = list(dict.fromkeys([*_label_names(data.get("labels")), *labels]))
        await self._patch(url, {"labels": ",".join(merged)})

    async def add_labels(self, repo: RepoRef, number: int | str, labels: list[str], item_type: ItemType = ItemType.ISSUE) -> None:
        if not labels:
            return                      # 实测空串会被拒（400），而调用方本来就只加不删
        if not self.item_label_endpoint_usable:
            await self._patch_labels_union(repo, number, labels, item_type)
            return
        try:
            # v5：Issue 与 PR 的标签都通过 issues 端点操作。
            # 体形状按平台分派：猜错的表现是「JSON 解析失败」，而不是「字段不认识」。
            url = f"{self._repo_api(repo)}/issues/{number}/labels"
            if self.item_label_body == "list":
                params = self._auth_params()
                await self._request("POST", url, json=list(labels), params=params or None)
            else:
                await self._post(url, {"labels": labels})
        except RateLimitError:
            raise
        except PlatformError as exc:
            # 直连端点不可用 → 自动落到并集写法。**哪个形状可用由真写的结果说了算**：
            # v5 的文档页取不到（404），不能靠猜，更不能把这次打标签直接丢掉。
            log.warning(
                "[%s] 条目级标签端点不可用（%s），改用 PATCH 条目做并集：%s",
                self.platform.value, number, exc,
            )
            await self._patch_labels_union(repo, number, labels, item_type)

    async def comment(self, repo: RepoRef, number: int | str, body: str, item_type: ItemType = ItemType.ISSUE) -> None:
        url = f"{self._repo_api(repo)}/issues/{number}/comments"

        async def _probe() -> Any:
            """已存在逐字相同的评论 → 说明这次写其实已经生效了。"""
            data = await self._get(url, per_page=100)
            for c in data or []:
                if isinstance(c, dict) and (c.get("body") or "") == body:
                    return c
            return None

        await self._post(url, {"body": body}, probe=_probe)

    async def close_item(self, repo: RepoRef, number: int | str, item_type: ItemType = ItemType.ISSUE) -> None:
        await self._patch(f"{self._repo_api(repo)}/issues/{number}", {"state": "closed"})

    async def fork_repo(self, repo: RepoRef) -> RepoRef:
        # 项目**已经在自己名下**时不要再 fork —— 与 GitLab 是同一个坑（那边实测
        # 409「Project namespace name已经被使用」）：仓库已在我们的命名空间里，
        # 再 fork 等于要在同一命名空间建同名项目。返回仓库自身即可——
        # `create_pr` 的 head 支持「同仓库 + 分支」形式。
        me = await self.whoami()
        if me and me.lower() == repo.owner.lower():
            return RepoRef(platform=self.platform, owner=repo.owner, name=repo.name)
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

    async def find_open_pr(self, upstream: RepoRef, *, head: str) -> dict[str, Any] | None:
        """按 head 查**仍开放**的 PR，没有则返回 None。

        v5 原先**没有覆写**这个方法（基类默认返回 None），于是 Gitee / AtomGit 上永远查不到
        已存在的 PR → 不走「复用已有 PR」而直接去强推 → 被 `--force-with-lease` 正确拦下，
        表现为「推送失败」（实测 AtomGit）。

        两个要点：**head 要裸分支名**（与 `create_pr` 同一个坑）；**只认 open**——`state=all`
        会把**已关闭**的同头 PR 也算成「已有」，于是跳过推送、直接报成功却什么都没干
        （关 PR 不删分支，重跑必踩）。另**必须按分支核对**：平台若不支持 `head` 过滤参数，
        会把整张列表返回，拿第一条充数就会误报「已提 PR」。
        """
        # `head` 可能是调用方按 GitHub 习惯拼的 `owner:branch`，也可能是 fork 的
        # `ns/repo:branch`；而平台 PR 里的 `head.ref` 是**裸分支名**。所以比对一律取
        # **分支那一段**——否则 fork 场景下永远匹配不上，而 fork 恰恰是这里要修的场景。
        branch = head.partition(":")[2] or head
        data = await self._get(
            f"{self._repo_api(upstream)}/pulls",
            head=branch,
            state="open",
            per_page=20,
        )
        for pr in data or []:
            if not isinstance(pr, dict):
                continue
            head_raw = pr.get("head")
            pr_head: dict[str, Any] = head_raw if isinstance(head_raw, dict) else {}
            ref = str(pr_head.get("ref") or pr.get("source_branch") or "")
            if ref != branch:
                # 平台没给分支字段（`ref` 为空）时**不能拿列表第一条充数**——那会谎报
                # 「已有 PR」，让 `submit` 跳过推送却记成功。宁可查不到：查不到就去推，
                # 推不动会被 `--force-with-lease` **响亮**拦下，比假装成功好。
                continue
            return {
                "number": pr.get("number") or pr.get("iid") or pr.get("id"),
                "url": pr.get("html_url") or pr.get("url"),
            }
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
        # AtomGit / Gitee 的建 PR 接口要**裸分支名**：调用方按 GitHub 习惯拼的
        # `owner:branch` 会被拒（实测 AtomGit：`POST /pulls` 返回 400，**错误体还是空的**，
        # 排查全靠拿真实分支逐个形状去试）。fork 场景的 `namespace/repo:branch` 带着
        # 别的 owner，不能剥——只剥「前缀正好是上游 owner」那一种。
        head_ref = head
        owner_part, sep, branch_part = head.partition(":")
        if sep and branch_part and owner_part.lower() == upstream.owner.lower():
            head_ref = branch_part

        async def _probe() -> Any:
            """同一个 head 上已经有 PR → 返回它，绝不重复创建。

            直接复用 :meth:`find_open_pr`：它在「只认 open」与「必须按分支核对」两件事上
            都有明确判定。这里原先自己写了另一版——**拿列表第一条充数**：平台若不支持
            `head=` 过滤就会返回整张列表，于是写请求结果不明时（5xx / 读超时）会把
            **别人的 PR** 记成自己刚建的，条目记为成功且不再重试。
            """
            return await self.find_open_pr(upstream, head=head)

        data = await self._post(
            f"{self._repo_api(upstream)}/pulls",
            {"title": title, "head": head_ref, "base": base, "body": body, "draft": draft},
            probe=_probe,
        )
        if not isinstance(data, dict):
            raise PlatformError(f"创建 PR 返回异常：{data}")
        return {
            "number": data.get("number") or data.get("id"),
            "url": data.get("html_url") or data.get("url"),
        }

    def item_url(self, repo: RepoRef, number: int | str, item_type: ItemType) -> str:
        seg = self.issue_segment if item_type is ItemType.ISSUE else self.pr_segment
        return f"https://{self.web_host}/{repo.owner}/{repo.name}/{seg}/{number}"

    issue_segment: str = "issues"
    pr_segment: str = "pulls"
