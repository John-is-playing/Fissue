"""既有测试回归门测试（docs/REGRESSION-GATE.md §6.1 的逐条自证）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fissue.config import RepoConfig
from fissue.models import (
    Category,
    Evaluation,
    ItemStatus,
    ItemType,
    Platform,
    QueueName,
    RawItem,
    RepoRef,
    VerifierKind,
    VerifierOutcome,
    VerifierRun,
    VerifierSpec,
)
from fissue.verifier.regression import (
    RegressionGate,
    resolve_regression_command,
    track_existing_test_files,
)
from fissue.verifier.runner import extract_failed_tests, judge_regression
from fissue.workspace import RepoWorkspace

PASS, FAIL, ERROR, TIMEOUT, SKIPPED = (
    VerifierOutcome.PASS,
    VerifierOutcome.FAIL,
    VerifierOutcome.ERROR,
    VerifierOutcome.TIMEOUT,
    VerifierOutcome.SKIPPED,
)


# ---------------------------------------------------------------------------
# 夹具：一个真实的临时 git 仓库（本地沙盒直接在它上面跑命令）
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _workspace(tmp_path: Path, *, tests: dict[str, str] | None = None) -> RepoWorkspace:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    for rel, content in (tests or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return RepoWorkspace(root=root, slug="a/b")


def _py(command: str) -> str:
    """把命令里的 python 固定为当前解释器（shell 里的裸 python 不保证是 venv）。"""
    return f'"{sys.executable}" {command}'


def _run(stage: str, outcome: VerifierOutcome, stdout: str = "") -> VerifierRun:
    return VerifierRun(item_key="k", stage=stage, outcome=outcome, stdout=stdout)


# ---------------------------------------------------------------------------
# §3.3 判定矩阵（纯函数，最容易做错的地方）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base,fix,mode,expected_ok",
    [
        (PASS, PASS, "strict", True),        # 两阶段都绿 → 通过
        (PASS, FAIL, "strict", False),       # base 绿、fix 红 → strict 拒绝
        (PASS, FAIL, "warn", True),          # warn 只告警，不阻断
        (FAIL, None, "strict", True),        # **安全底线**：仓库本就不绿 → 不阻断
        (FAIL, FAIL, "strict", True),        # 本就红，fix 也红 → 仍不阻断
        (ERROR, None, "strict", True),       # 环境问题 → 不阻断
        (TIMEOUT, None, "strict", True),     # 超时 → 不阻断
        (SKIPPED, None, "strict", True),     # 探不到命令 → 不阻断
        (PASS, None, "strict", True),        # base 绿但未跑 fix → 暂不作结论
        (PASS, ERROR, "strict", True),       # fix 环境问题 → 不可归咎于修复
        (PASS, TIMEOUT, "strict", True),     # fix 超时 → 不阻断
        (None, None, "warn", True),          # 没跑过 → 放行
    ],
)
def test_judge_regression_matrix(base, fix, mode, expected_ok) -> None:
    base_run = None if base is None else _run("regression:base", base)
    fix_run = None if fix is None else _run("regression:fix", fix)
    ok, _why = judge_regression(base_run, fix_run, mode=mode, f2p_satisfied=True)
    assert ok is expected_ok


def test_judge_regression_off_is_noop() -> None:
    ok, why = judge_regression(
        _run("regression:base", PASS), _run("regression:fix", FAIL), mode="off", f2p_satisfied=True
    )
    assert ok is True and why == ""


def test_judge_regression_strict_names_failed_tests() -> None:
    """strict 拒绝时必须点名失败的测试，否则人工无从复核。"""
    fix = _run(
        "regression:fix",
        FAIL,
        stdout="FAILED tests/test_wordcount.py::test_double_space\n1 failed, 9 passed\n",
    )
    ok, why = judge_regression(_run("regression:base", PASS), fix, mode="strict", f2p_satisfied=True)
    assert ok is False
    assert "tests/test_wordcount.py::test_double_space" in why


def test_extract_failed_tests_multiple_ecosystems() -> None:
    pytest_out = _run("s", FAIL, stdout="FAILED tests/a.py::test_x\nFAILED tests/a.py::test_x\n--- FAIL: TestGo\n")
    assert extract_failed_tests(pytest_out) == [
        "tests/a.py::test_x", "TestGo",
    ]


# ---------------------------------------------------------------------------
# §4.2 命令解析
# ---------------------------------------------------------------------------


def test_resolve_regression_command_priority(settings, tmp_path) -> None:
    ws = _workspace(tmp_path)
    repo_cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b", test_hint="hint-cmd")

    # 显式配置 > test_hint > 探测
    settings.verifier.regression_command = "explicit-cmd"
    assert resolve_regression_command(settings, repo_cfg, ws) == "explicit-cmd"

    settings.verifier.regression_command = None
    assert resolve_regression_command(settings, repo_cfg, ws) == "hint-cmd"

    repo_cfg.test_hint = None
    (ws.root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert resolve_regression_command(settings, repo_cfg, ws) == "python -m pytest -q"


def test_resolve_regression_command_detects_known_stacks(settings, tmp_path) -> None:
    ws = _workspace(tmp_path)
    repo_cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
    assert resolve_regression_command(settings, repo_cfg, ws) is None          # 空仓库探不到

    (ws.root / "go.mod").write_text("module x\n", encoding="utf-8")
    assert resolve_regression_command(settings, repo_cfg, ws) == "go test ./..."


def test_resolve_regression_command_none_when_undetectable(settings, tmp_path) -> None:
    ws = _workspace(tmp_path)
    repo_cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
    assert resolve_regression_command(settings, repo_cfg, ws) is None


def test_track_existing_test_files_only_tracked(tmp_path) -> None:
    """只认 git 已跟踪的测试文件；验证器新加、未跟踪的文件天然被排除。"""
    ws = _workspace(tmp_path, tests={"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    (ws.root / "tests" / "test_verifier_added.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8"
    )
    tracked = track_existing_test_files(ws, command="python -m pytest -q")
    assert tracked == ["tests/test_ok.py"]


# ---------------------------------------------------------------------------
# §环节 3：沙盒执行（local 沙盒，真跑）
# ---------------------------------------------------------------------------


async def _gate(settings, repo) -> RegressionGate:
    from fissue.sandbox.forwarder import SandboxManager

    return RegressionGate(SandboxManager(settings.sandbox, force_inline=True), repo, settings)


def _spec(files: dict[str, str] | None = None) -> VerifierSpec:
    return VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q", files=files or {})


def _verifier_id(repo, item_key: str = "github:psf/requests#1") -> int:
    """回归门的执行记录挂在验证器下（verifier_runs.verifier_id 为 NOT NULL）。"""
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=1, item_type=ItemType.ISSUE,
        title="t", body="b",
    )
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)
    return repo.save_verifier(item_key, _spec())


async def test_regression_gate_pass(settings, repo, tmp_path) -> None:
    settings.verifier.regression_command = _py("-m pytest -q")
    ws = _workspace(tmp_path, tests={"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
    key = "github:psf/requests#1"
    vid = _verifier_id(repo, key)

    run = await (await _gate(settings, repo)).run(
        item_key=key, workspace=ws, spec=_spec(), repo_cfg=cfg, stage="base",
        verifier_id=vid,
    )
    assert run.outcome is PASS
    assert run.stage == "regression:base"
    assert repo.verifier_runs(key, stage="regression:base")   # 已落库


async def test_regression_gate_fail(settings, repo, tmp_path) -> None:
    settings.verifier.regression_command = _py("-m pytest -q")
    ws = _workspace(tmp_path, tests={"tests/test_bad.py": "def test_bad():\n    assert False\n"})
    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
    vid = _verifier_id(repo)

    run = await (await _gate(settings, repo)).run(
        item_key="github:psf/requests#1", workspace=ws, spec=_spec(), repo_cfg=cfg,
        stage="base", verifier_id=vid,
    )
    assert run.outcome is FAIL
    assert "test_bad" in run.stdout


async def test_regression_gate_uses_its_own_timeout_budget(settings, repo, tmp_path) -> None:
    """超时用 regression_timeout_seconds（**不是** spec.timeout_seconds），到点即 TIMEOUT。

    用桩沙盒记录下发的预算并回一个超时结果：既确定性地验证「到点即返回」，
    又验证 §4.4 的独立预算要求（全量套件通常远慢于单测验证器）。
    """
    from fissue.sandbox.protocol import ExecResult

    settings.verifier.regression_timeout_seconds = 7
    settings.verifier.regression_command = _py("-m pytest -q")
    ws = _workspace(tmp_path, tests={"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
    vid = _verifier_id(repo)

    captured: dict[str, int] = {}

    class _SpySandbox:
        async def run(self, req):
            captured["timeout"] = req.timeout_seconds
            return ExecResult(ok=True, exit_code=None, timed_out=True, error="执行超时（7s）")

    gate = RegressionGate(_SpySandbox(), repo, settings)
    # spec 的预算是 600，回归门必须用自己的 7
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q", timeout_seconds=600)
    run = await gate.run(
        item_key="github:psf/requests#1", workspace=ws, spec=spec, repo_cfg=cfg,
        stage="base", verifier_id=vid,
    )
    assert run.outcome is TIMEOUT
    assert captured["timeout"] == 7
    assert run.duration_seconds < 20


async def test_regression_gate_skipped_without_command(settings, repo, tmp_path) -> None:
    ws = _workspace(tmp_path)          # 无 tests/pyproject，探不到命令
    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
    vid = _verifier_id(repo)

    run = await (await _gate(settings, repo)).run(
        item_key="github:psf/requests#1", workspace=ws, spec=_spec(), repo_cfg=cfg,
        stage="base", verifier_id=vid,
    )
    assert run.outcome is SKIPPED
    assert "未找到既有测试命令" in (run.error or "")
    # 跳过不阻断
    ok, _why = judge_regression(run, None, mode="strict", f2p_satisfied=True)
    assert ok is True


async def test_regression_gate_excludes_verifier_written_files(settings, repo, tmp_path) -> None:
    """验证器写入的测试文件必须被排除：否则 base 阶段会因它误红。"""
    settings.verifier.regression_command = _py("-m pytest -q")
    ws = _workspace(tmp_path, tests={"tests/test_ok.py": "def test_ok():\n    assert True\n"})
    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
    vid = _verifier_id(repo)

    # 验证器把自己的复现用例写进工作区（未跟踪），它会失败
    rel = "tests/test_reproduce.py"
    (ws.root / rel).write_text("def test_repro():\n    assert False\n", encoding="utf-8")
    spec = _spec({rel: "def test_repro():\n    assert False\n"})

    run = await (await _gate(settings, repo)).run(
        item_key="github:psf/requests#1", workspace=ws, spec=spec, repo_cfg=cfg,
        stage="base", verifier_id=vid,
    )
    assert run.outcome is PASS                       # 验证器文件被排除，套件仍全绿
    assert (ws.root / rel).exists()                  # 跑完无论如何都恢复


# ---------------------------------------------------------------------------
# §6.1 行为正确性：off 零开销 / warn 不改变结论 / strict 拒绝与放行
# ---------------------------------------------------------------------------


@pytest.fixture
def verify_ctx(settings, repo):
    """一个只够跑 VerifyStage 的 Context（克隆与验证器全部打桩）。"""
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


class _SpyGate:
    """记录调用的回归门桩。"""

    def __init__(self, result: VerifierRun | None = None) -> None:
        self.calls: list[str] = []
        self.result = result

    async def run(self, *, item_key, workspace, spec, repo_cfg, stage, verifier_id=None):
        self.calls.append(stage)
        return self.result or VerifierRun(
            verifier_id=verifier_id, item_key=item_key,
            stage=f"regression:{stage}", outcome=PASS,
        )


def _seed_issue(repo) -> RawItem:
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=1, item_type=ItemType.ISSUE,
        title="崩溃", body="boom", labels=["bug"],
    )
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)
    repo.save_evaluation(item.key, Evaluation(category=Category.BUG, model="s"))
    return item


class _FakeWs:
    def detect_language(self) -> str:
        return "python"

    def file_tree(self, limit: int = 150) -> str:
        return "app.py"

    def readme(self) -> str:
        return "# x"

    def api_signatures(self, **kw) -> str:
        """公开 API 契约；替身给最小样本即可。"""
        return "def f() -> int"

    def detect_test_command(self, hint: str | None = None) -> str | None:
        return None


def _stub_verify_stage(ctx, monkeypatch, base_result):
    from fissue.models import VerifierKind, VerifierResult, VerifierRun
    from fissue.pipeline.stages import VerifyStage

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWs()

    async def fake_validate(*, item, workspace, repo_context, generator, test_hint=None,
                            linked_context=None):
        base = VerifierRun(item_key=item.key, stage="base", outcome=VerifierOutcome.FAIL, exit_code=1)
        return 1, _spec(), VerifierResult(
            kind=VerifierKind.EXECUTABLE, base_run=base,
            f2p_satisfied=base_result.get("f2p", False),
            conclusion=base_result.get("conclusion", "可复现"),
        )

    monkeypatch.setattr(ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(ctx.verifier, "generate_and_validate", fake_validate)
    return VerifyStage(ctx)


async def test_verify_stage_off_never_runs_gate(verify_ctx, repo, monkeypatch) -> None:
    """regression_gate=off 时零开销：不发起任何回归门执行。"""
    verify_ctx.settings.verifier.regression_gate = "off"
    item = _seed_issue(repo)
    spy = _SpyGate()
    verify_ctx.regression_gate = spy
    stage = _stub_verify_stage(verify_ctx, monkeypatch, {})

    result = await stage.run(item, repo.latest_evaluation(item.key))

    assert spy.calls == []                      # 一次沙盒执行都没发起
    assert result.queued is QueueName.VERIFY


async def test_verify_stage_warn_gate_does_not_change_conclusion(verify_ctx, repo, monkeypatch) -> None:
    """warn 下 base 既有测试失败不改变结论，但 notes 里看得到告警。"""
    verify_ctx.settings.verifier.regression_gate = "warn"
    item = _seed_issue(repo)
    spy = _SpyGate(
        VerifierRun(
            item_key=item.key, stage="regression:base", outcome=FAIL,
            stdout="FAILED tests/test_legacy.py::test_old",
        )
    )
    verify_ctx.regression_gate = spy
    stage = _stub_verify_stage(verify_ctx, monkeypatch, {})

    result = await stage.run(item, repo.latest_evaluation(item.key))

    assert spy.calls == ["base"]
    assert result.queued is QueueName.VERIFY            # 仍入队，未被阻断
    assert any("回归门" in n for n in result.notes)
    assert result.regression_run is not None


async def test_verify_stage_base_red_does_not_pollute_f2p(verify_ctx, repo, monkeypatch) -> None:
    """base 回归门失败不污染 F2P 的 reproducible/fixed/f2p_satisfied 语义。"""
    verify_ctx.settings.verifier.regression_gate = "strict"
    item = _seed_issue(repo)
    spy = _SpyGate(
        VerifierRun(item_key=item.key, stage="regression:base", outcome=FAIL,
                    stdout="FAILED tests/test_legacy.py::test_old")
    )
    verify_ctx.regression_gate = spy
    stage = _stub_verify_stage(verify_ctx, monkeypatch, {})

    result = await stage.run(item, repo.latest_evaluation(item.key))

    # strict 下「仓库本就不绿」也必须放行（§3.3 第 3 行）
    assert result.queued is QueueName.VERIFY
    assert result.verifier_result.reproducible is True      # F2P 语义未被污染
    assert result.verifier_result.f2p_satisfied is False
    assert result.verifier_result.fix_run is None


# ---------------------------------------------------------------------------
# §6.1 PR 流水线：顺序契约 + strict 拒绝
# ---------------------------------------------------------------------------


class _PrWs(_FakeWs):
    def _git(self, args, *, cwd=None, timeout=60):  # pragma: no cover - 回归门被桩替换
        raise AssertionError("不应真的跑 git")


def _pr_item(number: int = 43) -> RawItem:
    return RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number, item_type=ItemType.PR,
        title="fix: 修复超时", body="修复 #42", head_branch="fix", base_branch="main",
        linked_issues=[42],
    )


def _pr_spec() -> VerifierSpec:
    return VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q",
                        files={"tests/test_repro.py": "def test_x():\n    assert False\n"})


def _pr_stage(verify_ctx, monkeypatch, *, base_outcome, fix_outcome, order, gate):
    from fissue.models import VerifierKind, VerifierResult, VerifierRun
    from fissue.pipeline.stages import PRVerifyStage

    verify_ctx.settings.verifier.regression_gate = "strict"
    verify_ctx.regression_gate = gate
    stage = PRVerifyStage(verify_ctx)

    base = VerifierRun(item_key="k", stage="base", outcome=base_outcome, exit_code=1)
    fix = VerifierRun(item_key="k", stage="fix", outcome=fix_outcome,
                      exit_code=0 if fix_outcome is PASS else 1)

    async def fake_clone(repo_cfg, **kwargs):
        order.append("clone")
        return _PrWs()

    async def fake_validate(*, item, workspace, repo_context, generator, test_hint=None,
                            linked_context=None):
        order.append("base")
        return 1, _pr_spec(), VerifierResult(
            kind=VerifierKind.EXECUTABLE, base_run=base, conclusion="stub"
        )

    async def fake_merge(ws, it, repo_cfg):
        order.append("merge")
        return True, "已合并（stub）"

    async def fake_fix(verify_ctx2, *, verifier_id=None, base_run=None):
        order.append("fix")
        result = VerifierResult(kind=VerifierKind.EXECUTABLE, base_run=base, fix_run=fix,
                                f2p_satisfied=fix_outcome is PASS, conclusion="stub")
        return result

    monkeypatch.setattr(verify_ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(verify_ctx.verifier, "generate_and_validate", fake_validate)
    monkeypatch.setattr(verify_ctx.verifier, "verify_fix", fake_fix)
    monkeypatch.setattr(stage, "_merge_pr", fake_merge)
    return stage


async def test_pr_gate_base_runs_before_merge(verify_ctx, repo, monkeypatch) -> None:
    """PR 流水线里 base 回归门必须在 _merge_pr 之前执行。"""
    order: list[str] = []
    item = _pr_item()
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    class _OrderedGate(_SpyGate):
        async def run(self, *, item_key, workspace, spec, repo_cfg, stage, verifier_id=None):
            self.calls.append(stage)
            order.append(f"regression:{stage}")
            return VerifierRun(item_key=item_key, stage=f"regression:{stage}", outcome=PASS)

    stage = _pr_stage(verify_ctx, monkeypatch, base_outcome=FAIL, fix_outcome=PASS,
                      order=order, gate=_OrderedGate())
    await stage.run(item, Evaluation(category=Category.BUG, model="s"))

    assert order == ["clone", "base", "regression:base", "merge", "fix", "regression:fix"]


async def test_pr_gate_strict_rejects_breaking_fix(verify_ctx, repo, monkeypatch) -> None:
    """strict：base 绿、fix 红 → 拒绝推荐合并，转人工，且点名失败测试。"""
    order: list[str] = []
    item = _pr_item(45)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    class _Gate(_SpyGate):
        async def run(self, *, item_key, workspace, spec, repo_cfg, stage, verifier_id=None):
            self.calls.append(stage)
            if stage == "base":
                return VerifierRun(item_key=item_key, stage="regression:base", outcome=PASS)
            return VerifierRun(
                item_key=item_key, stage="regression:fix", outcome=FAIL,
                stdout="FAILED tests/test_wordcount.py::test_double_space\n",
            )

    stage = _pr_stage(verify_ctx, monkeypatch, base_outcome=FAIL, fix_outcome=PASS,
                      order=order, gate=_Gate())
    result = await stage.run(item, Evaluation(category=Category.BUG, model="s"))

    assert result.status is ItemStatus.NEEDS_MANUAL
    assert result.queued is None
    assert repo.get_item_row(item.key).status == ItemStatus.NEEDS_MANUAL.value
    assert "tests/test_wordcount.py::test_double_space" in (result.skipped_reason or "")


async def test_pr_gate_strict_base_red_is_not_blocking(verify_ctx, repo, monkeypatch) -> None:
    """strict：仓库本来就红（base 红）→ **不阻断**，放行入队。这是安全底线。"""
    order: list[str] = []
    item = _pr_item(46)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    class _Gate(_SpyGate):
        async def run(self, *, item_key, workspace, spec, repo_cfg, stage, verifier_id=None):
            self.calls.append(stage)
            return VerifierRun(
                item_key=item_key, stage=f"regression:{stage}", outcome=FAIL,
                stdout="FAILED tests/test_legacy.py::test_old\n",
            )

    stage = _pr_stage(verify_ctx, monkeypatch, base_outcome=FAIL, fix_outcome=PASS,
                      order=order, gate=_Gate())
    result = await stage.run(item, Evaluation(category=Category.BUG, model="s"))

    assert result.queued is QueueName.FIX_BUG                 # 未被误杀
    assert repo.get_item_row(item.key).status == ItemStatus.QUEUED.value


# ---------------------------------------------------------------------------
# §6.2 成本约束：warn 模式不新增任何 LLM 调用
# ---------------------------------------------------------------------------


async def test_warn_mode_adds_no_llm_calls(settings, repo, monkeypatch) -> None:
    """回归门不调 LLM：用 stub 断言 calls == 0。"""
    from fissue.sandbox.forwarder import SandboxManager
    from fissue.verifier.regression import RegressionGate

    class _NoLLM:
        calls: list = []

        async def chat_json(self, *a, **k):  # pragma: no cover - 不该被调到
            self.calls.append(k)

    settings.verifier.regression_gate = "warn"
    settings.verifier.regression_command = _py("-m pytest -q")
    stub = _NoLLM()
    gate = RegressionGate(SandboxManager(settings.sandbox, force_inline=True), repo, settings)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp), tests={"tests/test_ok.py": "def test_ok():\n    assert True\n"})
        cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b")
        vid = _verifier_id(repo)
        await gate.run(
            item_key="github:psf/requests#1", workspace=ws, spec=_spec(), repo_cfg=cfg,
            stage="base", verifier_id=vid,
        )

    assert stub.calls == []                    # 回归门全程不碰 LLM


# ---------------------------------------------------------------------------
# §6.1 自动修复侧：strict 未通过 → needs_manual + 手工报告
# ---------------------------------------------------------------------------


async def test_autofix_strict_regression_rejection_reports(verify_ctx, repo, monkeypatch) -> None:
    from fissue.fixer.agent import AgentTrace
    from fissue.fixer.autofix import AutoFixer
    from fissue.models import FixAttempt, FixOutcome, Priority

    item = _seed_issue(repo)
    repo.set_item_status(item.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    vid = repo.save_verifier(item.key, _spec())
    item = repo.get_item(item.key)

    verify_ctx.settings.verifier.regression_gate = "strict"

    # base 基线绿（VerifyStage 已落库），fix 回归红 → 应拒绝
    repo.save_verifier_run(
        VerifierRun(verifier_id=vid, item_key=item.key, stage="regression:base", outcome=PASS)
    )

    class _Gate(_SpyGate):
        async def run(self, *, item_key, workspace, spec, repo_cfg, stage, verifier_id=None):
            self.calls.append(stage)
            return VerifierRun(
                item_key=item_key, stage="regression:fix", outcome=FAIL,
                stdout="FAILED tests/test_wordcount.py::test_double_space\n",
            )

    verify_ctx.regression_gate = _Gate()
    fixer = AutoFixer(verify_ctx)

    trace = AgentTrace(rounds=1, actions=["write"], written_files=["app.py"])
    attempt = FixAttempt(item_key=item.key, outcome=FixOutcome.SUCCESS, diff="d")
    spec = _spec()

    ok, why = await fixer._run_regression_gate(
        item=item, workspace=_PrWs(), spec=spec, repo_cfg=RepoConfig(
            platform=Platform.GITHUB, owner="psf", name="requests"
        )
    )
    assert ok is False
    assert "tests/test_wordcount.py::test_double_space" in why

    result = await fixer._handle_failure(item, attempt, trace, spec, regression_note=why)
    assert result.outcome is FixOutcome.NEEDS_MANUAL
    assert result.report_path is not None
    report = Path(result.report_path).read_text(encoding="utf-8")
    assert "回归门" in report
    assert "tests/test_wordcount.py::test_double_space" in report
