"""仓储层：把领域模型与 ORM 表互相转换，封装常用查询。

对外只暴露语义化方法（``upsert_item``、``latest_evaluation``、``queue_pending`` …），
让流水线代码不直接碰 SQL。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import Select, and_, delete, func, select, update
from sqlalchemy.orm import Session, selectinload

from ..logging_setup import get_logger
from ..models import (
    AuthorKind,
    BatchConclusion,
    Category,
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
            {
                "author": c.author,
                "author_kind": AuthorKind(c.author_kind or "unknown"),
                "body": c.body or "",
                "created_at": c.created_at,
            }
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

        :return: ``(item_id, created, changed)`` —— changed 表示内容指纹变化（需要重新评测）。
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
            row.raw = item.raw or {}
            row.fetched_at = _now()
            if created:
                row.category = Category.UNKNOWN.value
            s.flush()
            item_id = int(row.id)

            # 评论：整体替换（简单可靠，评论量不大）
            if changed:
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
        """入队（同队列同条目的 pending 去重）。"""
        with self.db.session() as s:
            existing = s.scalar(
                select(QueueEntryRow).where(
                    and_(
                        QueueEntryRow.queue == queue.value,
                        QueueEntryRow.item_key == item_key,
                        QueueEntryRow.status.in_(["pending", "running"]),
                    )
                )
            )
            if existing is not None:
                return int(existing.id)
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
            if self.db.is_sqlite:
                stmt = stmt.with_for_update()
            else:
                stmt = stmt.with_for_update(skip_locked=True)
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

    def mark_flushed(self, entry_ids: Sequence[int]) -> None:
        if not entry_ids:
            return
        with self.db.session() as s:
            s.execute(
                update(QueueEntryRow)
                .where(QueueEntryRow.id.in_(list(entry_ids)))
                .values(flushed_at=_now())
            )

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
            return BatchConclusion(
                item_key=row.item_key,
                verdict=row.verdict or "",
                labels=list(row.labels or []),
                priority=Priority(row.priority or "none"),
                reason=row.reason or "",
                confidence=row.confidence or 0.5,
                created_at=row.created_at,
            )

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
            if item is not None:
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
        """占用一个通知位（同 dedup_key 只发一次）。返回 True 表示本次应发送。"""
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
            except Exception:
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
