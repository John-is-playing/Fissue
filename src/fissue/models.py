"""领域模型与枚举。

这里只放「与存储/网络中性」的领域概念；数据库 ORM 表结构见 ``fissue.store.tables``。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    """带时区的当前时间。"""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class Platform(str, Enum):
    """支持的代码托管平台。"""

    GITHUB = "github"
    GITEE = "gitee"
    ATOMGIT = "atomgit"
    GITLAB = "gitlab"

    @property
    def display(self) -> str:
        return {
            "github": "GitHub",
            "gitee": "Gitee",
            "atomgit": "AtomGit",
            "gitlab": "GitLab",
        }[self.value]


class ItemType(str, Enum):
    """抓取对象类型。"""

    ISSUE = "issue"
    PR = "pr"


class Category(str, Enum):
    """BUG 还是 FEATURE —— 决定走哪条流水线。"""

    BUG = "bug"
    FEATURE = "feature"
    UNKNOWN = "unknown"


class AuthorKind(str, Enum):
    """提交者身份（PR 流程需要先分辨）。"""

    MAINTAINER = "maintainer"      # 仓库维护者 / 核心开发者
    COMMUNITY = "community"        # 社区普通贡献者
    AI_AGENT = "ai_agent"          # 明确的 AI 代理（如带 AI 标签）
    BOT = "bot"                    # 机器人（dependabot 等）
    UNKNOWN = "unknown"


class QueueName(str, Enum):
    """队列种类：验证队列 + BUG/FEATURE 两套修复队列（共用沙盒）。"""

    VERIFY = "verify"              # Issue-BUG：验证器执行队列
    FIX_BUG = "fix_bug"            # PR-BUG：合并 + 功能验证队列
    FIX_FEATURE = "fix_feature"    # PR-FEATURE：合并 + 功能验证队列


class ItemStatus(str, Enum):
    """条目在流水线中的状态。"""

    NEW = "new"
    CLASSIFIED = "classified"
    EVALUATED = "evaluated"
    VERIFIER_READY = "verifier_ready"
    QUEUED = "queued"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    LABELED = "labeled"            # 已批量打标 + 出结论
    FIX_QUEUED = "fix_queued"
    FIXING = "fixing"
    PR_CREATED = "pr_created"
    NEEDS_MANUAL = "needs_manual"
    SKIPPED = "skipped"
    FAILED = "failed"


class VerifierKind(str, Enum):
    """验证器形态（Q7: 混合）。"""

    EXECUTABLE = "executable"      # 可执行测试（pytest/jest/go test…）
    CHECKLIST = "checklist"        # 自然语言清单，交 LLM 判定
    SHELL = "shell"                # 自定义脚本，退出码判定

    @property
    def is_executable(self) -> bool:
        """是否属于「以退出码判定通过/失败」的可执行形态。"""
        return self in (VerifierKind.EXECUTABLE, VerifierKind.SHELL)


class VerifierOutcome(str, Enum):
    """验证器单次执行结果。"""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"                # 环境/依赖问题，非断言失败
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class FixOutcome(str, Enum):
    """自动修复结果。"""

    SUCCESS = "success"
    FAILED = "failed"
    NEEDS_MANUAL = "needs_manual"
    SKIPPED = "skipped"


class Priority(str, Enum):
    """自动修复准入优先级（Q1 的规则）。"""

    TIER1 = "tier1"                # 难度低 + 重要性高
    TIER2 = "tier2"                # 难度低 + 重要性低
    NONE = "none"                  # 不修复，等开发者


class Action(str, Enum):
    """建议的处理动作。"""

    FIX_NOW = "fix_now"
    TRIAGE = "triage"
    ANSWER = "answer"
    BACKLOG = "backlog"
    CLOSE = "close"
    NEEDS_INFO = "needs_info"


# ---------------------------------------------------------------------------
# 抓取到的原始条目
# ---------------------------------------------------------------------------


class RepoRef(BaseModel):
    """一个仓库引用。"""

    model_config = ConfigDict(frozen=True)

    platform: Platform
    owner: str
    name: str

    def __str__(self) -> str:
        return f"{self.platform.value}:{self.owner}/{self.name}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


class Comment(BaseModel):
    """评论。"""

    author: str
    author_kind: AuthorKind = AuthorKind.UNKNOWN
    body: str = ""
    created_at: datetime | None = None


class FileChange(BaseModel):
    """PR 中的单个文件变更（轻量摘要）。"""

    path: str
    status: str = "modified"       # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0
    patch: str | None = None       # 需要评代码质量时才拉取（Q1 分层）


class RawItem(BaseModel):
    """从平台抓取到的 Issue / PR 统一表示。"""

    platform: Platform
    repo: str                      # "owner/name"
    number: int
    item_type: ItemType
    title: str
    body: str = ""
    state: str = "open"
    labels: list[str] = Field(default_factory=list)
    author: str = ""
    author_kind: AuthorKind = AuthorKind.UNKNOWN
    comments: list[Comment] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    is_draft: bool = False
    merged: bool = False
    mergeable: bool | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    head_repo: str | None = None   # fork 的 owner/name
    linked_issues: list[int] = Field(default_factory=list)
    files: list[FileChange] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    # 流水线状态（与数据库 items 表对应，便于仓储查询结果直接携带结论）
    category: Category = Category.UNKNOWN
    status: ItemStatus = ItemStatus.NEW
    priority: Priority = Priority.NONE
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """全局唯一键：platform:owner/name#number。"""
        return f"{self.platform.value}:{self.repo}#{self.number}"

    def content_hash(self) -> str:
        """内容指纹，用于增量判断与缓存（标题/正文/标签变了才算更新）。"""
        payload = "\x1f".join(
            [
                self.title or "",
                self.body or "",
                ",".join(sorted(self.labels)),
                str(len(self.comments)),
                "1" if self.merged else "0",
                self.state or "",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# AI 评测结果
# ---------------------------------------------------------------------------


class DimensionScore(BaseModel):
    """单个维度的评分与理由。"""

    score: int = Field(ge=0, le=100, description="0-100")
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class Scores(BaseModel):
    """四维评分。"""

    authenticity: DimensionScore | None = None   # 真实性
    importance: DimensionScore | None = None     # 重要性
    feasibility: DimensionScore | None = None    # 可行性
    pr_quality: DimensionScore | None = None     # PR 质量（仅 PR）
    difficulty: DimensionScore | None = None     # 修复难度（Issue-BUG 用）

    def get(self, name: str) -> DimensionScore | None:
        return getattr(self, name, None)

    def as_dict(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for name in ("authenticity", "importance", "feasibility", "pr_quality", "difficulty"):
            dim = self.get(name)
            if dim is not None:
                out[name] = dim.score
        return out


class SpamSignal(BaseModel):
    """反刷子 / 误报信号。"""

    is_spam: bool = False
    is_duplicate: bool = False
    is_ai_generated: bool = False
    duplicate_of: list[int] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return self.is_spam or self.is_duplicate


class Evaluation(BaseModel):
    """一次完整的 AI 评测结果。"""

    scores: Scores = Field(default_factory=Scores)
    category: Category = Category.UNKNOWN
    labels_suggested: list[str] = Field(default_factory=list)
    action: Action = Action.TRIAGE
    summary: str = ""
    spam: SpamSignal = Field(default_factory=SpamSignal)
    priority: Priority = Priority.NONE
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)

    # 便捷访问
    @property
    def importance(self) -> int:
        return self.scores.importance.score if self.scores.importance else 0

    @property
    def difficulty(self) -> int:
        return self.scores.difficulty.score if self.scores.difficulty else 100

    @property
    def authenticity(self) -> int:
        return self.scores.authenticity.score if self.scores.authenticity else 0


# ---------------------------------------------------------------------------
# 验证器
# ---------------------------------------------------------------------------


class VerifierSpec(BaseModel):
    """验证器定义。"""

    kind: VerifierKind
    name: str = "verifier"
    language: str = "python"
    # 写入沙盒测试盘的相对路径
    files: dict[str, str] = Field(default_factory=dict)
    # 在沙盒内执行的命令（kind=executable/shell 时使用）
    command: str | None = None
    # kind=checklist 时的自然语言判定项
    checklist: list[str] = Field(default_factory=list)
    # 预期：F2P 要求 base 失败、修复后通过
    expect_fail_on_base: bool = True
    expect_pass_on_fix: bool = True
    timeout_seconds: int = 600
    notes: str = ""

    @property
    def is_executable(self) -> bool:
        return self.kind in (VerifierKind.EXECUTABLE, VerifierKind.SHELL)


class VerifierRun(BaseModel):
    """验证器一次执行的结果。"""

    verifier_id: int | None = None
    item_key: str
    stage: str                     # base | fix | merged
    outcome: VerifierOutcome
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    f2p_ok: bool | None = None     # 是否满足 fail-to-pass
    artifacts_dir: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class VerifierResult(BaseModel):
    """F2P 总判定：base 失败 + 修复后通过。"""

    kind: VerifierKind
    base_run: VerifierRun | None = None
    fix_run: VerifierRun | None = None
    f2p_satisfied: bool = False
    conclusion: str = ""

    @property
    def reproducible(self) -> bool:
        """base 阶段确实失败 → 问题可复现。"""
        return bool(self.base_run and self.base_run.outcome is VerifierOutcome.FAIL)

    @property
    def fixed(self) -> bool:
        """修复阶段确实通过 → 修复有效。"""
        return bool(self.fix_run and self.fix_run.outcome is VerifierOutcome.PASS)


# ---------------------------------------------------------------------------
# 自动修复
# ---------------------------------------------------------------------------


class FixAttempt(BaseModel):
    """一次自动修复尝试。"""

    item_key: str
    outcome: FixOutcome = FixOutcome.SKIPPED
    branch: str | None = None
    fork: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    diff: str = ""
    patch_path: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    rounds: int = 0
    error: str | None = None
    report_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# 批量打标结论（队列 flush 的产物）
# ---------------------------------------------------------------------------

class BatchConclusion(BaseModel):
    """一批条目交给 AI 后的整体结论。"""

    item_key: str
    verdict: str = ""                       # merge | reject | needs_review | fix | skip
    labels: list[str] = Field(default_factory=list)
    priority: Priority = Priority.NONE
    reason: str = ""
    confidence: float = 0.5
    created_at: datetime = Field(default_factory=utcnow)
