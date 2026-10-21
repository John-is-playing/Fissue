"""自动修复 Agent 测试：策略闸门、写入控制、失败降级报告。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from fissue.fixer.agent import AgentTrace, FixAgent, branch_name
from fissue.fixer.autofix import AutoFixer, FixReport, PRCreator
from fissue.models import (
    Category,
    Evaluation,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    RawItem,
    RepoRef,
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


@pytest.fixture(autouse=True)
def _stub_patch_reviewer(monkeypatch):
    """把评审打桩成「没有问题」。

    为什么必须桩：`auto_fix.review_rounds` 默认 1，而真实评审会去连 LLM 端点——本仓库 conftest
    的网络守卫只放行 localhost，而 httpx 的真实请求在那道守卫下**既不报错也不被拦截**，只会一路
    重试到超时（表现为 pytest 假死）。要断言评审行为的用例，在自己体内再 monkeypatch 一次即可
    （测试体内的 setattr 在 fixture 之后生效，覆盖它）。
    """
    from fissue.fixer.reviewer import PatchReviewer, ReviewResult

    async def _no_findings(self, **kwargs):
        return ReviewResult(conclusion="（测试默认：无意见）")

    monkeypatch.setattr(PatchReviewer, "review", _no_findings)


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
    assert "评测结果" in (attempt.error or "")


async def test_fix_item_skips_non_tier_priority(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.NONE, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.EVALUATED, priority=Priority.NONE)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "优先级" in (attempt.error or "")


async def test_fix_item_skips_suspicious_spam(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    from fissue.models import SpamSignal

    ev = Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s", spam=SpamSignal(is_spam=True))
    repo.save_evaluation(sample_issue.key, ev)
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "刷量" in (attempt.error or "")


async def test_fix_item_skips_without_verifier(fixer_ctx, repo, sample_issue) -> None:
    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "验证器" in (attempt.error or "")


async def test_fix_item_checklist_passes_gate(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """清单型不再是 autofix 死胡同（回归：曾硬跳过并报「无法自动判定修复结果」）。

    必须越过策略闸门进入修复流程——修得过不过由「修复后清单判定」判定。
    哨兵异常只为证明流程走到了克隆那一步，绝不真 clone 远端
    （子进程不受测试网络守卫保护，会挂死）。
    """
    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.CHECKLIST, checklist=["能跑通"]))

    async def _stop(repo_cfg, **kwargs):
        raise RuntimeError("已越过闸门（哨兵）")

    monkeypatch.setattr(fixer_ctx, "clone_workspace", _stop)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is not FixOutcome.SKIPPED
    assert "已越过闸门" in (attempt.error or "")


async def test_fix_item_skips_when_attempt_exists(fixer_ctx, repo, sample_issue) -> None:
    from fissue.models import FixAttempt

    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.EXECUTABLE, command="pytest"))
    repo.save_fix_attempt(FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS, pr_url="u"))

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "重复" in (attempt.error or "")


# ---------------------------------------------------------------------------
# 强制修复：评测缺失 / 优先级 / 刷量 三道闸不再拦；
# 验证器闸门保留——没有验证器无从判定修复，强也强不了。
# ---------------------------------------------------------------------------


async def test_fix_item_force_bypasses_priority_and_spam(fixer_ctx, repo, sample_issue) -> None:
    """优先级 none + 疑似刷量的强制条目：该拦它的是验证器闸门，而不是这两道。"""
    from fissue.models import SpamSignal

    _register(repo, sample_issue)
    ev = Evaluation(
        category=Category.BUG,
        priority=Priority.NONE,
        model="s",
        spam=SpamSignal(is_spam=True),
    )
    repo.save_evaluation(sample_issue.key, ev)
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED)
    item = repo.get_item(sample_issue.key)
    assert item is not None
    item.force_fix = True

    attempt = await AutoFixer(fixer_ctx).fix_item(item)
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "验证器" in (attempt.error or ""), f"应停在验证器闸门：{attempt.error}"


async def test_fix_item_force_without_evaluation_reaches_verifier_gate(fixer_ctx, repo, sample_issue) -> None:
    """没有评测的强制条目：评测闸门不再拦，仍停在验证器闸门。"""
    _register(repo, sample_issue)
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED)
    item = repo.get_item(sample_issue.key)
    assert item is not None
    item.force_fix = True

    attempt = await AutoFixer(fixer_ctx).fix_item(item)
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "验证器" in (attempt.error or ""), f"应停在验证器闸门：{attempt.error}"


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
    from fissue.pipeline.context import RuntimeContext

    _register(repo, sample_issue)
    fixer = AutoFixer.__new__(AutoFixer)

    # 与 test_e2e 同一手法：绕过构造造一个只带 repo 的 ctx（自造类与 RuntimeContext 不兼容）
    ctx = RuntimeContext.__new__(RuntimeContext)
    object.__setattr__(ctx, "repo", repo)
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
        #: 每轮收到的消息快照，供断言「提示是否真的进了上下文」。
        self.history: list[list[dict]] = []

    async def chat_json(self, messages, *, purpose: str = "", **kwargs):
        from fissue.ai.client import Usage

        self.calls += 1
        self.history.append(list(messages))
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
# Agent 循环：停滞检测（输出完全一致 → 提前收工）
#
# 回归：demo 实测 pipekit #8 —— base 阶段复现成功，但 Agent 每轮都只把
# drop_ranges 的 s[1] 改成 s[0]，验证器输出连续 10 轮**逐字节相同**
# （sha 一致、耗时同为 1.4s），一路空转到 12 轮上限，最后报出
# 「达到最大轮次（12）仍未通过验证器」——而真相是「卡住了」，不是「修了 12 次」。
# ---------------------------------------------------------------------------


class _DigestRunner:
    """按预设序列返回**不同输出**的验证器桩，用于驱动「输出是否变化」的判定。"""

    def __init__(self, outputs: list[tuple[int, str]]) -> None:
        from fissue.models import VerifierOutcome

        self.outputs = list(outputs) or [(1, "boom")]
        self.runs = 0
        self._Outcome = VerifierOutcome

    async def run_once(self, **kwargs):
        from fissue.models import VerifierRun

        exit_code, text = self.outputs[min(self.runs, len(self.outputs) - 1)]
        self.runs += 1
        outcome = self._Outcome.PASS if exit_code == 0 else self._Outcome.FAIL
        return VerifierRun(
            item_key=kwargs.get("item_key", "k"),
            stage="agent",
            outcome=outcome,
            exit_code=exit_code,
            stdout=text,
        )


def _agent_with_runner(settings, steps, runner):
    llm = _ScriptLLM(steps)
    return FixAgent(llm, None, runner, settings), llm, runner  # type: ignore[arg-type]


async def test_agent_stops_early_when_verifier_output_never_changes(settings, workspace, sample_issue) -> None:
    """输出完全一致且无进展 → 在 stall_rounds 内提前收工，不再空转到上限。"""
    settings.auto_fix.stall_rounds = 3
    # 每次都写同一个文件、验证器永远返回同一份失败输出
    steps = [{"action": "write", "files": {"app.py": "def f():\n    return 9\n"}}] * 10
    agent, llm, runner = _agent_with_runner(settings, steps, _DigestRunner([(1, "same failure")]))

    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=12,
    )

    assert attempt.outcome is FixOutcome.FAILED
    # 第 1 轮记基线（best=0），之后每轮无进展；累计到 3 → 第 4 轮收工
    assert runner.runs == 4
    assert llm.calls == 4                      # 没有空转到 12 轮
    assert "停滞" in (attempt.error or "")
    assert "仍未通过验证器" not in (attempt.error or "")   # 口径必须区分开
    assert trace.no_progress_streak == 3


async def test_agent_stalls_on_same_failing_case_despite_noisy_output(settings, workspace, sample_issue) -> None:
    """回归 pipekit #3：每轮输出字节都不同（临时路径/耗时噪声），但失败的**是同一个用例**。

    旧判据（输出逐字节相同）在这里完全失效——实测 #3 连跑 11 轮，每轮 sha 都不同，
    失败用例却始终是 test_count_batches_endpoint_overlap。新判据看「失败用例数
    有没有刷新更少」，因此能拦住这种形态。
    """
    settings.auto_fix.stall_rounds = 3
    noisy = [
        (1, f"FAILED tests/test_issue3.py::test_count_batches_endpoint_overlap\n"
            f"============================= 1 failed in 0.{i}s =============================")
        for i in range(12)
    ]
    # 每轮输出都不同（sha 各异），但失败用例数恒为 1
    steps = [{"action": "write", "files": {"app.py": f"def f():\n    return {i}\n"}} for i in range(12)]
    agent, llm, runner = _agent_with_runner(settings, steps, _DigestRunner(noisy))

    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=12,
    )

    assert runner.runs == 4                    # 旧判据下这里会跑到 12 轮
    assert llm.calls == 4
    assert "停滞" in (attempt.error or "")
    assert "test_count_batches_endpoint_overlap" in (attempt.error or "")   # 点名仍是哪条
    assert trace.failed_tests == ("tests/test_issue3.py::test_count_batches_endpoint_overlap",)


async def test_agent_stall_gate_can_be_disabled(settings, workspace, sample_issue) -> None:
    """stall_rounds=0 关闭闸门 → 退回旧的「耗尽轮次」行为。"""
    settings.auto_fix.stall_rounds = 0
    steps = [{"action": "write", "files": {"app.py": "def f():\n    return 9\n"}}] * 10
    agent, _llm, runner = _agent_with_runner(settings, steps, _DigestRunner([(1, "same failure")]))

    attempt, _trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=3,
    )

    assert runner.runs == 3
    assert "仍未通过验证器" in (attempt.error or "")


async def test_agent_keeps_going_while_output_keeps_changing(settings, workspace, sample_issue) -> None:
    """输出每轮都不同（真在推进）→ 不得被停滞闸门误杀。"""
    settings.auto_fix.stall_rounds = 2         # 阈值故意设得很激进
    steps = [{"action": "write", "files": {"app.py": f"def f():\n    return {i}\n"}} for i in range(1, 5)]
    runner = _DigestRunner([(1, "fail A"), (1, "fail B"), (1, "fail C"), (1, "fail D")])
    agent, _llm, _ = _agent_with_runner(settings, steps, runner)

    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=4,
    )

    assert runner.runs == 4                    # 一路跑到轮次上限，未被提前掐断
    assert trace.identical_output_streak == 1
    assert "仍未通过验证器" in (attempt.error or "")


async def test_agent_progress_resets_streak_when_failures_drop(settings, workspace, sample_issue) -> None:
    """失败用例数在减少（真在推进）→ 即使失败的还是同名用例，也不得判停滞。"""
    settings.auto_fix.stall_rounds = 2
    # 3 → 2 → 1 → 1 → 1 …… 稳步收敛，只是慢
    outputs = [
        (1, "FAILED t.py::a\nFAILED t.py::b\nFAILED t.py::c"),
        (1, "FAILED t.py::a\nFAILED t.py::b"),
        (1, "FAILED t.py::a"),
        (1, "FAILED t.py::a"),
    ]
    steps = [{"action": "write", "files": {"app.py": f"def f():\n    return {i}\n"}} for i in range(4)]
    agent, _llm, runner = _agent_with_runner(settings, steps, _DigestRunner(outputs))

    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=4,
    )

    assert runner.runs == 4, "收敛中不该被判停滞"
    assert trace.best_failure_count == 1
    assert "仍未通过验证器" in (attempt.error or "")   # 是「修不完」，不是「停滞」


async def test_agent_escalation_hint_appears_once(settings, workspace, sample_issue) -> None:
    """出现停滞迹象时，给模型一次「换思路」提示；之后不再重复塞入。"""
    settings.auto_fix.stall_rounds = 0         # 关闭收工，专门看提示
    steps = [{"action": "write", "files": {"app.py": "def f():\n    return 9\n"}}] * 5
    agent, llm, _ = _agent_with_runner(settings, steps, _DigestRunner([(1, "same")]))

    await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC,
        repo_context="ctx", max_rounds=5,
    )

    prompts = "\n".join(m["content"] for call in llm.history for m in call)
    assert prompts.count("没有任何进展") == 1, "提示应只出现一次"


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

    def call_graph(self, **kw) -> str:
        return "a.f → b.g"

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


def _fixable(repo, item) -> None:
    """把条目推到「可自动修复」的状态（有评测、tier1、已入修复队列、有可执行验证器）。"""
    _register(repo, item)
    repo.save_evaluation(item.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(item.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(item.key,
                       VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q"))


def _stub_fix_path(fixer, fixer_ctx, sample_issue, monkeypatch, *, gate=None) -> list[str | None]:
    """把克隆 / Agent / 完整 F2P / 回归门 / 补丁落盘都打桩，只留待测环节是真的。

    返回「每次 Agent 调用收到的 prior_failure」列表——返修断言全靠它。
    """
    from fissue.fixer.agent import AgentTrace
    from fissue.models import FixAttempt, VerifierResult

    feedback: list[str | None] = []

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWS()

    async def fake_agent(**kwargs):
        feedback.append(kwargs.get("prior_failure"))
        return FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS), AgentTrace(rounds=1)

    async def fake_verify(ctx_verify, *, verifier_id=None, base_run=None):
        # 真实的完整 F2P 会 await 沙盒（几十秒），这里用 sleep(0) 模拟那个**让出点**：
        # 没有它，`create_task` 起来的评审任务永远没机会开跑，并行性就无从断言。
        import asyncio as _asyncio

        await _asyncio.sleep(0)
        return VerifierResult(kind=VerifierKind.EXECUTABLE, f2p_satisfied=True, conclusion="ok")

    async def ok_gate(**kwargs):
        return True, ""

    monkeypatch.setattr(fixer_ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(fixer.agent, "run", fake_agent)
    monkeypatch.setattr(fixer_ctx.verifier, "verify_fix", fake_verify)
    monkeypatch.setattr(fixer, "_run_regression_gate", gate or ok_gate)
    monkeypatch.setattr(fixer.pr_creator, "_save_patch", lambda item, diff: "/tmp/x.patch")
    return feedback


def test_review_prompt_states_the_checklist_and_hard_rules(sample_issue) -> None:
    """评审提示词必须带上「真实踩过的坑」清单与反幻觉硬约束——它是这一步的全部依据。"""
    from fissue.ai import prompts

    text = prompts.fix_review_prompt(
        sample_issue,
        diff="+_seen = {}\n",
        changed_files={"src/x.py": "_seen = {}\n"},
        verifier_command="python -m pytest -q",
    )[0]["content"]

    for key in (
        "模块级可变全局",           # 这次的真实缺陷
        "改动层次",                 # 塞进解析函数会破坏契约
        "为了过测试而动测试",
        "fail-closed",
        "无界增长",
        "原样出现的代码片段",       # 反幻觉：引用不出原文就不算数
        "返回空数组",               # 不许为了显得尽职而编问题
        "只有前两级会触发返修",     # 严重度分级
    ):
        assert key in text, f"评审提示词缺少约束：{key}"


def test_parse_review_drops_fabricated_or_out_of_scope_findings() -> None:
    """反幻觉：越界文件、引用不实、没说为什么 → 一律丢弃；拼错的严重度降级为 nit。"""
    from fissue.fixer.reviewer import parse_review

    files = {"src/x.py": "_seen = {}\n"}
    result = parse_review(
        {
            "conclusion": "总评",
            "findings": [
                {"severity": "critical", "file": "src/x.py", "quote": "_seen = {}", "why": "全局"},
                {"severity": "weird", "file": "src/x.py", "quote": "_seen = {}", "why": "拼错"},
                {"severity": "major", "file": "src/y.py", "quote": "_seen = {}", "why": "越界"},
                {"severity": "major", "file": "src/x.py", "quote": "这段不在改动里", "why": "臆造"},
                {"severity": "nit", "file": "src/x.py", "quote": "_seen = {}", "why": ""},
            ],
        },
        changed_files=files,
        diff="+_seen = {}\n",
    )

    assert [(f.severity, f.blocking) for f in result.findings] == [("critical", True), ("nit", False)]
    assert len(result.dropped) == 3, result.dropped
    assert "片段：" in result.feedback() and "_seen = {}" in result.feedback()
    # 没有意见是完全合法的结果（而且我们希望它常见）
    assert parse_review({"findings": []}, changed_files=files).findings == []


async def test_review_blocking_findings_trigger_a_retry(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """评审报 critical → 带着评审意见（含原文片段）重跑一次；第二次干净就通过。"""
    from fissue.fixer.reviewer import PatchReviewer, ReviewFinding, ReviewResult

    _fixable(repo, sample_issue)
    fixer = AutoFixer(fixer_ctx)
    feedback = _stub_fix_path(fixer, fixer_ctx, sample_issue, monkeypatch)
    reviews = [
        ReviewResult(conclusion="有问题", findings=[ReviewFinding(
            severity="critical", file="src/fissue/web/hooks.py",
            quote="_seen = {}", why="模块级可变全局", suggestion="走显式数据流")]),
        ReviewResult(conclusion="干净"),
    ]

    async def fake_review(self, **kwargs):
        return reviews.pop(0)

    monkeypatch.setattr(PatchReviewer, "review", fake_review)

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    assert len(feedback) == 2, "评审报严重问题时应当返修一次"
    assert feedback[0] is None, "第一次不该带反馈"
    assert feedback[1] and "模块级可变全局" in feedback[1] and "_seen = {}" in feedback[1]
    assert attempt.dry_run is True


async def test_review_nits_are_recorded_without_a_retry(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """nit 不触发返修，但**必须记录**——人工复核要看得到（否则等于白评）。"""
    from pathlib import Path

    from fissue.fixer.reviewer import PatchReviewer, ReviewFinding, ReviewResult

    _fixable(repo, sample_issue)
    fixer = AutoFixer(fixer_ctx)
    feedback = _stub_fix_path(fixer, fixer_ctx, sample_issue, monkeypatch)

    async def fake_review(self, **kwargs):
        return ReviewResult(
            conclusion="能用",
            findings=[ReviewFinding(severity="nit", file="src/fissue/web/hooks.py",
                                    quote="_seen = {}", why="命名像私有但其实没必要")],
        )

    monkeypatch.setattr(PatchReviewer, "review", fake_review)

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    assert len(feedback) == 1, "nit 不该触发返修"
    # dry-run 不写人工报告（那条只在 needs_manual 时走），意见落在补丁旁边的旁挂文件里
    assert attempt.error and "评审意见见" in attempt.error
    assert attempt.patch_path, "dry-run 应当给出补丁路径"
    sidecar = Path(attempt.patch_path).with_suffix(".review.txt")
    assert sidecar.exists(), f"应当留下评审意见：{sidecar}"
    assert "命名像私有但其实没必要" in sidecar.read_text(encoding="utf-8")


async def test_review_can_be_disabled(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """`review_rounds=0` → 一次评审都不调用（关掉这一步的开关）。"""
    from fissue.fixer.reviewer import PatchReviewer, ReviewResult

    fixer_ctx.settings.auto_fix.review_rounds = 0
    _fixable(repo, sample_issue)
    fixer = AutoFixer(fixer_ctx)
    _stub_fix_path(fixer, fixer_ctx, sample_issue, monkeypatch)
    calls = {"n": 0}

    async def fake_review(self, **kwargs):
        calls["n"] += 1
        return ReviewResult()

    monkeypatch.setattr(PatchReviewer, "review", fake_review)

    await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    assert calls["n"] == 0, "关掉之后不该再调用评审"


async def test_review_overlaps_the_regression_gate(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """评审必须与 F2P/回归门**并行**（它只吃快照，不碰工作区）。

    这是这一步的墙钟保证：回归门要跑两分钟，评审若串在它后面，每次修复都平白多等一轮——那样
    用户会直接把这一步关掉。用「回归门开跑时，评审必须已经开始」来断言重叠。
    """
    import asyncio as _asyncio

    from fissue.fixer.reviewer import PatchReviewer, ReviewResult

    _fixable(repo, sample_issue)
    fixer = AutoFixer(fixer_ctx)
    started = _asyncio.Event()

    async def fake_review(self, **kwargs):
        started.set()
        return ReviewResult(conclusion="干净")

    async def gate_asserting_overlap(**kwargs):
        assert started.is_set(), "评审应当与回归门并行，而不是等它跑完"
        return True, ""

    monkeypatch.setattr(PatchReviewer, "review", fake_review)
    _stub_fix_path(fixer, fixer_ctx, sample_issue, monkeypatch, gate=gate_asserting_overlap)

    await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    assert started.is_set()


async def test_read_is_refused_after_the_stall_nudge(settings, workspace, sample_issue) -> None:
    """提醒之后 `read` 必须被拒——否则它只要「再读一个**新**文件」就能把空转计数清零。

    实测（Fissue #1 真跑）：提醒在 r8 发出，之后它仍读到 r10（r4 就读了一个新文件 daemon.py，
    于是空转计数被清零、循环接着跑），整轮烧掉约 19 万 prompt tokens。所以提醒之后必须堵死这条
    路：要么写，要么明确放弃，都要**有界**。
    """
    settings.auto_fix.stall_rounds = 4
    for name in ("README.md", "OTHER.md", "THIRD.md"):
        workspace.write(name, f"# {name}\n")
    # 前 5 轮反复读同一个文件（无进展）→ 第 6 轮命中提醒；第 6 轮的回复去读**新**文件
    steps = [{"action": "read", "paths": ["README.md"]} for _ in range(5)]
    steps += [
        {"action": "read", "paths": ["OTHER.md"]},
        {"action": "read", "paths": ["THIRD.md"]},
    ]
    agent, llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx"
    )

    assert any("read 已被禁用" in a for a in trace.actions), trace.actions[-8:]
    # 关键判别：提醒之后那个「新文件」绝不能进到上下文里（以前它能借此清零空转计数）
    assert "OTHER.md" not in trace.read_paths, "提醒之后不许再读到新文件"
    assert llm.calls == 6, f"提醒之后应当立刻收敛，实际烧了 {llm.calls} 次"
    assert attempt.outcome is not FixOutcome.SUCCESS, "空转就该收工"


def test_fix_agent_prompt_carries_the_regression_feedback(sample_issue) -> None:
    """回归门失败后重跑时，失败反馈必须摆进提示词（否则模型不知道该修什么）。

    回归（Fissue #1 真跑）：一次修复让自写验证器过了、却破坏了 `parse_event` 的既有契约，
    回归门拦下后直接收工——而「哪几条既有用例被改坏」正是最好的下一步提示。
    """
    from fissue.ai import prompts

    text = prompts.fix_agent_step_prompt(
        sample_issue,
        repo_context="ctx",
        verifier_command="python -m pytest -q",
        last_test_output=None,
        round_index=1,
        max_rounds=12,
        prior_failure="回归门未通过：失败用例 tests/test_web.py::test_parse_event",
    )[1]["content"]

    assert "上一轮修复的失败反馈" in text
    assert "test_parse_event" in text
    assert "既有用例" in text and "不要改" in text


async def test_agent_puts_the_prior_failure_in_the_prompt(settings, workspace, sample_issue) -> None:
    """`prior_failure` 必须真的进到模型看得见的那条提示里。"""
    steps = [{"action": "give_up", "reason": "做不了"}]
    agent, llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    await agent.run(
        item=sample_issue,
        workspace=workspace,
        spec=_SPEC,
        repo_context="ctx",
        prior_failure="回归门未通过：失败用例 tests/test_web.py::test_parse_event_handles_the_v5_shapes",
        max_rounds=1,
    )

    assert "test_parse_event_handles_the_v5_shapes" in str(llm.history[0])
    assert "上一轮修复的失败反馈" in str(llm.history[0])


async def test_regression_gate_failure_retries_the_agent_with_feedback(
    fixer_ctx, repo, sample_issue, monkeypatch
) -> None:
    """回归门失败 → 带着失败反馈重跑 Agent（默认 1 次）；第二次过了就算修好。

    回归（Fissue #1 真跑）：第一次修复让自写验证器过了、却破坏了既有契约；此前直接
    `needs_manual` 收工，等于把「再试一次就能改对」的机会扔掉。
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
    seen_feedback: list[str | None] = []

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWS()

    async def fake_agent_run(**kwargs):
        seen_feedback.append(kwargs.get("prior_failure"))
        return FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS), AgentTrace(rounds=1)

    async def fake_verify_fix(ctx_verify, *, verifier_id=None, base_run=None):
        return VerifierResult(kind=VerifierKind.EXECUTABLE, f2p_satisfied=True, conclusion="ok")

    gate_calls = {"n": 0}

    async def fake_gate(**kwargs):
        gate_calls["n"] += 1
        if gate_calls["n"] == 1:
            return False, "失败用例：tests/test_web.py::test_parse_event_handles_the_v5_shapes"
        return True, ""

    monkeypatch.setattr(fixer_ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(fixer.agent, "run", fake_agent_run)
    monkeypatch.setattr(fixer_ctx.verifier, "verify_fix", fake_verify_fix)
    monkeypatch.setattr(fixer, "_run_regression_gate", fake_gate)
    monkeypatch.setattr(fixer.pr_creator, "_save_patch", lambda item, diff: "/tmp/x.patch")

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    assert gate_calls["n"] == 2, "回归门失败要带着反馈重跑一次"
    assert len(seen_feedback) == 2
    assert seen_feedback[0] is None, "第一次不该带反馈"
    assert seen_feedback[1] is not None and "test_parse_event" in seen_feedback[1]
    assert attempt.dry_run is True


async def test_regression_gate_failure_without_budget_stays_needs_manual(
    fixer_ctx, repo, sample_issue, monkeypatch
) -> None:
    """`regression_retries=0` → 保持旧行为：回归门一失败就 needs_manual，不重跑。"""
    from fissue.fixer.agent import AgentTrace
    from fissue.models import FixAttempt, VerifierKind, VerifierResult

    fixer_ctx.settings.auto_fix.regression_retries = 0
    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key,
                         Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key,
                       VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q"))

    fixer = AutoFixer(fixer_ctx)
    agent_runs = {"n": 0}

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWS()

    async def fake_agent_run(**kwargs):
        agent_runs["n"] += 1
        return FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS), AgentTrace(rounds=1)

    async def fake_verify_fix(ctx_verify, *, verifier_id=None, base_run=None):
        return VerifierResult(kind=VerifierKind.EXECUTABLE, f2p_satisfied=True, conclusion="ok")

    async def fake_gate(**kwargs):
        return False, "失败用例：tests/test_web.py::test_parse_event_handles_the_v5_shapes"

    monkeypatch.setattr(fixer_ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(fixer.agent, "run", fake_agent_run)
    monkeypatch.setattr(fixer_ctx.verifier, "verify_fix", fake_verify_fix)
    monkeypatch.setattr(fixer, "_run_regression_gate", fake_gate)

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key), dry_run=True)

    assert agent_runs["n"] == 1, "余额为 0 时不该重跑"
    assert attempt.outcome is FixOutcome.NEEDS_MANUAL
    assert "回归门未通过" in (attempt.error or "")


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


