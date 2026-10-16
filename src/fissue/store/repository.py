"""仓储层：把领域模型与 ORM 表互相转换，封装常用查询。

对外只暴露语义化方法（``upsert_item``、``latest_evaluation``、``queue_pending`` …），
让流水线代码不直接碰 SQL。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import Select, and_, delete, func, select, update
from sqlalchemy.orm import Session, selectinload

from ..logging_setup import get_logger
from ..models import (
    MATERIAL_FIELDS,
    STALE_KEY,
    AuthorKind,
    BatchConclusion,
    Category,
    Comment,
    Evaluation,
    FileChange,
    FixAttempt,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    QueueName,
    RawItem,
    RepoRef,
    VerifierKind,
    VerifierRun,
    VerifierSpec,
)
from .db import Database
from .tables import (
    BatchConclusionRow,
    CommentRow,
    EvaluationRow,
    FixAttemptRow,
    ItemRow,
    LLMUsageRow,
    NotificationRow,
    QueueEntryRow,
    RepoRow,
    ScanRunRow,
    VerifierRow,
    VerifierRunRow,
)

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: 已有结论**已进入执行**的条目状态。内容实质变化时这些状态**不回落**——回落到 new 会让
#: 条目重新走一遍评测，而队列里那条修复派发还在，等于给「重复修复/重复建 PR」再开一道口子。
#: 代价是它们的结论会一直过期，所以必须把过期如实留痕（见 :meth:`Repository._mark_conclusion_stale`）。
IN_FLIGHT_STATUSES: frozenset[str] = frozenset(
    {ItemStatus.FIX_QUEUED.value, ItemStatus.FIXING.value, ItemStatus.PR_CREATED.value}
)


def _material_value(value: Any) -> str:
    """把实质字段归一成可比较的字符串（labels 顺序不该影响判定）。"""
    if isinstance(value, (list, tuple, set)):
        return ",".join(sorted(str(v) for v in value))
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value or "")


def _material_differs(row: ItemRow, item: RawItem) -> bool:
    """实质字段（``MATERIAL_FIELDS``）是否变了。**不含评论条数**——见该常量的注释。"""
    return any(
        _material_value(getattr(row, field, None)) != _material_value(getattr(item, field, None))
        for field in MATERIAL_FIELDS
    )


def _has_stale_mark(raw: Any) -> bool:
    """``raw`` 里有没有「结论已过期」的留痕（见 ``models.STALE_KEY``）。"""
    marks = raw.get(STALE_KEY) if isinstance(raw, dict) else None
    return isinstance(marks, list) and bool(marks)


# 尚未进入后续流程的状态：批量定论可以把它们推进到 labeled。
# 其余（fix_queued / fixing / pr_created / needs_manual / skipped / failed）表示
# 已经走到了更靠后的环节，不应被「打标签」这一步回退覆盖。
_PRELIMINARY_STATUSES = {
    ItemStatus.NEW.value,
    ItemStatus.CLASSIFIED.value,
    ItemStatus.EVALUATED.value,
    ItemStatus.VERIFIER_READY.value,
    ItemStatus.QUEUED.value,
    ItemStatus.VERIFYING.value,
    ItemStatus.VERIFIED.value,
    ItemStatus.LABELED.value,
}


# ---------------------------------------------------------------------------
# 转换函数
# ---------------------------------------------------------------------------


def _item_to_domain(row: ItemRow, *, with_comments: bool = True) -> RawItem:
    return RawItem(
        platform=Platform(row.platform),
        repo=row.repo_slug,
        number=row.number,
        item_type=ItemType(row.item_type),
        title=row.title or "",
        body=row.body or "",
        state=row.state or "open",
        labels=list(row.labels or []),
        author=row.author or "",
        author_kind=AuthorKind(row.author_kind or "unknown"),
        comments=[
            Comment(
                author=c.author,
                author_kind=AuthorKind(c.author_kind or "unknown"),
                body=c.body or "",
                created_at=c.created_at,
            )
            for c in (row.comments if with_comments else [])
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
        closed_at=row.closed_at,
        is_draft=bool(row.is_draft),
        merged=bool(row.merged),
        mergeable=row.mergeable,
        base_branch=row.base_branch,
        head_branch=row.head_branch,
        head_repo=row.head_repo,
        linked_issues=list(row.linked_issues or []),
        files=[FileChange(**f) for f in (row.files or [])],
        additions=row.additions or 0,
        deletions=row.deletions or 0,
        changed_files=row.changed_files or 0,
        category=Category(row.category or "unknown"),
        status=ItemStatus(row.status or "new"),
        priority=Priority(row.priority or "none"),
        raw=dict(row.raw or {}),
    )


def _evaluation_to_domain(row: EvaluationRow) -> Evaluation:
    from ..models import DimensionScore, Scores, SpamSignal

    raw_scores = row.scores or {}
    scores = Scores(
        **{
            name: DimensionScore(**raw_scores[name])
            for name in (
                "authenticity",
                "importance",
                "feasibility",
                "pr_quality",
                "difficulty",
            )
            if isinstance(raw_scores.get(name), dict)
        }
    )
    return Evaluation(
        scores=scores,
        category=Category(row.category or "unknown"),
        labels_suggested=list(row.labels_suggested or []),
        action=__import__("fissue.models", fromlist=["Action"]).Action(row.action or "triage"),
        summary=row.summary or "",
        spam=SpamSignal(**(row.spam or {})),
        priority=Priority(row.priority or "none"),
        model=row.model or "",
        prompt_tokens=row.prompt_tokens or 0,
        completion_tokens=row.completion_tokens or 0,
        cost_usd=row.cost_usd or 0.0,
        created_at=row.created_at,
    )


def _verifier_to_domain(row: VerifierRow) -> VerifierSpec:
    return VerifierSpec(
        kind=VerifierKind(row.kind),
        name=row.name or "verifier",
        language=row.language or "python",
        files=dict(row.files or {}),
        command=row.command,
        checklist=list(row.checklist or []),
        expect_fail_on_base=bool(row.expect_fail_on_base),
        expect_pass_on_fix=bool(row.expect_pass_on_fix),
        timeout_seconds=row.timeout_seconds or 600,
        notes=row.notes or "",
    )


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


class Repository:
    """高层数据访问对象。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- 仓库 -------------------------------------------------------------

    def ensure_repo(
        self,
        ref: RepoRef,
        *,
        base_branch: str | None = None,
        enabled: bool = True,
        extra: dict | None = None,
    ) -> int:
        with self.db.session() as s:
            row = s.scalar(select(RepoRow).where(RepoRow.slug == ref.slug))
            if row is None:
                row = RepoRow(
                    platform=ref.platform.value,
                    owner=ref.owner,
                    name=ref.name,
                    slug=ref.slug,
                    base_branch=base_branch,
                    enabled=enabled,
                    extra=extra or {},
                )
                s.add(row)
                s.flush()
            else:
                if base_branch:
                    row.base_branch = base_branch
                row.enabled = enabled
            return int(row.id)

    def get_repo(self, slug: str) -> RepoRow | None:
        with self.db.session() as s:
            row = s.scalar(select(RepoRow).where(RepoRow.slug == slug))
            if row is not None:
                s.expunge(row)
            return row

    def list_repos(self) -> list[RepoRow]:
        with self.db.session() as s:
            rows = list(s.scalars(select(RepoRow).order_by(RepoRow.slug)))
            for r in rows:
                s.expunge(r)
            return rows

    def set_repo_cursor(self, slug: str, cursor: dict[str, Any]) -> None:
        with self.db.session() as s:
            row = s.scalar(select(RepoRow).where(RepoRow.slug == slug))
            if row is None:
                return
            merged = dict(row.cursor or {})
            merged.update(cursor)
            row.cursor = merged
            row.last_scanned_at = _now()

    def get_repo_cursor(self, slug: str) -> dict[str, Any]:
        row = self.get_repo(slug)
        return dict(row.cursor or {}) if row else {}

    # -- 条目 -------------------------------------------------------------

    def upsert_item(self, repo_id: int, item: RawItem) -> tuple[int, bool, bool]:
        """插入或更新条目。

        :return: ``(item_id, created, changed)`` —— changed 表示内容指纹变化（**含评论条数**）。

        ``changed`` 只说明「平台侧的这条和库里不一样」，不直接等于「要重新评测」：指纹里带着
        ``len(comments)``，每来一条评论都会为真。真正让已有结论作废的是**实质字段**
        （``MATERIAL_FIELDS``）变化——那时本条目的结论会被标记过期，且只要结论还没进入执行
        （见 ``IN_FLIGHT_STATUSES``）就回落为 ``new`` 等待重评。
        """
        content_hash = item.content_hash()
        with self.db.session() as s:
            row = s.scalar(
                select(ItemRow).where(
                    and_(
                        ItemRow.platform == item.platform.value,
                        ItemRow.repo_slug == item.repo,
                        ItemRow.number == item.number,
                        ItemRow.item_type == item.item_type.value,
                    )
                )
            )
            created = row is None
            changed = created
            if row is None:
                row = ItemRow(
                    repo_id=repo_id,
                    platform=item.platform.value,
                    repo_slug=item.repo,
                    number=item.number,
                    item_type=item.item_type.value,
                    key=item.key,
                    content_hash=content_hash,
                    status=ItemStatus.NEW.value,
                )
                s.add(row)
            elif row.content_hash != content_hash:
                changed = True

            # 「内容实质变化」（标题/正文/标签/状态/merged）与「讨论增长」（评论条数）要分开：
            # 前者让已有结论作废，后者只重写评论表。必须在下面给 row 赋值**之前**算——
            # 这里读到的还是旧值。
            material_changed = not created and changed and _material_differs(row, item)

            # 更新字段
            row.repo_id = repo_id
            row.title = item.title or ""
            row.body = item.body or ""
            row.state = item.state or "open"
            row.labels = list(item.labels)
            row.author = item.author or ""
            row.author_kind = item.author_kind.value
            row.is_draft = item.is_draft
            row.merged = item.merged
            row.mergeable = item.mergeable
            row.base_branch = item.base_branch
            row.head_branch = item.head_branch
            row.head_repo = item.head_repo
            row.linked_issues = list(item.linked_issues)
            row.additions = item.additions
            row.deletions = item.deletions
            row.changed_files = item.changed_files
            row.files = [f.model_dump(mode="json") for f in item.files]
            row.created_at = item.created_at
            row.updated_at = item.updated_at
            row.closed_at = item.closed_at
            row.content_hash = content_hash
            incoming_raw = dict(item.raw or {})
            if isinstance(row.raw, dict) and STALE_KEY in row.raw:
                # 过期标记必须**黏住**：下一次抓取时内容已经和库里的 hash 一致（changed=False），
                # 不会再重新检测出「变过」。若在这里被 item.raw 冲掉，过期就重新变成不可见了。
                # 清零点只有一个：save_evaluation（重新出了结论）。
                incoming_raw.setdefault(STALE_KEY, row.raw[STALE_KEY])
            row.raw = incoming_raw
            row.fetched_at = _now()
            if created:
                row.category = Category.UNKNOWN.value
            s.flush()
            item_id = int(row.id)

            if material_changed:
                self._mark_conclusion_stale(row)

            # 评论：整体替换（简单可靠，评论量不大）
            if changed:
                stored = int(
                    s.scalar(
                        select(func.count())
                        .select_from(CommentRow)
                        .where(CommentRow.item_id == item_id)
                    )
                    or 0
                )
                if item.comments_incomplete and len(item.comments) < stored:
                    # 本轮评论没拉全（接口失败/被截断）且比库里还少 → **绝不能**用它覆盖。
                    # 原先这里照删照写：一次评论接口抖动就把整条讨论清空（评测随后可能就基于
                    # 「零评论」下结论，而结论会被回写到平台、增量抓取又不会再碰这条——错误固化）。
                    log.warning(
                        "保留已入库讨论 %s：本轮评论残缺且更少（收到 %d 条 < 库里 %d 条，原因 %s）",
                        item.key, len(item.comments), stored, item.degraded,
                    )
                else:
                    s.execute(delete(CommentRow).where(CommentRow.item_id == item_id))
                    for c in item.comments:
                        s.add(
                            CommentRow(
                                item_id=item_id,
                                author=c.author,
                                author_kind=c.author_kind.value,
                                body=c.body,
                                created_at=c.created_at,
                            )
                        )
            return item_id, created, changed

    def _mark_conclusion_stale(self, row: ItemRow) -> None:
        """把「已有结论已过期」如实留痕，并按状态决定要不要回落重评。

        两种处理：

        * 结论**还没进入执行** → 回落成 ``new``，交给既有的评测路径重新评测。旧结论留在
          ``evaluations`` 表里，历史不丢。
        * 结论**已经进入执行**（``IN_FLIGHT_STATUSES``）→ 只标记 + 响亮告警，**不回落**。
          回落会让条目重走评测，而队列里那条派发还在，等于给「重复修复 / 重复建 PR」再开
          一道口子。代价是它的结论会一直过期，所以标记必须能被报告/看板看到
          （``RawItem.stale_reasons``，导出与 API 都会带上）。
        """
        raw = dict(row.raw or {})
        marks = raw.get(STALE_KEY)
        marks = list(marks) if isinstance(marks, list) else []
        mark = f"content_changed_at={_now().isoformat(timespec='seconds')}"
        if mark not in marks:
            marks.append(mark)
        raw[STALE_KEY] = marks
        row.raw = raw        # 必须整体赋值：原地改 JSON 列不会被 SQLAlchemy 跟踪

        if row.status in IN_FLIGHT_STATUSES:
            log.warning(
                "内容实质变化但结论已进入执行（status=%s，key=%s）：不自动重评，只标记结论过期；"
                "要重评请人工把它置回 new 后重跑评测",
                row.status, row.key,
            )
            return
        if row.status != ItemStatus.NEW.value:
            log.info("内容实质变化（key=%s）：已有结论作废，置回 new 等待重新评测", row.key)
            row.status = ItemStatus.NEW.value

    def get_item(self, key: str) -> RawItem | None:
        with self.db.session() as s:
            row = s.scalar(
                select(ItemRow).options(selectinload(ItemRow.comments)).where(ItemRow.key == key)
            )
            return _item_to_domain(row) if row else None

    def get_item_row(self, key: str) -> ItemRow | None:
        with self.db.session() as s:
            row = s.scalar(select(ItemRow).where(ItemRow.key == key))
            if row is not None:
                s.expunge(row)
            return row

    def list_items(
        self,
        *,
        repo_slug: str | None = None,
        item_type: ItemType | None = None,
        status: ItemStatus | None = None,
        category: Category | None = None,
        priority: Priority | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "updated_at",
        desc: bool = True,
    ) -> list[RawItem]:
        stmt: Select = select(ItemRow)
        if repo_slug:
            stmt = stmt.where(ItemRow.repo_slug == repo_slug)
        if item_type:
            stmt = stmt.where(ItemRow.item_type == item_type.value)
        if status:
            stmt = stmt.where(ItemRow.status == status.value)
        if category:
            stmt = stmt.where(ItemRow.category == category.value)
        if priority:
            stmt = stmt.where(ItemRow.priority == priority.value)
        col = getattr(ItemRow, order_by, ItemRow.updated_at)
        stmt = stmt.order_by(col.desc() if desc else col.asc()).limit(limit).offset(offset)
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            return [_item_to_domain(r, with_comments=False) for r in rows]

    def set_item_status(
        self,
        key: str,
        status: ItemStatus,
        *,
        category: Category | None = None,
        priority: Priority | None = None,
    ) -> None:
        with self.db.session() as s:
            values: dict[str, Any] = {"status": status.value}
            if category is not None:
                values["category"] = category.value
            if priority is not None:
                values["priority"] = priority.value
            s.execute(update(ItemRow).where(ItemRow.key == key).values(**values))

    def reclaim_fixing_items(self, *, repo_slug: str | None = None) -> list[str]:
        """把「卡在 ``fixing``」的条目放回 ``fix_queued``，返回被恢复的 key。

        为什么需要它：``fix_item`` 在**开工前**就把状态置为 ``FIXING``，但只有
        正常结束（写回终态或 dry-run 主动归还）才会离开该状态。进程被 Ctrl-C /
        崩溃打断时，条目会永久留在 ``FIXING``，而 ``fix_candidates`` 只取
        ``FIX_QUEUED``——于是这条**再也不会被修**，且没有任何命令能放它回去
        （demo 实测：pipekit #12 就是这样卡住的，Ctrl-C 后从候选里消失）。

        安全性：调用点在**一批修复开始之前**，此时本进程自己还没把任何条目置为
        ``FIXING``，所以恢复到的必然是上一次（已死）运行留下的残留。同一进程内
        修复是顺序执行的，不存在「误抢自己正在修的条目」。
        跨进程例外：若 ``serve`` 正在修某条、同时你手动跑 ``fix``，那条会被这里
        放回队列。系统本来就没有跨进程的修复互斥，此处不引入新风险。
        """
        with self.db.session() as s:
            stmt = select(ItemRow.key).where(ItemRow.status == ItemStatus.FIXING.value)
            if repo_slug:
                stmt = stmt.where(ItemRow.repo_slug == repo_slug)
            keys = list(s.scalars(stmt))
            if keys:
                s.execute(
                    update(ItemRow)
                    .where(ItemRow.key.in_(keys))
                    .values(status=ItemStatus.FIX_QUEUED.value)
                )
            return keys

    #: 队列行停在 ``pending`` 超过多久算「阶段中断残留」。见 :meth:`reclaim_stuck_queue_entries`。
    STUCK_QUEUE_ENTRY_SECONDS = 3600

    def items_with_stale_conclusions(self, *, repo_slug: str | None = None) -> list[str]:
        """列出「结论已过期、但当时已在执行」因而**没有自动重评**的条目 key。

        这类条目只剩留痕（``RawItem.stale_reasons``）：内容实质变化时它们的修复已开工，
        自动回落重评会给「重复修复 / 重复建 PR」再开一道口子（见 :meth:`_mark_conclusion_stale`），
        所以留给人工显式重排队——入口是 ``fissue requeue --stale``。

        只查「已在执行」的那几个状态（数量天然很小）再在 Python 里过滤留痕：
        ``raw`` 是 JSON 列，跨 SQLite / PostgreSQL 的 JSON 查询写法不值得为这点量去写。
        """
        stmt = select(ItemRow).where(ItemRow.status.in_(sorted(IN_FLIGHT_STATUSES)))
        if repo_slug:
            stmt = stmt.where(ItemRow.repo_slug == repo_slug)
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            return [str(r.key) for r in rows if _has_stale_mark(r.raw)]

    def reclaim_stuck_queue_entries(self, *, stale_seconds: int | None = None) -> list[str]:
        """回收「阶段中断」留下的队列残留，返回被放回重做的条目 key。

        为什么需要：``stages`` 的写法是三连——``set_item_status(QUEUED)`` → ``enqueue()``
        → ``finish_queue_entry(done)``。进程在这三步之间被 Ctrl-C / 崩溃打断，队列行会
        **永久**停在 ``pending``：

        * ``queue_unflushed`` 只认 ``done``，这行永远不会被 flush；
        * ``queue_pending_count`` 于是恒大于 0，``should_flush`` 的**「队列已无待处理条目」
          这条兜底被永久废掉**（只剩数量阈值与空闲超时）；
        * 条目自己卡在 ``queued``：``verify`` 只取 ``evaluated``，它既不会被重验，也永远
          不会被打标签或派发。

        修法与 :meth:`reclaim_fixing_items` 同一套路：把残留行删掉（它从没产出过任何结果，
        删掉不丢信息；比改 ``failed`` 更稳——``(queue,item_key,status)`` 是唯一键，改状态
        会撞上存量脏数据里已存在的 ``failed`` 行），条目放回 ``evaluated``，``verify`` 正是
        从这里重新处理的。可见性靠这条 warning 日志 + 条目状态变化。

        安全性：调用点在**开始一轮 flush 之前**，此时本进程还没入队任何东西，回收到的必然
        是上一次（已死）运行的残留。跨进程例外与 ``reclaim_fixing_items`` 相同——系统本来
        就没有跨进程的队列互斥。
        """
        window = self.STUCK_QUEUE_ENTRY_SECONDS if stale_seconds is None else stale_seconds
        cutoff = _now() - timedelta(seconds=window)
        with self.db.session() as s:
            rows = list(
                s.scalars(
                    select(QueueEntryRow).where(
                        and_(
                            QueueEntryRow.status == "pending",
                            QueueEntryRow.enqueued_at.is_not(None),
                            QueueEntryRow.enqueued_at < cutoff,
                        )
                    )
                )
            )
            if not rows:
                return []
            keys = sorted({str(r.item_key) for r in rows})
            for row in rows:
                s.delete(row)
            s.execute(
                update(ItemRow)
                .where(
                    and_(
                        ItemRow.key.in_(keys),
                        # 只动「卡在 queued」的：条目若已被人工/后续流程推到别的状态，不覆盖
                        ItemRow.status == ItemStatus.QUEUED.value,
                    )
                )
                .values(status=ItemStatus.EVALUATED.value)
            )
            log.warning(
                "回收 %d 条「入队后未收尾」的队列残留（条目置回 evaluated 重做）：%s",
                len(rows), "、".join(keys[:5]),
            )
            return keys

    def set_item_category(self, key: str, category: Category) -> None:
        with self.db.session() as s:
            s.execute(update(ItemRow).where(ItemRow.key == key).values(category=category.value))

    def count_items(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(ItemRow)
        if repo_slug := filters.get("repo_slug"):
            stmt = stmt.where(ItemRow.repo_slug == repo_slug)
        if item_type := filters.get("item_type"):
            stmt = stmt.where(ItemRow.item_type == item_type.value)
        if status := filters.get("status"):
            stmt = stmt.where(ItemRow.status == status.value)
        with self.db.session() as s:
            return int(s.scalar(stmt) or 0)

    def items_needing_eval(self, *, repo_slug: str | None = None, limit: int = 50) -> list[RawItem]:
        """取需要（重新）评测的条目：状态为 new，或内容变过且未评测。"""
        stmt = select(ItemRow).where(ItemRow.status.in_([ItemStatus.NEW.value, ItemStatus.CLASSIFIED.value]))
        if repo_slug:
            stmt = stmt.where(ItemRow.repo_slug == repo_slug)
        stmt = stmt.order_by(ItemRow.updated_at.desc().nullslast()).limit(limit)
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            return [_item_to_domain(r, with_comments=False) for r in rows]

    def items_by_keys(self, keys: Sequence[str]) -> list[RawItem]:
        if not keys:
            return []
        with self.db.session() as s:
            rows = list(s.scalars(select(ItemRow).where(ItemRow.key.in_(list(keys)))))
            return [_item_to_domain(r, with_comments=False) for r in rows]

    # -- 评测 -------------------------------------------------------------

    def save_evaluation(self, item_key: str, evaluation: Evaluation) -> int:
        with self.db.session() as s:
            item = s.scalar(select(ItemRow).where(ItemRow.key == item_key))
            if item is None:
                raise ValueError(f"条目不存在：{item_key}")
            row = EvaluationRow(
                item_id=item.id,
                item_key=item_key,
                scores={
                    name: dim.model_dump(mode="json")
                    for name in (
                        "authenticity",
                        "importance",
                        "feasibility",
                        "pr_quality",
                        "difficulty",
                    )
                    if (dim := evaluation.scores.get(name)) is not None
                },
                category=evaluation.category.value,
                action=evaluation.action.value,
                priority=evaluation.priority.value,
                labels_suggested=list(evaluation.labels_suggested),
                summary=evaluation.summary,
                spam=evaluation.spam.model_dump(mode="json"),
                model=evaluation.model,
                prompt_tokens=evaluation.prompt_tokens,
                completion_tokens=evaluation.completion_tokens,
                cost_usd=evaluation.cost_usd,
            )
            s.add(row)
            item.category = evaluation.category.value
            item.priority = evaluation.priority.value
            if item.status in (ItemStatus.NEW.value,):
                item.status = ItemStatus.EVALUATED.value
            elif (
                item.status == ItemStatus.FIX_QUEUED.value
                and evaluation.priority is Priority.NONE
            ):
                # 重评后不再够格自动修复（如新判定为疑似重复、难度超阈值），
                # 若状态仍停在 fix_queued 就会与 priority=none 自相矛盾，
                # 报告/看板显示可疑。收敛为需人工，避免留下不一致的流转痕迹。
                # 只动 fix_queued：fixing / pr_created 表示修复已开工，不回退。
                item.status = ItemStatus.NEEDS_MANUAL.value

            # 重新出了结论，「已过期」这个标记就该消失——它表达的是「结论 vs 内容」是否一致。
            # 不清的话，条目会永远显示成过期（而内容可能再没变过）。
            if isinstance(item.raw, dict) and STALE_KEY in item.raw:
                fresh = dict(item.raw)
                fresh.pop(STALE_KEY, None)
                item.raw = fresh

            s.flush()
            return int(row.id)

    def latest_evaluation(self, item_key: str) -> Evaluation | None:
        with self.db.session() as s:
            row = s.scalar(
                select(EvaluationRow)
                .where(EvaluationRow.item_key == item_key)
                .order_by(EvaluationRow.created_at.desc(), EvaluationRow.id.desc())
                .limit(1)
            )
            return _evaluation_to_domain(row) if row else None

    def evaluations_for(self, keys: Sequence[str]) -> dict[str, Evaluation]:
        if not keys:
            return {}
        with self.db.session() as s:
            rows = list(
                s.scalars(
                    select(EvaluationRow)
                    .where(EvaluationRow.item_key.in_(list(keys)))
                    .order_by(EvaluationRow.id.desc())
                )
            )
        out: dict[str, Evaluation] = {}
        for row in rows:
            out.setdefault(row.item_key, _evaluation_to_domain(row))
        return out

    # -- 验证器 -----------------------------------------------------------

    def save_verifier(self, item_key: str, spec: VerifierSpec, *, rounds: int = 1) -> int:
        with self.db.session() as s:
            item = s.scalar(select(ItemRow).where(ItemRow.key == item_key))
            if item is None:
                raise ValueError(f"条目不存在：{item_key}")
            row = VerifierRow(
                item_id=item.id,
                item_key=item_key,
                kind=spec.kind.value,
                name=spec.name,
                language=spec.language,
                files=spec.files,
                command=spec.command,
                checklist=list(spec.checklist),
                expect_fail_on_base=spec.expect_fail_on_base,
                expect_pass_on_fix=spec.expect_pass_on_fix,
                timeout_seconds=spec.timeout_seconds,
                notes=spec.notes,
                generation_rounds=rounds,
                f2p_satisfied=None,
            )
            s.add(row)
            item.status = ItemStatus.VERIFIER_READY.value
            s.flush()
            return int(row.id)

    def latest_verifier(self, item_key: str) -> tuple[int, VerifierSpec] | None:
        with self.db.session() as s:
            row = s.scalar(
                select(VerifierRow)
                .where(VerifierRow.item_key == item_key)
                .order_by(VerifierRow.id.desc())
                .limit(1)
            )
            return (int(row.id), _verifier_to_domain(row)) if row else None

    def set_verifier_f2p(self, verifier_id: int, ok: bool) -> None:
        with self.db.session() as s:
            s.execute(update(VerifierRow).where(VerifierRow.id == verifier_id).values(f2p_satisfied=ok))

    def save_verifier_run(self, run: VerifierRun) -> int:
        """落库一次验证器执行。

        ``verifier_id`` 在这里是**必填**（表上就是 NOT NULL）。域模型与
        ``VerifierRunner.run_once`` 的参数都允许 ``None``——活链路里
        `generate_and_validate` 每轮都会先 ``save_verifier`` 拿到真 id，所以那条路走不到；
        但边界这里挡一道，免得将来某个调用方撞上一条难以定位的
        ``IntegrityError: NOT NULL constraint failed: verifier_runs.verifier_id``。
        """
        if run.verifier_id is None:
            raise ValueError(
                f"验证器执行记录缺少 verifier_id（item={run.item_key} stage={run.stage}）："
                "verifier_runs.verifier_id 是 NOT NULL，请先 repo.save_verifier(...) 拿到 id"
            )
        with self.db.session() as s:
            row = VerifierRunRow(
                verifier_id=run.verifier_id,
                item_key=run.item_key,
                stage=run.stage,
                outcome=run.outcome.value,
                exit_code=run.exit_code,
                stdout=run.stdout[-20000:],
                stderr=run.stderr[-20000:],
                duration_seconds=run.duration_seconds,
                f2p_ok=run.f2p_ok,
                artifacts_dir=run.artifacts_dir,
                error=run.error,
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def verifier_runs(self, item_key: str, *, stage: str | None = None, limit: int = 20) -> list[VerifierRun]:
        stmt = select(VerifierRunRow).where(VerifierRunRow.item_key == item_key)
        if stage:
            stmt = stmt.where(VerifierRunRow.stage == stage)
        stmt = stmt.order_by(VerifierRunRow.id.desc()).limit(limit)
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
        from ..models import VerifierOutcome

        return [
            VerifierRun(
                verifier_id=r.verifier_id,
                item_key=r.item_key,
                stage=r.stage,
                outcome=VerifierOutcome(r.outcome),
                exit_code=r.exit_code,
                stdout=r.stdout or "",
                stderr=r.stderr or "",
                duration_seconds=r.duration_seconds or 0.0,
                f2p_ok=r.f2p_ok,
                artifacts_dir=r.artifacts_dir,
                error=r.error,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # -- 队列 -------------------------------------------------------------

    def enqueue(
        self,
        queue: QueueName,
        item_key: str,
        *,
        item_id: int | None = None,
        payload: dict | None = None,
    ) -> int:
        """入队（同一队列同一条目只保留**一行**，重复入队即复用并重置）。

        为什么不是「只复用 pending/running」：唯一约束 ``(queue,item_key,status)``
        把终态也算进去了，所以「同一队列同一条目」只能存在**一个** ``done`` 行。
        若重跑时新插一行 pending，它稍后转 ``done`` 就会撞上上一轮遗留的 ``done``，
        抛 ``UNIQUE constraint failed``（重跑验证必崩）。这里改为复用同一行并重置，
        顺带收敛库里可能已存在的重复行——历史留痕在 ``verifier_runs`` /
        ``batch_conclusions`` 里，队列只承载「当前状态」。
        """
        with self.db.session() as s:
            rows = list(
                s.scalars(
                    select(QueueEntryRow)
                    .where(
                        and_(
                            QueueEntryRow.queue == queue.value,
                            QueueEntryRow.item_key == item_key,
                        )
                    )
                    .order_by(QueueEntryRow.id)
                )
            )
            if rows:
                keep, extras = rows[0], rows[1:]
                for extra in extras:
                    s.delete(extra)
                if extras:
                    # 必须先**真正删掉**多余行再改 keep 的状态：SQLAlchemy 的工作单元
                    # 先执行 UPDATE 后执行 DELETE，否则新状态会撞上尚未删除的旧行。
                    s.flush()
                keep.status = "pending"
                keep.item_id = item_id if item_id is not None else keep.item_id
                keep.payload = payload or {}
                keep.result = {}
                keep.error = None
                keep.attempts = 0
                keep.started_at = None
                keep.finished_at = None
                keep.flushed_at = None
                keep.enqueued_at = _now()
                s.flush()
                return int(keep.id)

            row = QueueEntryRow(
                queue=queue.value,
                item_key=item_key,
                item_id=item_id,
                status="pending",
                payload=payload or {},
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def claim_next(self, queue: QueueName, *, max_attempts: int = 3) -> QueueEntryRow | None:
        """原子地取出一条 pending 并标记 running。"""
        with self.db.session() as s:
            stmt = (
                select(QueueEntryRow)
                .where(
                    and_(
                        QueueEntryRow.queue == queue.value,
                        QueueEntryRow.status == "pending",
                        QueueEntryRow.attempts < max_attempts,
                    )
                )
                .order_by(QueueEntryRow.enqueued_at.asc())
                .limit(1)
            )
            stmt = (
                stmt.with_for_update()
                if self.db.is_sqlite
                else stmt.with_for_update(skip_locked=True)
            )
            row = s.scalar(stmt)
            if row is None:
                return None
            row.status = "running"
            row.started_at = _now()
            row.attempts += 1
            s.flush()
            s.expunge(row)
            return row

    def finish_queue_entry(
        self,
        entry_id: int,
        *,
        status: str = "done",
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self.db.session() as s:
            s.execute(
                update(QueueEntryRow)
                .where(QueueEntryRow.id == entry_id)
                .values(status=status, result=result or {}, error=error, finished_at=_now())
            )

    def queue_pending_count(self, queue: QueueName) -> int:
        with self.db.session() as s:
            return int(
                s.scalar(
                    select(func.count())
                    .select_from(QueueEntryRow)
                    .where(and_(QueueEntryRow.queue == queue.value, QueueEntryRow.status == "pending"))
                )
                or 0
            )

    def queue_pending_keys(self, queue: QueueName, *, limit: int = 100) -> list[str]:
        with self.db.session() as s:
            rows = s.execute(
                select(QueueEntryRow.item_key)
                .where(and_(QueueEntryRow.queue == queue.value, QueueEntryRow.status == "pending"))
                .order_by(QueueEntryRow.enqueued_at.asc())
                .limit(limit)
            ).all()
            return [r[0] for r in rows]

    def queue_unflushed(self, queue: QueueName, *, limit: int = 500) -> list[QueueEntryRow]:
        """已完成但尚未 flush（交 AI 批量定论）的条目。"""
        with self.db.session() as s:
            rows = list(
                s.scalars(
                    select(QueueEntryRow)
                    .where(
                        and_(
                            QueueEntryRow.queue == queue.value,
                            QueueEntryRow.status == "done",
                            QueueEntryRow.flushed_at.is_(None),
                        )
                    )
                    .order_by(QueueEntryRow.finished_at.asc())
                    .limit(limit)
                )
            )
            for r in rows:
                s.expunge(r)
            return rows

    #: 条目被抢占为 ``running`` 后，超过这个秒数仍未收尾即视为**崩溃残留**，放回可 flush 池。
    #: 见 :meth:`claim_unflushed`。
    CLAIM_STALE_SECONDS = 3600

    def claim_unflushed(self, queue: QueueName, *, limit: int = 50) -> list[QueueEntryRow]:
        """原子地「挑出并抢占」已完成待定论的条目，返回**归本调用者**的那些。

        为什么必须抢占：「挑（``flushed_at IS NULL``）→ 调 LLM → 标记已 flush」原先
        之间没有任何互斥，两个写者会挑到同一批——daemon 的 ``_flush_loop`` 与同一进程里
        uvicorn 的 ``/queues/flush``（`fissue daemon --web`），或独立 serve 进程与 CLI。
        后果是同一批条目被送两次 LLM（重复计费）、平台标签被打两遍、``batch_conclusions``
        落两条。

        做法：把 ``status='done'`` 改成 ``'running'``，**逐行带条件更新并看 rowcount**，
        只有真正抢到那一行（此刻仍是 done 且未 flush）的调用者才认领它。整个过程在一个
        事务里，SQLite 与 PostgreSQL 的写锁语义都保证 rowcount 的判定是权威的。

        ``synchronize_session=False`` 是刻意的：不让 SQLAlchemy 把 SET 值同步回内存对象
        （那会把内存对象标脏，随后的 autoflush 会按主键无条件写回，绕过这里的条件判定）。
        """
        with self.db.session() as s:
            self._release_stale_claims(s, queue)
            rows = list(
                s.scalars(
                    select(QueueEntryRow)
                    .where(
                        and_(
                            QueueEntryRow.queue == queue.value,
                            QueueEntryRow.status == "done",
                            QueueEntryRow.flushed_at.is_(None),
                        )
                    )
                    .order_by(QueueEntryRow.finished_at.asc())
                    .limit(limit)
                )
            )
            claimed: list[QueueEntryRow] = []
            for row in rows:
                from sqlalchemy.engine import CursorResult

                res = cast(
                    CursorResult[Any],
                    s.execute(
                        update(QueueEntryRow)
                        .where(
                            and_(
                                QueueEntryRow.id == row.id,
                                QueueEntryRow.status == "done",
                                QueueEntryRow.flushed_at.is_(None),
                            )
                        )
                        .values(status="running", started_at=_now())
                        .execution_options(synchronize_session=False)
                    ),
                )
                if res.rowcount == 1:
                    claimed.append(row)
            for r in claimed:
                s.expunge(r)
            return claimed

    def finish_flush_claim(
        self,
        *,
        concluded_ids: Sequence[int] = (),
        missing_ids: Sequence[int] = (),
    ) -> None:
        """收尾一次 flush 抢占：两边都回到 ``done``，只有拿到结论的落 ``flushed_at``。

        缺结论的**不落** ``flushed_at``（下一轮 flush 还会看到它，可自愈），但必须回
        ``done``——``queue_unflushed`` 只认 ``done``，停在 ``running`` 等于把条目永久藏起来。
        handler 抛异常那种失败路径，就是 ``concluded_ids=()`` 的调用。
        """
        with self.db.session() as s:
            if concluded_ids:
                s.execute(
                    update(QueueEntryRow)
                    .where(QueueEntryRow.id.in_([int(i) for i in concluded_ids]))
                    .values(status="done", flushed_at=_now())
                )
            if missing_ids:
                s.execute(
                    update(QueueEntryRow)
                    .where(QueueEntryRow.id.in_([int(i) for i in missing_ids]))
                    .values(status="done")
                )

    def _release_stale_claims(self, s: Session, queue: QueueName) -> None:
        """把崩溃残留的 ``running`` 行放回 ``done``。

        抢占之后进程被 kill（或断电）会让行停在 ``running`` 且未落 ``flushed_at``，而 flush
        只认 ``done``——这些条目就再无人认领。超龄即放回：代价是「这一轮它被又跑了一次」，
        好过「永久卡住」。
        """
        cutoff = _now() - timedelta(seconds=self.CLAIM_STALE_SECONDS)
        s.execute(
            update(QueueEntryRow)
            .where(
                and_(
                    QueueEntryRow.queue == queue.value,
                    QueueEntryRow.status == "running",
                    QueueEntryRow.started_at.is_not(None),
                    QueueEntryRow.started_at < cutoff,
                )
            )
            .values(status="done", started_at=None)
        )

    def mark_flushed(self, entry_ids: Sequence[int]) -> None:
        if not entry_ids:
            return
        with self.db.session() as s:
            s.execute(
                update(QueueEntryRow)
                .where(QueueEntryRow.id.in_(list(entry_ids)))
                .values(flushed_at=_now())
            )

    def bump_queue_attempts(self, entry_ids: Sequence[int], *, max_attempts: int) -> list[int]:
        """给「本轮没拿到结论」的条目 +1 次尝试数，返回其中**已达上限**的 id。

        为什么要上限：某条目本轮没定论会被留在队里等下一轮 flush，这能自愈（模型偶发
        漏项、被截断）。但对模型来说恒不可定论的条目（正文极长、次次撞 max_tokens），
        无上限重试就是「每轮 flush 都白烧一次 token」。到顶后调用方把它落成显式
        ``failed``——响亮地失败，比静默丢弃或无限重试都好。
        """
        if not entry_ids:
            return []
        ids = [int(i) for i in entry_ids]
        with self.db.session() as s:
            s.execute(
                update(QueueEntryRow)
                .where(QueueEntryRow.id.in_(ids))
                .values(attempts=QueueEntryRow.attempts + 1)
            )
            rows = s.execute(
                select(QueueEntryRow.id).where(
                    and_(
                        QueueEntryRow.id.in_(ids),
                        QueueEntryRow.attempts >= max_attempts,
                    )
                )
            ).all()
            return [int(r[0]) for r in rows]

    def oldest_unflushed_age(self, queue: QueueName) -> float | None:
        """队首「完成但未 flush」条目距今秒数（用于空闲超时判定）。"""
        with self.db.session() as s:
            ts = s.scalar(
                select(func.min(QueueEntryRow.finished_at)).where(
                    and_(
                        QueueEntryRow.queue == queue.value,
                        QueueEntryRow.status == "done",
                        QueueEntryRow.flushed_at.is_(None),
                    )
                )
            )
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now() - ts).total_seconds()

    def queue_stats(self) -> dict[str, dict[str, int]]:
        with self.db.session() as s:
            rows = s.execute(
                select(QueueEntryRow.queue, QueueEntryRow.status, func.count())
                .group_by(QueueEntryRow.queue, QueueEntryRow.status)
            ).all()
        out: dict[str, dict[str, int]] = {}
        for queue, status, count in rows:
            out.setdefault(queue, {})[status] = int(count)
        return out

    def save_batch_conclusion(
        self,
        item_key: str,
        batch_id: str,
        *,
        verdict: str,
        labels: list[str],
        priority: Priority,
        reason: str,
        confidence: float,
        model: str,
    ) -> int:
        with self.db.session() as s:
            row = BatchConclusionRow(
                item_key=item_key,
                batch_id=batch_id,
                verdict=verdict,
                labels=list(labels),
                priority=priority.value,
                reason=reason,
                confidence=confidence,
                model=model,
            )
            s.add(row)
            item = s.scalar(select(ItemRow).where(ItemRow.key == item_key))
            if item is not None:
                item.priority = priority.value
                # 只在「尚未进入后续流程」时标记为已定论。
                # 批量结论可能紧接着触发修复派发（fix_queued）等更靠后的状态，
                # 这里若无条件覆盖，会把下游刚决定的状态冲掉。
                if item.status in _PRELIMINARY_STATUSES:
                    item.status = ItemStatus.LABELED.value
            s.flush()
            return int(row.id)

    def latest_batch_conclusion(self, item_key: str) -> BatchConclusion | None:
        with self.db.session() as s:
            row = s.scalar(
                select(BatchConclusionRow)
                .where(BatchConclusionRow.item_key == item_key)
                .order_by(BatchConclusionRow.id.desc())
                .limit(1)
            )
            if row is None:
                return None
            return self._conclusion_of(row)

    @staticmethod
    def _conclusion_of(row: BatchConclusionRow) -> BatchConclusion:
        return BatchConclusion(
            item_key=row.item_key,
            verdict=row.verdict or "",
            labels=list(row.labels or []),
            priority=Priority(row.priority or "none"),
            reason=row.reason or "",
            confidence=row.confidence or 0.5,
            created_at=row.created_at,
        )

    def latest_batch_conclusions(self, keys: Sequence[str]) -> dict[str, BatchConclusion]:
        """批量取「每条的最新一条」批量结论：**一条 SQL**，替代逐条调用。

        原先导出对每条条目各查一次（每次还新开一个 session）：一万条就是一万次查询，而
        ``GET /api/export?limit=10000`` 正好允许这个规模。按 ``id`` 升序扫一遍、同 key
        后者覆盖前者，留下的就是最新那条——与 :meth:`latest_batch_conclusion` 同一口径。
        """
        if not keys:
            return {}
        out: dict[str, BatchConclusion] = {}
        with self.db.session() as s:
            rows = s.scalars(
                select(BatchConclusionRow)
                .where(BatchConclusionRow.item_key.in_([str(k) for k in keys]))
                .order_by(BatchConclusionRow.item_key, BatchConclusionRow.id)
            )
            for row in rows:
                out[str(row.item_key)] = self._conclusion_of(row)
        return out

    def item_keys(
        self,
        *,
        repo_slug: str | None = None,
        status: ItemStatus | None = None,
        category: Category | None = None,
    ) -> list[str]:
        """只取 key（不构造领域对象）。

        概览算平均分只需要 key 去批量取评测——原先为此把十万条领域对象（含标签、正文元数据）
        全加载进内存。
        """
        stmt = self._item_filtered(
            select(ItemRow.key), repo_slug=repo_slug, status=status, category=category
        )
        with self.db.session() as s:
            return [str(k) for k in s.scalars(stmt)]

    def item_stats(
        self,
        *,
        repo_slug: str | None = None,
        status: ItemStatus | None = None,
        category: Category | None = None,
    ) -> dict[str, Any]:
        """概览计数走**聚合查询**（一行 GROUP BY），不把条目对象加载进内存。

        与逐条 ``Counter`` 完全等价：每条只属于一个分组，而分组把自己的 count 分别加到四个
        计数器上——「每种类型/分类/优先级/状态各多少」四组数字都由此得出。
        """
        stmt = self._item_filtered(
            select(ItemRow.item_type, ItemRow.category, ItemRow.priority, ItemRow.status, func.count()),
            repo_slug=repo_slug,
            status=status,
            category=category,
        )
        stmt = stmt.group_by(ItemRow.item_type, ItemRow.category, ItemRow.priority, ItemRow.status)
        by_type: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        by_status: dict[str, int] = {}
        total = 0
        with self.db.session() as s:
            for item_type, cat, prio, stat, count in s.execute(stmt):
                n = int(count or 0)
                total += n
                by_type[str(item_type)] = by_type.get(str(item_type), 0) + n
                by_category[str(cat)] = by_category.get(str(cat), 0) + n
                by_priority[str(prio)] = by_priority.get(str(prio), 0) + n
                by_status[str(stat)] = by_status.get(str(stat), 0) + n
        return {
            "total": total,
            "by_type": by_type,
            "by_category": by_category,
            "by_priority": by_priority,
            "by_status": by_status,
        }

    @staticmethod
    def _item_filtered(
        stmt: Select,
        *,
        repo_slug: str | None = None,
        status: ItemStatus | None = None,
        category: Category | None = None,
    ) -> Select:
        """条目查询的公共过滤（与 :meth:`list_items` 同口径）。"""
        if repo_slug:
            stmt = stmt.where(ItemRow.repo_slug == repo_slug)
        if status:
            stmt = stmt.where(ItemRow.status == status.value)
        if category:
            stmt = stmt.where(ItemRow.category == category.value)
        return stmt

    # -- 修复尝试 ---------------------------------------------------------

    def save_fix_attempt(self, attempt: FixAttempt) -> int:
        with self.db.session() as s:
            row = FixAttemptRow(
                item_key=attempt.item_key,
                outcome=attempt.outcome.value,
                branch=attempt.branch,
                fork=attempt.fork,
                pr_number=attempt.pr_number,
                pr_url=attempt.pr_url,
                diff=attempt.diff,
                patch_path=attempt.patch_path,
                changed_files=list(attempt.changed_files),
                rounds=attempt.rounds,
                error=attempt.error,
                report_path=attempt.report_path,
            )
            s.add(row)
            item = s.scalar(select(ItemRow).where(ItemRow.key == attempt.item_key))
            if item is not None and not attempt.dry_run:
                # 干跑不改状态：只出了补丁、没提 PR，条目仍应留在 fix_queued
                # 等真正修复；否则干跑一趟就把待修队列消费掉了。
                item.status = {
                    FixOutcome.SUCCESS: ItemStatus.PR_CREATED.value,
                    FixOutcome.NEEDS_MANUAL: ItemStatus.NEEDS_MANUAL.value,
                    FixOutcome.FAILED: ItemStatus.FAILED.value,
                }.get(attempt.outcome, item.status)
            s.flush()
            return int(row.id)

    def fix_attempts(self, item_key: str) -> list[FixAttempt]:
        with self.db.session() as s:
            rows = list(
                s.scalars(
                    select(FixAttemptRow)
                    .where(FixAttemptRow.item_key == item_key)
                    .order_by(FixAttemptRow.id.desc())
                )
            )
        return [
            FixAttempt(
                item_key=r.item_key,
                outcome=FixOutcome(r.outcome),
                branch=r.branch,
                fork=r.fork,
                pr_number=r.pr_number,
                pr_url=r.pr_url,
                diff=r.diff or "",
                patch_path=r.patch_path,
                changed_files=list(r.changed_files or []),
                rounds=r.rounds or 0,
                error=r.error,
                report_path=r.report_path,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # -- LLM 用量与预算 ---------------------------------------------------

    def record_usage(
        self,
        *,
        repo_key: str,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        success: bool = True,
    ) -> None:
        with self.db.session() as s:
            s.add(
                LLMUsageRow(
                    repo_key=repo_key,
                    purpose=purpose,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd,
                    success=success,
                )
            )

    def usage_today(self, *, repo_key: str | None = None) -> dict[str, float]:
        start = datetime.now(timezone.utc) - timedelta(days=1)
        stmt = select(
            func.coalesce(func.sum(LLMUsageRow.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsageRow.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsageRow.cost_usd), 0.0),
            func.count(),
        ).where(LLMUsageRow.created_at >= start)
        if repo_key:
            stmt = stmt.where(LLMUsageRow.repo_key == repo_key)
        with self.db.session() as s:
            prompt, completion, cost, calls = s.execute(stmt).one()
        return {
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
            "total_tokens": int(prompt or 0) + int(completion or 0),
            "cost_usd": float(cost or 0.0),
            "calls": int(calls or 0),
        }

    # -- 扫描记录 ---------------------------------------------------------

    def start_scan(self, repo_key: str) -> int:
        with self.db.session() as s:
            row = ScanRunRow(repo_key=repo_key)
            s.add(row)
            s.flush()
            return int(row.id)

    def finish_scan(
        self,
        scan_id: int,
        *,
        fetched: int = 0,
        created: int = 0,
        updated: int = 0,
        unchanged: int = 0,
        error: str | None = None,
    ) -> None:
        with self.db.session() as s:
            s.execute(
                update(ScanRunRow)
                .where(ScanRunRow.id == scan_id)
                .values(
                    finished_at=_now(),
                    fetched=fetched,
                    created=created,
                    updated=updated,
                    unchanged=unchanged,
                    error=error,
                )
            )

    # -- 通知 -------------------------------------------------------------

    def try_claim_notification(self, dedup_key: str, event: str, item_key: str, channel: str, payload: dict) -> bool:
        """占用一个通知位（同 dedup_key 只发一次）。返回 True 表示本次应发送。

        **只有唯一键冲突**才等于「这一条已经发过」，其余异常一律抛出。原先这里是裸
        ``except Exception: return False``，而调用方把 False 读成「已发过」并把该渠道记成
        发送成功（见 ``service.notify.Notifier.notify``）——于是一次 SQLite
        ``database is locked`` 就足以让这条告警**永久消失，还报成成功**。
        """
        from sqlalchemy.exc import IntegrityError

        with self.db.session() as s:
            row = NotificationRow(
                event=event,
                item_key=item_key,
                channel=channel,
                dedup_key=dedup_key,
                payload=payload,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                # uq_notification_dedup：这个 dedup_key 已被占过位 → 确实已发过
                s.rollback()
                return False
            return True

    def mark_notification(self, dedup_key: str, *, ok: bool, error: str | None = None) -> None:
        with self.db.session() as s:
            s.execute(
                update(NotificationRow)
                .where(NotificationRow.dedup_key == dedup_key)
                .values(ok=ok, error=error)
            )
