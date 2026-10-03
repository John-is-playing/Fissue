"""SQLAlchemy ORM 表结构（PostgreSQL）。

表设计概览
----------
``repos``            登记的仓库
``items``            Issue / PR 主表（含抓取元数据与内容指纹）
``comments``         评论
``evaluations``      AI 评测结果（四维评分 + 标签 + 反刷子）
``verifiers``        生成出来的验证器定义
``verifier_runs``    验证器每次执行结果（base / fix / merged）
``queue_entries``    队列条目（验证队列 + BUG/FEATURE 两套修复队列）
``batch_conclusions``批量 flush 后的 AI 结论
``fix_attempts``     自动修复尝试与 PR 产物
``llm_usage``        token / 成本用量（预算控制）
``scan_runs``        每次扫描记录（增量游标）
``notifications``    已发送通知（去重）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _json_col() -> Mapped[dict]:
    return mapped_column(JSON, default=dict)


class RepoRow(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)
    owner: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(511), unique=True, index=True)
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor: Mapped[dict] = _json_col()          # 增量游标（每类型的 last_updated_at）
    extra: Mapped[dict] = _json_col()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["ItemRow"]] = relationship(back_populates="repo", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("platform", "owner", "name", name="uq_repo_identity"),)


class ItemRow(Base):
    """Issue / PR 主表。"""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)
    repo_slug: Mapped[str] = mapped_column(String(511), index=True)
    number: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(String(8), index=True)     # issue | pr
    key: Mapped[str] = mapped_column(String(767), unique=True, index=True)

    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), default="open", index=True)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    author: Mapped[str] = mapped_column(String(255), default="")
    author_kind: Mapped[str] = mapped_column(String(16), default="unknown", index=True)

    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    merged: Mapped[bool] = mapped_column(Boolean, default=False)
    mergeable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    head_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    head_repo: Mapped[str | None] = mapped_column(String(511), nullable=True)
    linked_issues: Mapped[list] = mapped_column(JSON, default=list)

    additions: Mapped[int] = mapped_column(Integer, default=0)
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    changed_files: Mapped[int] = mapped_column(Integer, default=0)
    files: Mapped[list] = mapped_column(JSON, default=list)

    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    priority: Mapped[str] = mapped_column(String(16), default="none", index=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    raw: Mapped[dict] = _json_col()

    repo: Mapped[RepoRow] = relationship(back_populates="items")
    comments: Mapped[list["CommentRow"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    evaluations: Mapped[list["EvaluationRow"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    verifiers: Mapped[list["VerifierRow"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("platform", "repo_slug", "number", "item_type", name="uq_item_identity"),
        Index("ix_items_status_category", "status", "category"),
    )


class CommentRow(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    author: Mapped[str] = mapped_column(String(255), default="")
    author_kind: Mapped[str] = mapped_column(String(16), default="unknown")
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    item: Mapped[ItemRow] = relationship(back_populates="comments")


class EvaluationRow(Base):
    """一次 AI 评测（保留历史，取最新一条为当前结论）。"""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    item_key: Mapped[str] = mapped_column(String(767), index=True)

    scores: Mapped[dict] = _json_col()               # {dim: {score, reason, evidence, confidence}}
    category: Mapped[str] = mapped_column(String(16), default="unknown")
    action: Mapped[str] = mapped_column(String(24), default="triage")
    priority: Mapped[str] = mapped_column(String(16), default="none")
    labels_suggested: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    spam: Mapped[dict] = _json_col()

    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    item: Mapped[ItemRow] = relationship(back_populates="evaluations")


class VerifierRow(Base):
    __tablename__ = "verifiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    item_key: Mapped[str] = mapped_column(String(767), index=True)
    kind: Mapped[str] = mapped_column(String(16))      # executable | checklist | shell
    name: Mapped[str] = mapped_column(String(255), default="verifier")
    language: Mapped[str] = mapped_column(String(32), default="python")
    files: Mapped[dict] = _json_col()                  # 相对路径 -> 文件内容
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    checklist: Mapped[list] = mapped_column(JSON, default=list)
    expect_fail_on_base: Mapped[bool] = mapped_column(Boolean, default=True)
    expect_pass_on_fix: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=600)
    notes: Mapped[str] = mapped_column(Text, default="")
    f2p_satisfied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    generation_rounds: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    item: Mapped[ItemRow] = relationship(back_populates="verifiers")
    runs: Mapped[list["VerifierRunRow"]] = relationship(
        back_populates="verifier", cascade="all, delete-orphan"
    )


class VerifierRunRow(Base):
    __tablename__ = "verifier_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    verifier_id: Mapped[int] = mapped_column(ForeignKey("verifiers.id", ondelete="CASCADE"), index=True)
    item_key: Mapped[str] = mapped_column(String(767), index=True)
    stage: Mapped[str] = mapped_column(String(16), index=True)   # base | fix | merged
    outcome: Mapped[str] = mapped_column(String(16), index=True) # pass | fail | error | timeout | skipped
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    f2p_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    artifacts_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    verifier: Mapped[VerifierRow] = relationship(back_populates="runs")


class QueueEntryRow(Base):
    """队列条目：验证队列 / BUG 修复队列 / FEATURE 修复队列。"""

    __tablename__ = "queue_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    queue: Mapped[str] = mapped_column(String(16), index=True)   # verify | fix_bug | fix_feature
    item_key: Mapped[str] = mapped_column(String(767), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = _json_col()
    result: Mapped[dict] = _json_col()
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("queue", "item_key", "status", name="uq_queue_item_pending"),
    )


class BatchConclusionRow(Base):
    __tablename__ = "batch_conclusions"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_key: Mapped[str] = mapped_column(String(767), index=True)
    batch_id: Mapped[str] = mapped_column(String(64), index=True)
    verdict: Mapped[str] = mapped_column(String(24), default="")
    labels: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[str] = mapped_column(String(16), default="none")
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    model: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class FixAttemptRow(Base):
    __tablename__ = "fix_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_key: Mapped[str] = mapped_column(String(767), index=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fork: Mapped[str | None] = mapped_column(String(511), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str] = mapped_column(Text, default="")
    patch_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_files: Mapped[list] = mapped_column(JSON, default=list)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LLMUsageRow(Base):
    """每次 LLM 调用的用量，用于预算控制与统计。"""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_key: Mapped[str] = mapped_column(String(767), index=True, default="")
    purpose: Mapped[str] = mapped_column(String(48), index=True, default="")   # classify|evaluate|verifier|batch|fix|agent
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ScanRunRow(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_key: Mapped[str] = mapped_column(String(767), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotificationRow(Base):
    """已发送通知，用于去重与审计。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[str] = mapped_column(String(48), index=True)
    item_key: Mapped[str] = mapped_column(String(767), index=True, default="")
    channel: Mapped[str] = mapped_column(String(32), default="")
    dedup_key: Mapped[str] = mapped_column(String(255), index=True)
    payload: Mapped[dict] = _json_col()
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("dedup_key", name="uq_notification_dedup"),)