# ---------------------------------------------------------------------------
# P1：fork 失败要停手，不能改换推送目标
# ---------------------------------------------------------------------------


async def test_fork_failure_stops_without_pushing_upstream(
    fixer_ctx, repo, sample_issue, monkeypatch
) -> None:
    """配了 fork 就只往 fork 推；fork 建不出来时停手，绝不改去推上游仓库。

    原先这里会静默降级成「直接推上游」，等于在别人的仓库里凭空多出一个分支，
    而唯一的痕迹是一行 warning 日志。
    """
    from fissue.config import RepoConfig
    from fissue.models import FixAttempt
    from fissue.platforms.base import PlatformError

    _register(repo, sample_issue)
    creator = PRCreator(fixer_ctx)
    repo_cfg = RepoConfig(platform=Platform.GITHUB, owner="psf", name="requests")
    adapter = await fixer_ctx.adapter_for_repo(repo_cfg)

    async def fake_fork(ref):
        raise PlatformError("fork 被拒绝")

    pushed: list[int] = []

    async def fake_push(*args, **kwargs):
        pushed.append(1)
        raise AssertionError("fork 失败时不该推送任何分支")

    async def fake_text(item, *, trace, spec, f2p_ok):
        return "标题", "正文"

    monkeypatch.setattr(adapter, "fork_repo", fake_fork)
    monkeypatch.setattr(creator, "_commit_and_push", fake_push)
    monkeypatch.setattr(creator, "_compose_pr_text", fake_text)
    monkeypatch.setattr(creator, "_save_patch", lambda item, diff: "/tmp/fork-fail.patch")

    attempt = await creator.submit(
        attempt=FixAttempt(item_key=sample_issue.key),
        trace=AgentTrace(rounds=1),
        item=sample_issue,
        workspace=cast(RepoWorkspace, _FakeWS()),
        repo_cfg=repo_cfg,
        spec=VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q"),
        f2p_ok=True,
        diff="diff --git a/app.py b/app.py\n",
    )

    assert attempt.outcome is FixOutcome.NEEDS_MANUAL
    assert attempt.patch_path == "/tmp/fork-fail.patch", "修复成果不该白费，补丁要留给人"
    assert "fork" in (attempt.error or "")
    assert pushed == [], "绝不改换推送目标"


