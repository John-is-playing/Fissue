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
