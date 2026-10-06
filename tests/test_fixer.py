"""自动修复 Agent 测试：策略闸门、写入控制、失败降级报告。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fissue.config import load_settings
from fissue.fixer.agent import FixAgent, AgentTrace, branch_name
from fissue.fixer.autofix import AutoFixer, FixReport, PRCreator
from fissue.models import (
    Category,
    DimensionScore,
    Evaluation,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    RawItem,
    RepoRef,
    Scores,
    VerifierKind,
    VerifierSpec,
)
from fissue.workspace import RepoWorkspace


@pytest.fixture
def workspace(tmp_path: Path) -> RepoWorkspace:
    """一个真实的临时 git 仓库。"""
    root = tmp_path / "repo"
    root.mkdir()
    env = {"GIT_TERMINAL_PROMPT": "0"}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env={**__import__("os").environ, **env})

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    return RepoWorkspace(root=root, slug="a/b")


def _agent(settings) -> FixAgent:
    return FixAgent(None, None, None, settings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Agent：策略与写入控制
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,protected",
    [
        (".github/workflows/ci.yml", True),
        (".github/ISSUE_TEMPLATE/bug.md", True),
        (".gitlab/ci.yml", True),
        ("LICENSE", True),
        ("poetry.lock", True),
        ("app.py", False),
        ("src/main.py", False),
        ("tests/test_new.py", False),
        ("docs/LICENSE.md", False),
    ],
)
def test_protected_paths(settings, path: str, protected: bool) -> None:
    assert _agent(settings)._is_protected(path) is protected


def test_verifier_path_lock(settings) -> None:
    agent = _agent(settings)
    locked = {"tests/test_fissue_verifier.py"}
    assert agent._is_verifier_path("tests/test_fissue_verifier.py", locked) is True
    assert agent._is_verifier_path("tests/other.py", locked) is True     # 同目录也算
    assert agent._is_verifier_path("src/app.py", locked) is False


def test_apply_writes_happy_path(settings, workspace) -> None:
    agent = _agent(settings)
    trace = AgentTrace()
    written, err = agent._apply_writes(workspace, {"app.py": "def f():\n    return 2\n"}, locked=set(), trace=trace)
    assert err is None and written == ["app.py"]
    assert workspace.read("app.py").endswith("return 2\n")
    assert agent._diff_lines(workspace) > 0


def test_apply_writes_rejects_protected_and_verifier(settings, workspace) -> None:
    agent = _agent(settings)
    trace = AgentTrace()
    written, err = agent._apply_writes(
        workspace,
        {".github/ci.yml": "x", "tests/test_fissue_verifier.py": "y"},
        locked={"tests/test_fissue_verifier.py"},
        trace=trace,
    )
    assert written == []
    assert err is not None
    assert len(trace.rejected) == 2


def test_apply_writes_rejects_escape(settings, workspace) -> None:
    agent = _agent(settings)
    trace = AgentTrace()
    written, err = agent._apply_writes(workspace, {"../evil.py": "x"}, locked=set(), trace=trace)
    assert written == [] and err is not None


def test_apply_writes_enforces_max_changed_files(settings, workspace) -> None:
    s = settings.model_copy(deep=True)
    s.auto_fix.max_changed_files = 1
    agent = _agent(s)
    trace = AgentTrace()
    written, err = agent._apply_writes(workspace, {"n1.txt": "a", "n2.txt": "b"}, locked=set(), trace=trace)
    assert written == []
    assert "超过上限" in (err or "")


def test_apply_writes_enforces_max_diff_lines(settings, workspace) -> None:
    s = settings.model_copy(deep=True)
    s.auto_fix.max_diff_lines = 5
    agent = _agent(s)
    trace = AgentTrace()
    written, err = agent._apply_writes(workspace, {"huge.txt": "x\n" * 200}, locked=set(), trace=trace)
    assert written == []
    assert "diff 行数" in (err or "")


def test_apply_writes_rejects_non_string_content(settings, workspace) -> None:
    agent = _agent(settings)
    trace = AgentTrace()
    written, err = agent._apply_writes(workspace, {"a.txt": 123}, locked=set(), trace=trace)
    assert written == []


def test_last_passed_detection(settings) -> None:
    agent = _agent(settings)
    trace = AgentTrace(test_outputs=["[exit=1 outcome=fail]\nx", "[exit=0 outcome=pass]\nok"])
    assert agent._last_passed(trace) is True
    trace.test_outputs = ["[exit=1 outcome=fail]\nx"]
    assert agent._last_passed(trace) is False
    assert agent._last_passed(AgentTrace()) is False


def test_branch_name_is_unique_and_readable() -> None:
    item = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=42, item_type=ItemType.ISSUE, title="t")
    b1 = branch_name("fissue/fix-", item, salt="1")
    b2 = branch_name("fissue/fix-", item, salt="2")
    assert b1.startswith("fissue/fix-42-requests-")
    assert b1 != b2


def test_finalize_collects_diff_and_changed_files(settings, workspace) -> None:
    agent = _agent(settings)
    agent._apply_writes(workspace, {"app.py": "def f():\n    return 9\n"}, locked=set(), trace=AgentTrace())
    attempt, trace = agent._finalize(__import__("fissue.models", fromlist=["FixAttempt"]).FixAttempt(item_key="k"),
                                     AgentTrace(rounds=2), workspace)
    assert "app.py" in attempt.diff
    assert attempt.changed_files == ["app.py"]
    assert attempt.rounds == 2


async def test_agent_disabled_returns_skipped(settings, workspace, sample_issue) -> None:
    s = settings.model_copy(deep=True)
    s.auto_fix.enabled = False
    agent = _agent(s)
    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace,
        spec=VerifierSpec(kind=VerifierKind.EXECUTABLE, command="pytest"),
        repo_context="ctx",
    )
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "未启用" in trace.summary


# ---------------------------------------------------------------------------
# 策略闸门（AutoFixer.fix_item）
# ---------------------------------------------------------------------------


@pytest.fixture
def fixer_ctx(settings, repo):
    """构造一个只够跑策略闸门的 AutoFixer 上下文。"""
    import asyncio

    from fissue.ai.client import LLMClient
    from fissue.ai.evaluator import Evaluator
    from fissue.pipeline.context import RuntimeContext
    from fissue.sandbox.forwarder import SandboxManager
    from fissue.verifier.generator import VerifierGenerator
    from fissue.verifier.regression import RegressionGate
    from fissue.verifier.runner import VerifierRunner

    c = RuntimeContext.__new__(RuntimeContext)
    object.__setattr__(c, "settings", settings)
    object.__setattr__(c, "db", repo.db)
    object.__setattr__(c, "repo", repo)
    object.__setattr__(c, "_adapters", {})
    object.__setattr__(c, "_lock", asyncio.Lock())
    llm = LLMClient(settings.llm)
    object.__setattr__(c, "llm", llm)
    object.__setattr__(c, "sandbox", SandboxManager(settings.sandbox, force_inline=True))
    object.__setattr__(c, "evaluator", Evaluator(llm, repo, settings))
    object.__setattr__(c, "generator", VerifierGenerator(llm, settings))
    object.__setattr__(c, "verifier", VerifierRunner(c.sandbox, repo, settings, client=llm))
    object.__setattr__(c, "regression_gate", RegressionGate(c.sandbox, repo, settings))
    return c


def _register(repo, item: RawItem) -> None:
    rid = repo.ensure_repo(RepoRef(platform=item.platform, owner="psf", name="requests"))
    repo.upsert_item(rid, item)


async def test_fix_item_skips_without_evaluation(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    attempt = await AutoFixer(fixer_ctx).fix_item(sample_issue)
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "评测结果" in attempt.error


async def test_fix_item_skips_non_tier_priority(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.NONE, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.EVALUATED, priority=Priority.NONE)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "优先级" in attempt.error


async def test_fix_item_skips_suspicious_spam(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    from fissue.models import SpamSignal

    ev = Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s", spam=SpamSignal(is_spam=True))
    repo.save_evaluation(sample_issue.key, ev)
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "刷量" in attempt.error


async def test_fix_item_skips_without_verifier(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "验证器" in attempt.error


async def test_fix_item_skips_checklist_verifier(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.CHECKLIST, checklist=["能跑通"]))

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "清单型" in attempt.error


async def test_fix_item_skips_when_attempt_exists(fixer_ctx, repo, sample_issue) -> None:
    from fissue.models import FixAttempt

    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.EXECUTABLE, command="pytest"))
    repo.save_fix_attempt(FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS, pr_url="u"))

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "重复" in attempt.error


async def test_fix_item_retries_after_needs_manual(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """NEEDS_MANUAL 不该把条目永久封禁，允许重试。

    回归：_has_open_attempt 曾把 NEEDS_MANUAL 也当作「已存在修复尝试」，
    于是 #1 一条陈旧的失败记录（来源还是「Agent 空转到轮次上限」那个 bug）
    就成了永久路障，之后每次 fix 都被 skip。

    这里把 clone_workspace 换成哨兵异常：只为证明流程**越过了闸门**，
    绝不去真的 clone 远端（子进程不受测试网络守卫保护，会挂死）。
    """
    from fissue.models import FixAttempt

    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.EXECUTABLE, command="pytest"))
    repo.save_fix_attempt(FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.NEEDS_MANUAL,
                                     error="达到最大轮次（12）仍未通过验证器"))

    async def _stop(repo_cfg, **kwargs):
        raise RuntimeError("已越过闸门（哨兵）")

    monkeypatch.setattr(fixer_ctx, "clone_workspace", _stop)

    fixer = AutoFixer(fixer_ctx)
    assert fixer._has_open_attempt(sample_issue.key) is False        # 不该被拦住

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key))
    # 没有被 skip，而是走到了克隆那一步 —— 证明确实在重试
    assert attempt.outcome is not FixOutcome.SKIPPED
    assert "重复" not in (attempt.error or "")
    assert "已越过闸门" in (attempt.error or "")


def test_has_open_attempt_only_blocks_success(repo, sample_issue) -> None:
    """只有 SUCCESS 才算「已提过 PR」；其余终态一律放行重试。"""
    from fissue.models import FixAttempt

    _register(repo, sample_issue)
    fixer = AutoFixer.__new__(AutoFixer)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.repo = repo
    fixer.ctx = ctx

    for outcome, blocked in [
        (FixOutcome.NEEDS_MANUAL, False),
        (FixOutcome.FAILED, False),
        (FixOutcome.SKIPPED, False),
    ]:
        repo.db.drop_all()
        repo.db.create_all()
        _register(repo, sample_issue)
        repo.save_fix_attempt(FixAttempt(item_key=sample_issue.key, outcome=outcome))
        assert fixer._has_open_attempt(sample_issue.key) is blocked, outcome

    repo.save_fix_attempt(FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS, pr_url="u"))
    assert fixer._has_open_attempt(sample_issue.key) is True


# ---------------------------------------------------------------------------
# 修复报告统计（FixReport）
# ---------------------------------------------------------------------------


def test_fix_report_does_not_count_dry_run_as_failure() -> None:
    """dry-run 下「未提 PR」是设计如此，补丁已产出即算成功，不得计入失败。

    回归：报告把非 SUCCESS 一律算失败，于是 demo 实跑 dry-run 输出
    「成功 0，失败 2」——而其中 #2 的修复其实是完整的（验证器全过、补丁正确）。
    """
    from fissue.fixer.autofix import FixReport
    from fissue.models import FixAttempt

    def att(outcome, pr=None, patch=None):
        return FixAttempt(item_key="k", outcome=outcome, pr_url=pr, patch_path=patch)

    # dry-run：修复成功但只产补丁不提 PR → 计入成功
    r = FixReport()
    r.attempted = 2
    r.record(att(FixOutcome.NEEDS_MANUAL, patch="/p.patch"), dry_run=True)
    r.record(att(FixOutcome.SKIPPED), dry_run=True)
    assert r.succeeded == 1
    assert r.skipped == 1
    assert r.failed == 0
    assert "成功 1" in r.summary and "失败 0" in r.summary

    # 非 dry-run：NEEDS_MANUAL 是真失败，不冒充成功
    r2 = FixReport()
    r2.attempted = 1
    r2.record(att(FixOutcome.NEEDS_MANUAL, patch="/p.patch"), dry_run=False)
    assert r2.succeeded == 0
    assert r2.needs_manual == 1
    assert r2.failed == 0


def test_fix_report_counts_all_outcomes_separately() -> None:
    """四类终态各自计数，不再把 skipped / needs_manual 混进 failed。"""
    from fissue.fixer.autofix import FixReport
    from fissue.models import FixAttempt

    r = FixReport()
    r.attempted = 4
    r.record(FixAttempt(item_key="a", outcome=FixOutcome.SUCCESS, pr_url="http://x/1"))
    r.record(FixAttempt(item_key="b", outcome=FixOutcome.NEEDS_MANUAL, error="F2P 未通过"))
    r.record(FixAttempt(item_key="c", outcome=FixOutcome.SKIPPED, error="优先级不足"))
    r.record(FixAttempt(item_key="d", outcome=FixOutcome.FAILED, error="异常"))
    assert (r.succeeded, r.needs_manual, r.skipped, r.failed) == (1, 1, 1, 1)
    for frag in ("成功 1", "需人工 1", "跳过 1", "失败 1"):
        assert frag in r.summary


def test_fix_report_dry_run_patch_counted_and_summary_labeled() -> None:
    """dry-run 产出补丁的条数要单独可见，汇总里点明口径。

    回归（ratekit 实测）：dry-run 6 条全产出补丁，汇总显示「成功 6，需人工 0」，
    同一屏的明细却全写 needs_manual，读者无法判断到底成没成。
    """
    from fissue.fixer.autofix import FixReport
    from fissue.models import FixAttempt

    r = FixReport()
    r.attempted = 6
    for i in range(6):
        r.record(
            FixAttempt(item_key=f"k{i}", outcome=FixOutcome.NEEDS_MANUAL,
                       patch_path=f"/p{i}.patch"),
            dry_run=True,
        )
    assert r.succeeded == 6 and r.dry_run_patches == 6
    assert r.needs_manual == 0
    assert "dry-run 产出补丁 6" in r.summary


def test_attempt_row_matches_summary_under_dry_run() -> None:
    """明细与汇总必须同一口径：dry-run 补丁已产出 → 明细也显示成功态。

    回归：汇总计入「成功」，明细却原样输出 needs_manual，自相矛盾。
    非 dry-run 时不得把真失败(needs_manual)冒充成成功。
    """
    from fissue.cli.ops import _attempt_row
    from fissue.models import FixAttempt

    a = FixAttempt(item_key="k", outcome=FixOutcome.NEEDS_MANUAL, patch_path="/p.patch",
                   error="dry-run：仅产出补丁，未提交 PR")

    dry = _attempt_row(a, dry_run=True)
    assert dry["outcome"] == "success"
    assert dry["pr"] == "/p.patch"                 # 补丁路径照常给出，便于核对

    real = _attempt_row(a, dry_run=False)
    assert real["outcome"] == "needs_manual"       # 非 dry-run 不冒充成功


# ---------------------------------------------------------------------------
# 失败降级报告
# ---------------------------------------------------------------------------


def test_write_manual_report_creates_files(fixer_ctx, repo, sample_issue, workspace, tmp_path) -> None:
    _register(repo, sample_issue)
    fixer = AutoFixer(fixer_ctx)
    attempt = __import__("fissue.models", fromlist=["FixAttempt"]).FixAttempt(
        item_key=sample_issue.key, outcome=FixOutcome.NEEDS_MANUAL, error="达到最大轮次"
    )
    trace = AgentTrace(rounds=3, actions=["r1: read", "r2: write"], written_files=["app.py"],
                       rejected=["a: 命中保护路径"], test_outputs=["[exit=1 outcome=fail]"])
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="v", command="pytest")

    path = fixer._write_manual_report(sample_issue, attempt, trace, spec)
    p = Path(path)
    assert p.exists() and p.name == "report.md"
    content = p.read_text(encoding="utf-8")
    assert "需人工处理" in content
    assert "达到最大轮次" in content
    assert (p.parent / "trace.json").exists()


async def test_handle_failure_discard_mode(fixer_ctx, repo, sample_issue) -> None:
    s = fixer_ctx.settings.model_copy(deep=True)
    s.auto_fix.on_failure = "discard"
    ctx = fixer_ctx
    object.__setattr__(ctx, "settings", s)
    _register(repo, sample_issue)
    fixer = AutoFixer(ctx)
    attempt = __import__("fissue.models", fromlist=["FixAttempt"]).FixAttempt(
        item_key=sample_issue.key, outcome=FixOutcome.FAILED
    )
    out = await fixer._handle_failure(sample_issue, attempt, AgentTrace(), VerifierSpec(kind=VerifierKind.EXECUTABLE))
    assert out.outcome is FixOutcome.FAILED
    assert out.report_path is None


async def test_fix_candidates_report_shape(fixer_ctx, repo, monkeypatch) -> None:
    fixer = AutoFixer(fixer_ctx)

    async def fake_fix(item, *, dry_run=False, max_rounds=None):
        from fissue.models import FixAttempt

        return FixAttempt(item_key=item.key, outcome=FixOutcome.SUCCESS,
                          pr_url="https://example/pr/1", branch="b", rounds=2)

    monkeypatch.setattr(fixer, "fix_item", fake_fix)
    report = await fixer.fix_candidates(limit=3)
    assert isinstance(report, FixReport)
    assert report.attempted == report.succeeded == 0     # 无候选
    assert report.summary


def test_save_patch_writes_file(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    creator = PRCreator(fixer_ctx)
    path = creator._save_patch(sample_issue, "diff --git a/x b/x\n")
    assert Path(path).exists()
    assert Path(path).read_text(encoding="utf-8").startswith("diff --git")


def test_template_body_mentions_ai_and_f2p(fixer_ctx, sample_issue) -> None:
    creator = PRCreator(fixer_ctx)
    body = creator._template_body(
        sample_issue,
        trace=AgentTrace(written_files=["app.py"], summary="加了判空"),
        spec=VerifierSpec(kind=VerifierKind.EXECUTABLE, name="v", command="pytest -q"),
        f2p_ok=True,
    )
    assert "Fissue" in body and "fail-to-pass" in body and "✅ 通过" in body
    assert "app.py" in body


def test_decorate_body_ensures_ai_marker(fixer_ctx, sample_issue) -> None:
    creator = PRCreator(fixer_ctx)
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="v", command="pytest")
    decorated = creator._decorate_body("普通正文", sample_issue, spec, f2p_ok=False)
    assert "Fissue" in decorated and "未通过" in decorated
    # 已含 Fissue 时不再重复追加
    assert creator._decorate_body("含 Fissue 的正文", sample_issue, spec, f2p_ok=True).count("Fissue") == 1


# ---------------------------------------------------------------------------
# Agent 循环：验证器通过即收工（不空转到轮次上限）
# ---------------------------------------------------------------------------


class _ScriptLLM:
    """按脚本逐步应答的 LLM，用于驱动 Agent 循环。"""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.calls = 0

    async def chat_json(self, messages, *, purpose: str = "", **kwargs):
        from fissue.ai.client import Usage

        self.calls += 1
        data = self.steps.pop(0) if self.steps else {}
        return data, Usage(purpose="agent", model="scripted", prompt_tokens=10,
                           completion_tokens=5, cost_usd=0.0)

    async def aclose(self) -> None:
        return None


class _StubRunner:
    """固定的验证器执行结果。"""

    def __init__(self, outcome) -> None:
        from fissue.models import VerifierOutcome

        self.outcome = outcome or VerifierOutcome.PASS
        self.runs = 0

    async def run_once(self, **kwargs):
        from fissue.models import VerifierOutcome, VerifierRun

        self.runs += 1
        passed = self.outcome is VerifierOutcome.PASS
        return VerifierRun(
            item_key=kwargs.get("item_key", "k"),
            stage="agent",
            outcome=self.outcome,
            exit_code=0 if passed else 1,
            stdout="ok" if passed else "boom",
        )


def _agent_with(settings, steps, outcome=None):
    from fissue.models import VerifierOutcome

    llm = _ScriptLLM(steps)
    runner = _StubRunner(outcome or VerifierOutcome.PASS)
    return FixAgent(llm, None, runner, settings), llm, runner  # type: ignore[arg-type]


_SPEC = VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q")


async def test_agent_stops_when_write_makes_verifier_pass(settings, workspace, sample_issue) -> None:
    """write 之后验证器通过 → 立即 SUCCESS，不空转到轮次上限。

    回归：demo 实测 #1 —— 模型 11 次写入 + 11 次验证器**全部 pass**，却因
    write/run 分支从不检查结果、只等模型额外回一次 done，一路空转到 12 轮上限，
    最后报出与事实相反的「仍未通过验证器」。
    """
    agent, llm, runner = _agent_with(settings, [
        {"action": "write", "files": {"app.py": "def f():\n    return 2\n"}},
        {"action": "write", "files": {"app.py": "def f():\n    return 3\n"}},   # 不该被消费
    ])
    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=12,
    )
    assert attempt.outcome is FixOutcome.SUCCESS
    assert attempt.rounds == 1
    assert llm.calls == 1                      # 只问了一轮，没有空转
    assert runner.runs == 1


async def test_agent_stops_when_run_passes(settings, workspace, sample_issue) -> None:
    """run 之后验证器通过 → 同样立即 SUCCESS。"""
    agent, llm, _ = _agent_with(settings, [{"action": "run"}, {"action": "run"}])
    attempt, _trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=12,
    )
    assert attempt.outcome is FixOutcome.SUCCESS
    assert llm.calls == 1


async def test_agent_keeps_looping_while_verifier_fails(settings, workspace, sample_issue) -> None:
    """验证器未通过时继续循环，耗尽轮次才算失败（不能误判成功）。"""
    from fissue.models import VerifierOutcome

    agent, llm, runner = _agent_with(
        settings,
        [{"action": "write", "files": {"app.py": "def f():\n    return 9\n"}}] * 3,
        outcome=VerifierOutcome.FAIL,
    )
    attempt, _trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=3,
    )
    assert attempt.outcome is FixOutcome.FAILED
    assert llm.calls == 3
    assert runner.runs == 3
    assert "仍未通过验证器" in (attempt.error or "")


async def test_agent_requires_done_when_no_verifier_run(settings, workspace, sample_issue) -> None:
    """模型只读不写、没跑过验证器 → 不得判成功。"""
    agent, _llm, runner = _agent_with(settings, [{"action": "read", "paths": ["app.py"]}] * 2)
    attempt, _trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=2,
    )
    assert attempt.outcome is FixOutcome.FAILED
    assert runner.runs == 0


# ---------------------------------------------------------------------------
# 编码：git 输出必须显式按 UTF-8 解码
# ---------------------------------------------------------------------------


def test_git_decodes_output_as_utf8_explicitly(settings, workspace, monkeypatch) -> None:
    """``_git`` 必须显式传 encoding="utf-8"。

    回归：Windows 中文环境默认按 GBK 解码子进程输出，git 的中文会变成乱码
    （demo 实测补丁里 '空字符串应返回 0' 变成 '绌哄瓧绗︿覆搴旇繑鍥� 0'）。
    该问题只在 PowerShell（cp936）下复现，Git Bash 里因 PYTHONUTF8=1 看不出来，
    所以这里直接锁「显式指定编码」这个契约，与运行环境的 locale 无关。
    """
    seen: dict = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    workspace._git(["git", "rev-parse", "HEAD"])
    assert seen.get("encoding") == "utf-8"


def test_git_roundtrips_chinese_text(settings, workspace) -> None:
    """中文经 git 输出往返后不损坏。"""
    workspace._git(["git", "config", "user.email", "t@t"])
    (workspace.root / "cn.txt").write_text("空字符串应返回 0\n", encoding="utf-8")
    workspace._git(["git", "add", "-A"])
    workspace._git(["git", "commit", "-qm", "修复：空字符串应返回 0"])
    out = workspace._git(["git", "log", "-1", "--format=%s"]).stdout
    assert "空字符串应返回 0" in out
    assert "绌哄瓧绗" not in out


def test_save_patch_preserves_chinese(fixer_ctx, repo, sample_issue) -> None:
    """补丁里的中文必须以 UTF-8 原样落盘（不是乱码）。"""
    _register(repo, sample_issue)
    creator = PRCreator(fixer_ctx)
    diff = 'diff --git a/x b/x\n+    """空字符串应返回 0"""\n'
    path = creator._save_patch(sample_issue, diff)
    text = Path(path).read_text(encoding="utf-8")
    assert "空字符串应返回 0" in text
    assert "绌哄瓧绗" not in text


def test_save_patch_uses_pure_lf(fixer_ctx, repo, sample_issue) -> None:
    """补丁必须纯 LF 落盘。

    回归：``write_text`` 在 Windows 上把 \\n 转成 \\r\\n，补丁每行多一个 CR，
    git apply 的上下文行匹配不上 → ``patch does not apply``。
    实测 dry-run 产出的补丁有 13 个 CR 字节，打到干净克隆上直接失败。
    """
    _register(repo, sample_issue)
    creator = PRCreator(fixer_ctx)

    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    path = creator._save_patch(sample_issue, diff)
    raw = Path(path).read_bytes()
    assert b"\r" not in raw, "补丁里不该有 CR"
    assert raw.decode("utf-8") == diff

    # 输入本身带 CRLF 时也要归一，避免上游把 CR 带进来
    crlf_path = creator._save_patch(sample_issue, diff.replace("\n", "\r\n"))
    assert b"\r" not in Path(crlf_path).read_bytes()


def test_saved_patch_applies_cleanly_to_repo(fixer_ctx, repo, sample_issue, workspace) -> None:
    """产出的补丁能被 git apply 干净应用——这是补丁的**唯一用途**。

    只断言「文件里有 CR」不够，这里直接拿一个真实 git 仓库真打一次：
    这正是 dry-run 产物此前失效的方式（crbug 报 patch does not apply）。
    """
    _register(repo, sample_issue)
    creator = PRCreator(fixer_ctx)

    # workspace 是真 git 仓库，app.py 原本是 "def f():\n    return 1\n"
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    path = creator._save_patch(sample_issue, diff)

    check = subprocess.run(
        ["git", "apply", "--check", path], cwd=workspace.root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert check.returncode == 0, f"补丁打不上：{check.stderr}"

    applied = subprocess.run(
        ["git", "apply", path], cwd=workspace.root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert applied.returncode == 0, f"应用失败：{applied.stderr}"
    assert "return 2" in (workspace.root / "app.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 干跑不得消费待修队列
# ---------------------------------------------------------------------------


class _FakeWS:
    """只够 fix_item 收尾用的工作区替身。"""

    def diff(self) -> str:
        return "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-1\n+2\n"

    def detect_language(self) -> str:
        return "python"

    def file_tree(self, limit: int = 200) -> str:
        return "app.py"

    def readme(self) -> str:
        return "# demo"

    def api_signatures(self, **kw) -> str:
        return "def f() -> int"

    def cleanup(self) -> None:
        return None


async def test_dry_run_keeps_item_in_fix_queue(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """干跑只出补丁、不提 PR → 条目必须留在 fix_queued。

    回归（envkit 实测）：dry-run 把 9 条全跑了一遍后，条目被置成 needs_manual，
    而修复候选只取 fix_queued —— 于是去掉 --dry-run 的真跑反而取不到任何候选，
    待修队列被一趟干跑白消费掉。
    """
    from fissue.fixer.agent import AgentTrace
    from fissue.models import FixAttempt, VerifierKind, VerifierResult

    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key,
                         Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key,
                       VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q"))

    fixer = AutoFixer(fixer_ctx)

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWS()

    async def fake_agent_run(**kwargs):
        return FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS), AgentTrace(rounds=1)

    async def fake_verify_fix(ctx_verify, *, verifier_id=None, base_run=None):
        return VerifierResult(kind=VerifierKind.EXECUTABLE, f2p_satisfied=True, conclusion="ok")

    async def fake_gate(**kwargs):
        return True, ""

    monkeypatch.setattr(fixer_ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(fixer.agent, "run", fake_agent_run)
    monkeypatch.setattr(fixer_ctx.verifier, "verify_fix", fake_verify_fix)
    monkeypatch.setattr(fixer, "_run_regression_gate", fake_gate)
    monkeypatch.setattr(fixer.pr_creator, "_save_patch", lambda item, diff: "/tmp/x.patch")

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    assert attempt.dry_run is True
    assert attempt.patch_path == "/tmp/x.patch"
    # 关键：仍在待修队列里，真跑才取得到
    assert repo.get_item_row(sample_issue.key).status == ItemStatus.FIX_QUEUED.value


async def test_real_run_after_dry_run_still_finds_candidate(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """干跑之后再跑真修，候选仍取得到（端到端口径一致）。"""
    from fissue.fixer.agent import AgentTrace
    from fissue.models import FixAttempt, VerifierKind, VerifierResult
    from fissue.pipeline.flush import FlushProcessor

    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key,
                         Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key,
                       VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q"))

    fixer = AutoFixer(fixer_ctx)

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWS()

    async def fake_agent_run(**kwargs):
        return FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS), AgentTrace(rounds=1)

    async def fake_verify_fix(ctx_verify, *, verifier_id=None, base_run=None):
        return VerifierResult(kind=VerifierKind.EXECUTABLE, f2p_satisfied=True, conclusion="ok")

    async def fake_gate(**kwargs):
        return True, ""

    monkeypatch.setattr(fixer_ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(fixer.agent, "run", fake_agent_run)
    monkeypatch.setattr(fixer_ctx.verifier, "verify_fix", fake_verify_fix)
    monkeypatch.setattr(fixer, "_run_regression_gate", fake_gate)
    monkeypatch.setattr(fixer.pr_creator, "_save_patch", lambda item, diff: "/tmp/x.patch")

    await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    candidates = FlushProcessor(fixer_ctx).fix_candidates(repo_slug=sample_issue.repo, limit=10)
    assert sample_issue.key in {c.key for c in candidates}