async def test_existing_pr_is_reused_without_pushing(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """已有开放 PR → 复用、不推送、如实报成功。

    分支名是「条目 + 内容哈希」的**确定性**名字，同名即同内容，那个 PR 已经覆盖了
    这次修复。此时强推会被远端正确拒绝（``--force-with-lease`` 报 stale info），
    而把它报成「推送失败 / 需人工」是误导——真相是这条修复早就有 PR 了。

    实测：envkit 九个条目在 10-09 那次真跑里都建过 PR，此后每次重跑都撞在这个
    假失败上（Agent 明明修好了，验证器与回归门都过）。
    """
    from fissue.config import RepoConfig
    from fissue.models import FixAttempt

    _register(repo, sample_issue)
    creator = PRCreator(fixer_ctx)
    fixer_ctx.settings.auto_fix.pr_strategy.mode = "direct"     # 不涉及 fork
    repo_cfg = RepoConfig(platform=Platform.GITHUB, owner="psf", name="requests")
    adapter = await fixer_ctx.adapter_for_repo(repo_cfg)

    async def fake_find(ref, *, head):
        return {"number": 21, "url": "https://github.com/psf/requests/pull/21"}

    pushed: list[int] = []

    async def fake_push(*args, **kwargs):
        pushed.append(1)
        raise AssertionError("已有开放 PR 时不该推送任何分支")

    async def fake_text(item, *, trace, spec, f2p_ok):
        return "标题", "正文"

    monkeypatch.setattr(adapter, "find_open_pr", fake_find)
    monkeypatch.setattr(creator, "_commit_and_push", fake_push)
    monkeypatch.setattr(creator, "_compose_pr_text", fake_text)

    attempt = await creator.submit(
        attempt=FixAttempt(item_key=sample_issue.key),
        trace=AgentTrace(rounds=2),
        item=sample_issue,
        workspace=cast(RepoWorkspace, _FakeWS()),
        repo_cfg=repo_cfg,
        spec=VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q"),
        f2p_ok=True,
        diff="diff --git a/app.py b/app.py\n",
    )

    assert pushed == [], "已有 PR 时绝不该再推送"
    assert attempt.outcome is FixOutcome.SUCCESS, "已有 PR 说明修复早已完成，不该报需人工"
    assert attempt.pr_number == 21
    assert attempt.pr_url and "/pull/21" in attempt.pr_url


# ---------------------------------------------------------------------------
# P2：一次修复尝试的用量只记一次（逐轮明细 + 汇总会重复）
# ---------------------------------------------------------------------------


def test_fix_usage_is_recorded_only_per_call(fixer_ctx, repo, sample_issue) -> None:
    """不该额外写汇总行——逐轮记账已经把它写全了。

    实测：Agent 每一轮的调用都由 `LLMClient` 的 usage_sink 逐次写进 `llm_usage`
    （purpose=`agent:<仓库>`），而 `_record_usage` 又把整次尝试的**累加量**写成一行
    `agent`——同一批 token 记两遍。量化过：那批汇总行让全表用量虚高 **14.6%**，
    而 `llm.budget` 的每日闸门正是按这张表算的，虚高会让它在没超预算时提前硬停。
    """
    from fissue.ai.client import Usage
    from fissue.fixer.agent import AgentTrace

    before = repo.usage_today(repo_key=sample_issue.repo)["calls"]

    fixer = AutoFixer(fixer_ctx)
    record = getattr(fixer, "_record_usage", None)     # 方法被删掉也算通过：无行可记
    if record is not None:
        record(
            sample_issue,
            AgentTrace(usage=Usage(prompt_tokens=999, completion_tokens=999, model="m")),
        )

    after = repo.usage_today(repo_key=sample_issue.repo)
    assert after["calls"] == before, "不该写任何汇总行：逐轮调用已经记过账了"
    assert after["prompt_tokens"] == 0, "更不该把那 999 的累加量再记一遍"


# ---------------------------------------------------------------------------
# P2：allow_file_write 开关（此前从未被代码读取）
# ---------------------------------------------------------------------------


def test_allow_file_write_false_blocks_all_writes(settings, workspace) -> None:
    """`auto_fix.allow_file_write=false` 时一个文件都不写。

    这个开关原先写在示例配置里、文档也提它，但代码从未读取——设成 false 照样改
    工作区，是个会给人虚假安全感的空开关。
    """
    settings.auto_fix.allow_file_write = False
    agent = _agent(settings)
    existing = workspace.root / "app.py"
    original = existing.read_text(encoding="utf-8")

    written, reason = agent._apply_writes(
        workspace,
        {"app.py": "改坏了\n", "new_file.py": "x = 1\n"},
        locked=set(),
        trace=AgentTrace(),
    )

    assert written == []
    assert "allow_file_write" in (reason or "")
    assert existing.read_text(encoding="utf-8") == original, "配置禁止时不该改动已有文件"
    assert not (workspace.root / "new_file.py").exists(), "配置禁止时不该新建文件"


def test_allow_file_write_true_still_writes(settings, workspace) -> None:
    """对照组：开关为 true（默认）时照常写入，别把闸门做成焊死的。"""
    agent = _agent(settings)

    written, _reason = agent._apply_writes(
        workspace, {"app.py": "x = 1\n"}, locked=set(), trace=AgentTrace()
    )

    assert written == ["app.py"]
    assert (workspace.root / "app.py").read_text(encoding="utf-8") == "x = 1\n"


# ---------------------------------------------------------------------------
# P2：验证器自带的测试文件必须能被 Agent 读到
# ---------------------------------------------------------------------------


def _spec_with_files() -> VerifierSpec:
    """带生成文件的验证器（verify 阶段产物：只存在库里，不在被测仓库里）。"""
    return VerifierSpec(
        kind=VerifierKind.EXECUTABLE,
        command="python -m pytest tests/test_reproduce.py -q",
        files={"tests/test_reproduce.py": "def test_snapshot():\n    assert 1 == 2\n"},
    )


def test_agent_reads_verifier_files_from_spec(settings, workspace) -> None:
    """验证器文件不在工作区，但 Agent 必须读得到。

    envkit 实跑时，Agent 连续 12 轮都在读 ``tests/test_reproduce.py``——那是验证器
    自己生成的测试文件，磁盘上从来没有它，于是每轮都「读文件失败：不存在」，
    一个文件都没改就耗尽轮次被判需人工。它恰恰是「要满足什么」的规格。
    """
    spec = _spec_with_files()
    agent = _agent(settings)

    content = agent._safe_read(
        workspace, "tests/test_reproduce.py", AgentTrace(), locked_files=spec.files
    )

    assert content is not None and "assert 1 == 2" in content
    assert not (workspace.root / "tests" / "test_reproduce.py").exists(), "不该落盘"


def test_agent_still_returns_none_for_genuinely_missing_file(settings, workspace) -> None:
    """对照：真不存在的文件仍然读不到，别把兜底做成万能。"""
    agent = _agent(settings)

    assert agent._safe_read(workspace, "nope.py", None, locked_files={"tests/t.py": "x"}) is None


async def test_verifier_file_content_reaches_next_round_prompt(settings, workspace, sample_issue) -> None:
    """读到的验证器内容必须进下一轮提示——只「读得到」但没送进上下文就白搭。"""
    spec = _spec_with_files()
    steps = [
        {"action": "read", "paths": ["tests/test_reproduce.py"]},
        {"action": "done"},
    ]
    agent, llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    await agent.run(item=sample_issue, workspace=workspace, spec=spec, repo_context="")

    second_round = "\n".join(m["content"] for m in llm.history[1])
    assert "test_snapshot" in second_round, "验证器内容应进入第二轮上下文"


# ---------------------------------------------------------------------------
# P2：克隆的瞬时失败要重试（一次网络抖动不该让条目永久退出待修队列）
# ---------------------------------------------------------------------------


def _ready_for_fix(repo, sample_issue) -> None:
    """把条目铺到「能走到克隆那一步」的状态。"""
    _register(repo, sample_issue)
    repo.save_evaluation(
        sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s")
    )
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.EXECUTABLE, command="pytest"))


def _no_sleep(monkeypatch) -> None:
    async def _skip(_seconds: float) -> None:
        return None

    monkeypatch.setattr("fissue.fixer.autofix.asyncio.sleep", _skip)


@pytest.mark.parametrize("clone_retries,expected_calls", [(2, 3), (0, 1), (1, 2)])
async def test_clone_retries_transient_failure(
    fixer_ctx, repo, sample_issue, monkeypatch, clone_retries: int, expected_calls: int
) -> None:
    """克隆遇到 ``GitError`` 要按 ``auto_fix.clone_retries`` 重试。

    实测 envkit #1：一次 ``GnuTLS recv error (-110)`` 抖动就让整条修复尝试以
    ``failed`` 收场，而 ``fix_candidates`` 只取 ``fix_queued``——条目从此再也不会
    被修。verify 侧早有用 ``item_retries`` 应对同类抖动的先例。
    """
    from fissue.workspace import GitError

    _no_sleep(monkeypatch)
    fixer_ctx.settings.auto_fix.clone_retries = clone_retries
    _ready_for_fix(repo, sample_issue)

    calls: list[int] = []

    async def _always_transient(repo_cfg, **kwargs):
        calls.append(1)
        raise GitError("克隆失败：GnuTLS recv error (-110)")

    monkeypatch.setattr(fixer_ctx, "clone_workspace", _always_transient)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))

    assert len(calls) == expected_calls
    assert attempt.outcome is FixOutcome.FAILED, "重试耗尽后仍要如实报失败，不掩盖"
    assert "克隆失败" in (attempt.error or "")


async def test_clone_retry_gets_past_transient_failure(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """前两次抖动、第三次通过 → 流程继续往下走，而不是整条被判失败。"""
    from fissue.workspace import GitError

    _no_sleep(monkeypatch)
    _ready_for_fix(repo, sample_issue)

    calls: list[int] = []

    async def _flaky(repo_cfg, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise GitError("克隆失败：TLS 抖动")
        raise RuntimeError("哨兵：已越过克隆")

    monkeypatch.setattr(fixer_ctx, "clone_workspace", _flaky)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))

    assert len(calls) == 3, "应当重试到第 3 次"
    assert "哨兵" in (attempt.error or ""), "第 3 次确实被调用、并越过了克隆"


async def test_non_transient_error_is_not_retried(fixer_ctx, repo, sample_issue, monkeypatch) -> None:
    """非克隆类异常不重试——重试只针对网络/克隆抖动，别把真错误也拖三遍。"""
    _ready_for_fix(repo, sample_issue)

    calls: list[int] = []

    async def _logic_error(repo_cfg, **kwargs):
        calls.append(1)
        raise RuntimeError("配置/逻辑错误，重试也没用")

    monkeypatch.setattr(fixer_ctx, "clone_workspace", _logic_error)

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(sample_issue.key))

    assert len(calls) == 1
    assert attempt.outcome is FixOutcome.FAILED


# ---------------------------------------------------------------------------
# P2：提交时只暂存 Agent 改过的文件（验证器文件绝不能进 PR）
# ---------------------------------------------------------------------------


def test_stage_paths_excludes_verifier_files(settings, workspace) -> None:
    """提交修复分支只暂存 Agent 改过的文件，验证器文件绝不能进 PR。

    验证器（含它生成的测试文件）在**本地执行时会被写进工作区**。提交若用
    ``git add -A``，它们会被一起暂存、提交、推送——等于把「判定用的标尺」公开到
    上游仓库。dry-run 暴露不了这个问题：补丁走 ``git diff``，而 git diff 不含
    未跟踪文件。
    """
    # 模拟本地执行器把验证器自带的测试文件写进工作区（未跟踪）
    (workspace.root / "tests" / "test_reproduce.py").write_text(
        "def test_x():\n    assert False\n", encoding="utf-8"
    )
    # 模拟 Agent 真正改动的文件
    (workspace.root / "registry.py").write_text("import copy\n", encoding="utf-8")

    assert workspace.stage_paths(["registry.py"]) is True

    staged = workspace._git(["git", "diff", "--cached", "--name-only"]).stdout
    assert "registry.py" in staged
    assert "test_reproduce.py" not in staged, "验证器文件绝不能被提交进 PR"


def test_stage_all_would_have_included_verifier_files(settings, workspace) -> None:
    """对照：这正是当初不能用 ``git add -A`` 的原因（留作回归证据）。"""
    (workspace.root / "tests" / "test_reproduce.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )

    assert workspace.stage_all() is True

    staged = workspace._git(["git", "diff", "--cached", "--name-only"]).stdout
    assert "test_reproduce.py" in staged, "git add -A 会把未跟踪的验证器文件一起带上"


def test_stage_paths_rejects_empty_list(workspace) -> None:
    """没有改动时如实返回 False，让调用方给出「没有可提交的改动」。"""
    assert workspace.stage_paths([]) is False


# ---------------------------------------------------------------------------
# P2：只读不写的空转要早停，且说清真因
# ---------------------------------------------------------------------------


async def test_barren_read_only_loop_stops_early(settings, workspace, sample_issue) -> None:
    """连续只读不写 → 提前收工，且 error 说清是空转、不是修了很多次。

    实测 envkit #1：连续 12 轮只发 ``read``、一个文件没写，白烧 12 次 LLM 调用，
    最后却报「达到最大轮次仍未通过验证器」——真相是它**一次都没尝试修改**，
    那句话会误导人工复核。旧判据（验证器失败用例数没减少）对这种情况永远不成立：
    从不写入就不会触发验证器执行。
    """
    settings.auto_fix.stall_rounds = 4
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q")
    steps = [{"action": "read", "paths": ["README.md"]} for _ in range(12)]
    agent, llm, runner = _agent_with_runner(settings, steps, _StubRunner(None))

    attempt, trace = await agent.run(item=sample_issue, workspace=workspace, spec=spec, repo_context="")

    # 阈值 4：第 6 轮命中 → 先提醒一轮（这一轮照常调用模型），第 7 轮才真的收工
    assert llm.calls == 6, "命中闸门后要先提醒一轮，再收工"
    assert attempt.rounds == 7, "收工在「第 7 轮」这个名义轮次上"
    assert any("停摆提醒" in a for a in trace.actions), "必须留下提醒的痕迹"
    assert "不要再读文件" in str(llm.history[-1]), "提醒必须真的进了下一轮的提示词"
    # v1.0.0-b8 起还有一条「从未动笔」闸门，两条都命中时优先报后者（更具体）
    assert "一个文件都没改过" in (attempt.error or "")
    assert "修了很多次没修好" in (attempt.error or "")
    assert runner.runs == 0, "从未写入，验证器不该被执行"


async def test_write_accepts_fragment_replacement(settings, workspace, sample_issue) -> None:
    """`files` 的值可以是 `{find, replace}`：只改片段，不必重写整份文件。

    回归（Fissue #1 自维护实测）：模型对着 700 行的 `src/fissue/web/app.py` **连续 7 轮
    只读不写**——`write` 只认整份内容，完整重排一份大文件对它代价太大（输出长、易错、
    还可能被截断），于是它宁可再读一遍，最后被空转闸门判停。
    """
    workspace.write("app.py", "a = 1\nb = 2\nc = 3\n")
    steps = [{"action": "write", "files": {"app.py": {"find": "b = 2", "replace": "b = 22"}}}]
    agent, _llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    _attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx"
    )

    assert workspace.read("app.py") == "a = 1\nb = 22\nc = 3\n"
    assert "app.py" in trace.written_files
    assert any("片段替换" in a for a in trace.actions)


async def test_fragment_replacement_refuses_to_guess(settings, workspace, sample_issue) -> None:
    """片段不唯一 / 找不到 / 文件不存在 → 拒绝并给可操作理由；字符串形态保持兼容。"""
    from fissue.fixer.agent import AgentTrace

    agent, _llm, _runner = _agent_with_runner(settings, [], _StubRunner(None))
    trace = AgentTrace()
    workspace.write("dup.py", "k = 1\nk = 1\n")

    got, err = agent._resolve_content(
        workspace, "dup.py", {"find": "k = 1", "replace": "k = 2"}, trace=trace
    )
    assert got == "" and err is not None and "匹配到 2 处" in err

    got, err = agent._resolve_content(
        workspace, "dup.py", {"find": "nope", "replace": "x"}, trace=trace
    )
    assert got == "" and err is not None and "没找到" in err

    got, err = agent._resolve_content(
        workspace, "missing.py", {"find": "a", "replace": "b"}, trace=trace
    )
    assert got == "" and err is not None and "文件不存在" in err

    # 老形态（整份内容字符串）必须原样通过——兼容性是硬要求
    assert agent._resolve_content(workspace, "dup.py", "整份内容", trace=trace) == ("整份内容", None)


async def test_repeated_read_of_an_unchanged_file_is_refused(
    settings, workspace, sample_issue
) -> None:
    """同一文件第二次读 → **直接拒绝**（不再只回一句「没有变化」），且不重复供给正文。

    为什么从「回提示」改成「拒绝」（v1.0.0-b8）：实测（Fissue #1 真跑）它把「再看一眼」当成了
    常规操作——9 轮里 8 次读的都是「内容未变」的那两个文件，每一次都算一个动作、把轮次耗掉。
    内容本来就在它上下文里，再索取一次毫无意义。
    """
    settings.auto_fix.stall_rounds = 12          # 关掉两条闸门，专心看读拒绝
    settings.auto_fix.no_write_rounds = 0
    workspace.write("README.md", "# HELLO-MARKER\n")
    steps = [{"action": "read", "paths": ["README.md"]} for _ in range(3)]
    agent, llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    _attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx", max_rounds=3
    )

    assert "HELLO-MARKER" in str(llm.history[0]), "第一次读要把正文给出去"
    assert any("拒绝重复读" in a for a in trace.actions), "重复读要被明确拒绝"
    # 拒绝是在**下一轮**提示词才体现的：模型的动作在它读到的那条提示之后才被处理
    assert "早就在你的上下文里" in str(llm.history[2]), "拒绝理由要回给模型"
    assert "不要重复索取" in str(llm.history[2])


async def test_barren_guard_does_not_punish_exploration(settings, workspace, sample_issue) -> None:
    """每轮都读到**新文件** → 不算空转（别把多轮探查砍成停滞）。

    回归：`pipekit` 是「跨模块调用链」夹具，定位根因本来就要连着读好几轮不同文件
    （实测 r1 读 5 个 → r2 读 6 个 → r3 读 8 个，每轮都在扩大范围）。而这个判据最初
    只看「有没有写入」，于是在第 4 轮把这种正当探查判成空转、直接收工——两个 pipekit
    条目都因此被判要人工，而问题其实出在判据上。

    注意：本条只测**空转闸门**，所以把 v1.0.0-b8 新增的「从未动笔」闸门关掉（那条另有用例
    ``test_never_writing_exploration_is_bounded`` 在守）。
    """
    settings.auto_fix.stall_rounds = 4
    settings.auto_fix.no_write_rounds = 0        # 本条只测空转判据
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q")
    for i in range(6):
        (workspace.root / f"mod{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    steps = [{"action": "read", "paths": [f"mod{i}.py"]} for i in range(6)]
    agent, llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    attempt, _trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=spec, repo_context=""
    )

    assert llm.calls >= 6, "每轮都读到新文件，不该被空转闸门提前砍掉（旧判据第 4 轮就砍）"
    # 读完 6 个新文件后确实没有新信息了 → 仍应正常收工，而不是烧满 12 轮
    assert "没有读到任何新文件" in (attempt.error or ""), attempt.error


async def test_never_writing_exploration_is_bounded(settings, workspace, sample_issue) -> None:
    """一直只读不写（**哪怕每轮都读到新文件**）→ 「从未动笔」闸门兜住，不再拖到轮次上限。

    回归（Fissue #1 真跑）：9 轮只读不写里有 5 次「读到新文件」，每一次都把空转计数**整体清零**，
    于是它拖到第 9 轮才停；而「一直没产出任何改动」这件事本身早就够了。所以另立一个**只被 write
    清零**的计数器——正当的多轮探查照旧（那条判据另有用例在守），只是不再允许它拖到底。
    """
    settings.auto_fix.stall_rounds = 12          # 关掉空转闸门，只看「从未动笔」
    settings.auto_fix.no_write_rounds = 6
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q")
    for i in range(12):
        (workspace.root / f"mod{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    steps = [{"action": "read", "paths": [f"mod{i}.py"]} for i in range(12)]
    agent, llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=spec, repo_context=""
    )

    # 第 6 轮命中 → 提醒一轮（这一轮照常调用模型）→ 第 7 轮收工
    assert llm.calls == 6, f"应当第 6 轮提醒、第 7 轮收工，实际烧了 {llm.calls} 次"
    assert any("停摆提醒" in a for a in trace.actions), "要先提醒一次"
    assert "一个文件都没改过" in (attempt.error or ""), attempt.error
    assert attempt.outcome is FixOutcome.FAILED


async def test_write_of_invalid_python_is_rejected(settings, workspace, sample_issue) -> None:
    """把 `.py` 写成非法 Python → 拒绝该文件、不落盘，并把语法错误（行号 + 出错处）回给模型。

    回归（Fissue #1 真跑）：模型把 `src/fissue/web/hooks.py` 整个换成了 `/*placeholder*/`，于是
    `app.py` 导入即 SyntaxError、验证器 5 次全败、12 轮耗尽——现有护栏（锁定文件、保护路径、单文件
    大小、改动文件数、diff 行数）都没拦住，因为「300 行删除」本身不超标。
    """
    workspace.write("app.py", "x = 1\n")
    steps = [
        {"action": "write", "files": {"app.py": "/*placeholder*/"}},
        {"action": "give_up", "reason": "改不动"},
    ]
    agent, llm, runner = _agent_with_runner(settings, steps, _StubRunner(None))

    _attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx", max_rounds=2
    )

    assert workspace.read("app.py") == "x = 1\n", "被拒的文件绝不能落盘"
    assert "app.py" not in trace.written_files
    assert any("不是合法 Python" in r for r in trace.rejected), trace.rejected
    prompt = str(llm.history[1])
    assert "不是合法 Python" in prompt, "拒绝理由要回给模型"
    assert "placeholder" in prompt, "要带出错处，模型才定位得到"
    assert runner.runs == 0, "什么都没写进去，不该跑验证器"


async def test_partial_write_rejection_is_reported_to_the_model(
    settings, workspace, sample_issue
) -> None:
    """同批里坏文件被拒、好文件照常落盘——但**必须告诉模型**（否则它会去查验证器）。

    实测（Fissue #1）：它把 `hooks.py` 写成占位符、同时把 `app.py` 的导入改好，接着 5 轮都在查
    「验证器为什么不过」，从没回头看那个被写坏的文件。
    """
    from fissue.models import VerifierOutcome

    steps = [
        {"action": "write", "files": {"good.py": "y = 2\n", "bad.py": "def broken(:\n"}},
        {"action": "give_up", "reason": "停"},
    ]
    # 验证器打桩成失败 → 循环继续，才有第二轮提示词可断言
    agent, llm, _runner = _agent_with_runner(settings, steps, _StubRunner(VerifierOutcome.FAIL))

    _attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx", max_rounds=2
    )

    assert "good.py" in trace.written_files and "bad.py" not in trace.written_files
    prompt = str(llm.history[1])
    assert "bad.py" in prompt and "有文件没有被写入" in prompt, prompt[-500:]


async def test_fragment_edit_that_breaks_syntax_is_rejected(
    settings, workspace, sample_issue
) -> None:
    """片段替换如果把文件弄成非法 Python，同样拒绝（自检看的是**最终内容**）。

    注意别把「语法错」写错：`return` 后面什么都不写是**合法**的（返回 None），所以要拿括号没闭合
    这种来测。
    """
    workspace.write("app.py", "def f():\n    return 1\n")
    steps = [
        {"action": "write", "files": {"app.py": {"find": "    return 1", "replace": "    return (1"}}},
        {"action": "give_up", "reason": "改不动"},
    ]
    agent, _llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    _attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx", max_rounds=2
    )

    assert workspace.read("app.py") == "def f():\n    return 1\n", "非法结果不能落盘"
    assert any("不是合法 Python" in r for r in trace.rejected), trace.rejected


async def test_syntax_check_only_applies_to_python(settings, workspace, sample_issue) -> None:
    """非 `.py` 文件不做语法自检（写成什么样都放行）。"""
    steps = [{"action": "write", "files": {"notes.md": "/*placeholder*/\n不是 Python 也行\n"}}]
    agent, _llm, runner = _agent_with_runner(settings, steps, _StubRunner(None))

    _attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx", max_rounds=1
    )

    assert "notes.md" in trace.written_files, trace.rejected
    assert trace.rejected == []


async def test_syntax_check_can_be_disabled(settings, workspace, sample_issue) -> None:
    """`auto_fix.syntax_check=false` → 回到旧行为（真有仓库需要故意写非法 Python 时用）。"""
    settings.auto_fix.syntax_check = False
    steps = [{"action": "write", "files": {"broken.py": "/*placeholder*/"}}]
    agent, _llm, _runner = _agent_with_runner(settings, steps, _StubRunner(None))

    _attempt, trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=_SPEC, repo_context="ctx", max_rounds=1
    )

    assert "broken.py" in trace.written_files, trace.rejected
    assert trace.rejected == []


async def test_barren_streak_resets_after_a_write(settings, workspace, sample_issue) -> None:
    """对照：中途真的写了文件就不算空转（别把正常的多轮阅读误判成空转）。"""
    from fissue.models import VerifierOutcome

    settings.auto_fix.stall_rounds = 3
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q")
    steps = [
        {"action": "write", "files": {"app.py": "x = 2\n"}},   # r1 写 → 计数归零
        {"action": "read", "paths": ["README.md"]},            # r2 空转 1
        {"action": "read", "paths": ["README.md"]},            # r3 空转 2
        {"action": "read", "paths": ["README.md"]},            # r4 空转 3 → 到此才停
    ]
    agent, llm, _runner = _agent_with_runner(
        settings, steps, _StubRunner(VerifierOutcome.FAIL)
    )

    attempt, _trace = await agent.run(
        item=sample_issue, workspace=workspace, spec=spec, repo_context=""
    )

    assert llm.calls > 3, "r1 的写入应当把空转计数清零，否则第 3 轮就被误判为空转"
    assert "空转" in (attempt.error or "")


# ---------------------------------------------------------------------------
# 清单型验证器 × 修复闭环（B）：修复后 LLM 在修复工作区上逐条重判
# ---------------------------------------------------------------------------


async def test_judge_checklist_on_fix_pass(fixer_ctx, repo, sample_issue, workspace, monkeypatch) -> None:
    """全部满足 → f2p_satisfied；证据含清单引用文件内容 + 本次 diff。"""
    from fissue.models import VerifierKind, VerifierSpec

    _register(repo, sample_issue)
    spec = VerifierSpec(
        kind=VerifierKind.CHECKLIST,
        name="MySQL 支持",
        checklist=["`tests/test_a.py` 包含 test_x 用例"],
    )
    vid = repo.save_verifier(sample_issue.key, spec)
    workspace.write("app.py", "def f():\n    return 2\n")

    seen: dict[str, str] = {}

    async def fake_chat_json(messages, **kw):
        seen["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return (
            {
                "passed": True,
                "items": [{"index": 1, "satisfied": True, "reason": "已实现"}],
                "conclusion": "1/1 项满足",
            },
            None,
        )

    monkeypatch.setattr(fixer_ctx.verifier.client, "chat_json", fake_chat_json)
    fixer = AutoFixer(fixer_ctx)
    result = await fixer._judge_checklist_on_fix(sample_issue, workspace, spec, vid, "repo-ctx")

    assert result.f2p_satisfied is True
    assert "清单判定（fix）" in result.conclusion
    assert "1/1 项满足" in result.conclusion
    ev = seen["user"]
    assert "repo-ctx" in ev
    assert "test_x" in ev                      # 清单引用文件的内容被读进证据
    assert "return 2" in ev                    # 本次改动 diff 被附上


async def test_judge_checklist_on_fix_fail_includes_missing(fixer_ctx, repo, sample_issue, workspace, monkeypatch) -> None:
    """未全部满足：未满足项理由进结论，供修复循环反馈给 Agent。"""
    from fissue.models import VerifierKind, VerifierSpec

    _register(repo, sample_issue)
    spec = VerifierSpec(kind=VerifierKind.CHECKLIST, name="MySQL 支持", checklist=["a", "b"])
    vid = repo.save_verifier(sample_issue.key, spec)

    async def fake_chat_json(messages, **kw):
        return (
            {
                "passed": False,
                "items": [
                    {"index": 1, "satisfied": False, "reason": "未实现 DSN 解析"},
                    {"index": 2, "satisfied": True, "reason": "已声明"},
                ],
                "conclusion": "1/2 项满足",
            },
            None,
        )

    monkeypatch.setattr(fixer_ctx.verifier.client, "chat_json", fake_chat_json)
    fixer = AutoFixer(fixer_ctx)
    result = await fixer._judge_checklist_on_fix(sample_issue, workspace, spec, vid, "")
    assert result.f2p_satisfied is False
    assert "清单判定（fix）" in result.conclusion
    assert "未实现 DSN 解析" in result.conclusion


async def test_fix_item_checklist_pass_judgement_submits_pr(
    fixer_ctx, repo, sample_issue, workspace, monkeypatch
) -> None:
    """全链路：Agent 修复 → 修复后判定全部满足 → 回归门过 → 提 PR。"""
    from fissue.fixer.agent import AgentTrace
    from fissue.models import FixAttempt

    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.CHECKLIST, checklist=["能跑通"]))

    async def _clone(repo_cfg, **kwargs):
        return workspace

    monkeypatch.setattr(fixer_ctx, "clone_workspace", _clone)
    monkeypatch.setattr(fixer_ctx.settings.auto_fix, "review_rounds", 0)
    monkeypatch.setattr(fixer_ctx.settings.auto_fix, "regression_retries", 0)

    async def fake_chat_json(messages, **kw):
        return (
            {
                "passed": True,
                "items": [{"index": 1, "satisfied": True, "reason": "已实现"}],
                "conclusion": "1/1 项满足",
            },
            None,
        )

    monkeypatch.setattr(fixer_ctx.verifier.client, "chat_json", fake_chat_json)

    class _A:
        async def run(self, **kw):
            return (
                FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS, rounds=1),
                AgentTrace(),
            )

    submitted: dict = {}

    async def fake_submit(**kw):
        submitted.update(kw)
        kw["attempt"].pr_url = "http://pr/1"
        kw["attempt"].outcome = FixOutcome.SUCCESS
        return kw["attempt"]

    async def _reg(self, **kw):
        return True, ""

    fixer = AutoFixer(fixer_ctx)
    monkeypatch.setattr(fixer, "agent", _A())
    monkeypatch.setattr(fixer.pr_creator, "submit", fake_submit)
    monkeypatch.setattr(AutoFixer, "_run_regression_gate", _reg)

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.SUCCESS
    assert attempt.pr_url == "http://pr/1"
    assert submitted["f2p_ok"] is True


async def test_fix_item_checklist_fail_judgement_needs_manual(
    fixer_ctx, repo, sample_issue, workspace, monkeypatch
) -> None:
    """修复后判定未全部满足且重试余额用尽 → needs_manual，error 带未满足项理由。"""
    from fissue.fixer.agent import AgentTrace
    from fissue.models import FixAttempt

    _register(repo, sample_issue)
    repo.save_evaluation(sample_issue.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s"))
    repo.set_item_status(sample_issue.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.save_verifier(sample_issue.key, VerifierSpec(kind=VerifierKind.CHECKLIST, checklist=["a", "b"]))

    async def _clone(repo_cfg, **kwargs):
        return workspace

    monkeypatch.setattr(fixer_ctx, "clone_workspace", _clone)
    monkeypatch.setattr(fixer_ctx.settings.auto_fix, "review_rounds", 0)
    monkeypatch.setattr(fixer_ctx.settings.auto_fix, "regression_retries", 0)

    async def fake_chat_json(messages, **kw):
        return (
            {
                "passed": False,
                "items": [
                    {"index": 1, "satisfied": False, "reason": "未实现 DSN 解析"},
                    {"index": 2, "satisfied": True, "reason": "已声明"},
                ],
                "conclusion": "1/2 项满足",
            },
            None,
        )

    monkeypatch.setattr(fixer_ctx.verifier.client, "chat_json", fake_chat_json)

    class _A:
        async def run(self, **kw):
            return (
                FixAttempt(item_key=sample_issue.key, outcome=FixOutcome.SUCCESS, rounds=1),
                AgentTrace(),
            )

    async def _reg(self, **kw):
        return True, ""

    fixer = AutoFixer(fixer_ctx)
    monkeypatch.setattr(fixer, "agent", _A())
    monkeypatch.setattr(AutoFixer, "_run_regression_gate", _reg)

    attempt = await fixer.fix_item(repo.get_item(sample_issue.key))
    assert attempt.outcome is FixOutcome.NEEDS_MANUAL
    assert "清单判定（fix）" in (attempt.error or "")
    assert "未实现 DSN 解析" in (attempt.error or "")


async def test_run_verifier_checklist_short_circuit(settings) -> None:
    """清单型无可执行命令：不跑沙盒、返回验收提示。

    runner 传 None——代码路径若真去调 run_once 会直接报错，即守门。
    """
    from fissue.fixer.agent import AgentTrace
    from fissue.models import VerifierKind, VerifierSpec

    spec = VerifierSpec(kind=VerifierKind.CHECKLIST, checklist=["能跑通"])
    trace = AgentTrace()
    out = await _agent(settings)._run_verifier(
        None, None, spec, trace, verifier_id=None, round_index=1
    )
    assert "清单型验证器" in out and "LLM" in out


def test_checklist_command_lists_items() -> None:
    """agent prompt 的验收标准：逐条列清单 + 明说无可执行命令。"""
    from fissue.models import VerifierKind, VerifierSpec

    spec = VerifierSpec(kind=VerifierKind.CHECKLIST, checklist=["a", "b"])
    cmd = FixAgent._checklist_command(spec)
    assert "1. a" in cmd and "2. b" in cmd
    assert "无可执行命令" in cmd
