"""端到端测试：打通「抓取 → 评测 → 验证器 F2P → 批量 flush → 自动修复」。

全程离线：
* 平台适配器用 respx 拦截 HTTP；
* LLM 用按序返回的打桩客户端；
* 沙盒用 local 后端在一个**真实临时 git 仓库**里跑 pytest。

这条链路是项目的核心价值主张——如果它成立，说明
「验证器确实能在修复前失败、修复后通过」这一闭环是可靠的。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from fissue.models import (
    Category,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    QueueName,
    RepoRef,
    VerifierKind,
    VerifierOutcome,
)
from fissue.pipeline.context import RuntimeContext
from fissue.pipeline.fetcher import Fetcher
from fissue.pipeline.flush import FlushProcessor
from fissue.pipeline.queue import QueueManager
from fissue.pipeline.stages import EvalStage, VerifyStage
from fissue.workspace import RepoWorkspace

GH = "https://api.github.com"

# 带 bug 的源码：add 写成了减法
BUGGY_CALC = """\
def add(a, b):
    return a - b
"""
FIXED_CALC = """\
def add(a, b):
    return a + b
"""

# 验证器：修复前必然失败（2+3 != 5），修复后通过
VERIFIER_TEST = """\
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from calc import add


def test_add_returns_sum():
    assert add(2, 3) == 5
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def template_repo(tmp_path: Path) -> Path:
    """模板仓库（带 bug，已 git init）。"""
    root = tmp_path / "template"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "calc.py").write_text(BUGGY_CALC, encoding="utf-8")
    (root / "README.md").write_text("# demo\n\nA tiny calculator.\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def e2e_settings(settings, tmp_path: Path):
    s = settings.model_copy(deep=True)
    s.queues.verify_queue.flush_size = 1
    s.queues.verify_queue.idle_flush_seconds = 99999
    s.verifier.max_rounds = 1
    s.auto_fix.agent_max_rounds = 4
    s.sandbox.limits.timeout_seconds = 120
    return s


@pytest.fixture
def e2e_ctx(e2e_settings, repo, template_repo, monkeypatch):
    """构造 Context，并把「克隆仓库」替换为「复制模板仓库」。"""
    import asyncio
    import shutil
    import tempfile

    from fissue.ai.client import LLMClient
    from fissue.ai.evaluator import Evaluator
    from fissue.sandbox.forwarder import SandboxManager
    from fissue.verifier.generator import VerifierGenerator
    from fissue.verifier.regression import RegressionGate
    from fissue.verifier.runner import VerifierRunner

    ctx = RuntimeContext.__new__(RuntimeContext)
    object.__setattr__(ctx, "settings", e2e_settings)
    object.__setattr__(ctx, "db", repo.db)
    object.__setattr__(ctx, "repo", repo)
    object.__setattr__(ctx, "_adapters", {})
    object.__setattr__(ctx, "_lock", asyncio.Lock())

    llm = LLMClient(e2e_settings.llm)
    sandbox = SandboxManager(e2e_settings.sandbox, force_inline=True)
    object.__setattr__(ctx, "llm", llm)
    object.__setattr__(ctx, "sandbox", sandbox)
    object.__setattr__(ctx, "evaluator", Evaluator(llm, repo, e2e_settings))
    object.__setattr__(ctx, "generator", VerifierGenerator(llm, e2e_settings))
    object.__setattr__(ctx, "verifier", VerifierRunner(sandbox, repo, e2e_settings, client=llm))
    object.__setattr__(ctx, "regression_gate", RegressionGate(sandbox, repo, e2e_settings))

    copies: list[Path] = []

    async def fake_clone(repo_cfg, *, branch=None, depth=1):
        dest = Path(tempfile.mkdtemp(prefix="fissue-e2e-")) / "repo"
        shutil.copytree(template_repo, dest)
        copies.append(dest)
        return RepoWorkspace(root=dest, slug="psf/requests", default_branch="main")

    monkeypatch.setattr(ctx, "clone_workspace", fake_clone)
    object.__setattr__(ctx, "_e2e_copies", copies)
    return ctx


class ScriptedLLM:
    """按脚本应答的 LLM：按 purpose 分发，保证顺序无关。"""

    def __init__(self, script: dict[str, list[dict[str, Any]]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.seen: list[str] = []
        self.budget_guard = None

    async def chat_json(self, messages, *, purpose: str = "", **kwargs):
        from fissue.ai.client import Usage

        # purpose 形如 "agent@psf/requests" → 归一到 "agent"
        key = purpose.split("@")[0]
        self.seen.append(key)
        queue = self.script.get(key) or self.script.get("*") or []
        data = queue.pop(0) if queue else {}
        return data, Usage(purpose=key, model="scripted", prompt_tokens=80, completion_tokens=40,
                           cost_usd=0.0005)

    async def chat(self, messages, *, purpose: str = "", **kwargs):
        from fissue.ai.client import LLMResponse, Usage

        return LLMResponse(content="", model="scripted", usage=Usage(purpose=purpose))

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 第一步：抓取（respx 拦截）
# ---------------------------------------------------------------------------


@respx.mock
async def test_e2e_fetch_creates_item(e2e_ctx, repo) -> None:
    respx.get(f"{GH}/repos/psf/requests/issues").mock(
        return_value=httpx.Response(200, json=[{
            "number": 7,
            "title": "add 函数结果错误",
            "body": "调用 add(2,3) 期望 5，实际得到 -1。",
            "state": "open",
            "labels": [{"name": "bug"}],
            "user": {"login": "reporter"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }])
    )
    respx.get(f"{GH}/repos/psf/requests/issues/7/comments").mock(return_value=httpx.Response(200, json=[]))

    stats = await Fetcher(e2e_ctx).fetch_repo(e2e_ctx.settings.repo("psf/requests"))
    assert stats.created == 1 and stats.error is None

    item = repo.get_item("github:psf/requests#7")
    assert item is not None
    assert item.title == "add 函数结果错误"
    assert item.status is ItemStatus.NEW


# ---------------------------------------------------------------------------
# 完整闭环
# ---------------------------------------------------------------------------


async def test_e2e_full_loop(e2e_ctx, repo, template_repo, monkeypatch) -> None:
    """抓取 → 评测 → 生成验证器 → base 失败(可复现) → flush 定论 → 自动修复 → F2P 通过。"""
    ctx = e2e_ctx
    key = "github:psf/requests#7"

    # ---- 0) 直接落库一条 Issue（抓取已在上面单独验证） ------------------
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"),
                           base_branch="main")
    from fissue.models import RawItem

    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=7, item_type=ItemType.ISSUE,
        title="add 函数结果错误（崩溃/错误行为）",       # 含"错误" → 关键词可判为 BUG
        body="调用 add(2,3) 期望 5，实际得到 -1。",
        labels=["bug"], author="reporter",
    )
    repo.upsert_item(rid, item)

    # ---- 1) 评测 --------------------------------------------------------
    script = ScriptedLLM({
        "evaluate": [{
            "category": "bug",
            "scores": {
                "authenticity": {"score": 95, "reason": "复现明确", "confidence": 0.9},
                "importance": {"score": 80, "reason": "核心计算错误", "confidence": 0.9},
                "feasibility": {"score": 90, "reason": "一行修复", "confidence": 0.9},
                "difficulty": {"score": 15, "reason": "改动一行", "confidence": 0.9},
            },
            "labels_suggested": ["bug"],
            "action": "fix_now",
            "summary": "计算函数实现错误，应返回和而非差",
        }],
        "verifier": [{
            "kind": "executable",
            "name": "add-sum",
            "language": "python",
            "files": {"test_fix.py": VERIFIER_TEST},
            "command": "python -m pytest test_fix.py -q",
            "expect_fail_on_base": True,
            "expect_pass_on_fix": True,
            "timeout_seconds": 60,
            "notes": "add 返回差值，故断言 5 会失败；修复为求和后通过",
        }],
        "batch_conclusion": [{
            "results": [{
                "key": key, "verdict": "fix", "labels": ["reproduced"],
                "priority": "tier1", "reason": "低难度高重要性且已复现", "confidence": 0.95,
            }]
        }],
        "agent": [
            {"action": "write", "files": {"calc.py": FIXED_CALC}, "reason": "把减法改成加法"},
            {"action": "done", "summary": "已把 add 修正为求和"},
        ],
        "fix_pr_body": [{"title": "fix: 修正 add 实现", "body": "把差值改为求和。"}],
    })
    object.__setattr__(ctx, "llm", script)
    ctx.evaluator.client = script
    ctx.generator.client = script
    ctx.verifier.client = script
    ctx.evaluator.repo = repo

    import fissue.fixer.agent as agent_mod

    # 让 FixAgent 用脚本 LLM
    ev_result = await EvalStage(ctx).run_batch([repo.get_item(key)])
    assert ev_result.evaluated == 1
    evaluation = repo.latest_evaluation(key)
    assert evaluation is not None
    assert evaluation.category is Category.BUG
    assert evaluation.priority is Priority.TIER1
    assert evaluation.importance == 80 and evaluation.difficulty == 15

    # ---- 2) 验证：生成验证器 + base 阶段必须失败 ------------------------
    verify_stage = VerifyStage(ctx)
    from fissue.fixer.agent import FixAgent

    # VerifyStage 与 AutoFixer 内部各自持有 agent/verifier，统一注入脚本 LLM
    ctx.verifier.client = script
    ctx.verifier.runner = ctx.verifier  # noqa: B018  —— 保持接口一致
    v_result = await verify_stage.run(repo.get_item(key), evaluation)

    assert v_result.error is None, v_result.error
    assert v_result.verifier_kind is VerifierKind.EXECUTABLE
    assert v_result.verifier_result is not None
    assert v_result.verifier_result.reproducible is True, v_result.verifier_result.conclusion
    assert v_result.verifier_result.base_run.outcome is VerifierOutcome.FAIL
    assert v_result.queued is QueueName.VERIFY

    # ---- 3) flush：批量定论 → 打标签 + 派发修复 -------------------------
    processor = FlushProcessor(ctx)

    async def fake_label(payload, labels):
        return True, ""                     # 避免测试触网

    monkeypatch.setattr(processor, "_safe_label", fake_label)
    qm = QueueManager(ctx)
    outcome = await qm.flush(QueueName.VERIFY, handler=processor.make_handler(), force=True)
    assert outcome.flushed == 1
    assert outcome.conclusions[0]["verdict"] == "fix"

    row = repo.get_item_row(key)
    assert row.status == ItemStatus.FIX_QUEUED.value
    assert row.priority == Priority.TIER1.value

    # ---- 4) 自动修复（Agent 循环 → F2P 复核 → dry-run 出补丁）----------
    from fissue.fixer.autofix import AutoFixer

    fixer = AutoFixer(ctx)
    fixer.agent = FixAgent(script, repo, ctx.verifier, ctx.settings)   # 用脚本 LLM

    attempt = await fixer.fix_item(repo.get_item(key), dry_run=True)

    assert attempt.outcome is FixOutcome.NEEDS_MANUAL   # dry-run 不提交 PR
    assert attempt.error and "dry-run" in attempt.error
    assert attempt.patch_path and Path(attempt.patch_path).exists()
    assert "add(a, b)" in attempt.diff and "+" in attempt.diff

    # Agent 确实改过文件
    assert "calc.py" in attempt.changed_files

    # ---- 5) 校验 F2P 的两次执行记录都落库 -------------------------------
    runs = repo.verifier_runs(key)
    stages = {(r.stage, r.outcome.value) for r in runs}
    assert ("base", "fail") in stages                    # 修复前失败
    assert ("fix", "pass") in stages                     # 修复后通过
    assert ("agent", "pass") in stages                   # Agent 循环里也跑过一次

    # 且 F2P 结论被持久化为「满足」
    verifier = repo.latest_verifier(key)
    assert verifier is not None

    # ---- 6) 最终：验证器本身确实有区分能力 ------------------------------
    # 在未修复的模板仓库上跑同一个验证器 → 必须失败
    from fissue.sandbox.protocol import ExecRequest, MountSpec

    ws = RepoWorkspace(root=template_repo, slug="psf/requests")
    # 注意：local 沙盒以第一个挂载的 source 作为工作目录（与容器内挂载语义一致）
    ws_mount = [MountSpec(source=str(ws.root), target="/workspace", read_only=False)]

    req = ExecRequest(
        command="python -m pytest test_fix.py -q",
        files={"test_fix.py": VERIFIER_TEST},
        workdir="/workspace",
        mounts=ws_mount,
        label="e2e-check",
        timeout_seconds=60,
    )
    res = ctx.sandbox.run_sync(req)
    assert res.exit_code != 0, "验证器在未修复代码上竟然通过了——F2P 不成立"

    # 修好后 → 必须通过
    ws.write("calc.py", FIXED_CALC)
    res2 = ctx.sandbox.run_sync(
        ExecRequest(command="python -m pytest test_fix.py -q", workdir="/workspace",
                    mounts=ws_mount, label="e2e-check-fixed", timeout_seconds=60)
    )
    assert res2.exit_code == 0, f"修复后验证器仍失败：{res2.stdout[-800:]}"


# ---------------------------------------------------------------------------
# 负例：验证器不可靠时绝不进入自动修复
# ---------------------------------------------------------------------------


async def test_e2e_unreliable_verifier_blocks_autofix(e2e_ctx, repo) -> None:
    """验证器在 base 阶段就通过 → 判定不可靠 → needs_manual，不进修复队列。"""
    ctx = e2e_ctx
    key = "github:psf/requests#8"

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    from fissue.models import RawItem

    repo.upsert_item(rid, RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=8, item_type=ItemType.ISSUE,
        title="错误：疑似问题", body="说不清", labels=["bug"], author="u",
    ))
    from fissue.models import DimensionScore, Evaluation, Scores

    repo.save_evaluation(key, Evaluation(
        category=Category.BUG, priority=Priority.TIER1, model="stub",
        scores=Scores(authenticity=DimensionScore(score=90), importance=DimensionScore(score=80),
                      difficulty=DimensionScore(score=10)),
    ))
    repo.set_item_status(key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    script = ScriptedLLM({
        "verifier": [{
            "kind": "executable",
            "name": "always-pass",
            "files": {"test_always.py": "def test_ok():\n    assert True\n"},
            "command": "python -m pytest test_always.py -q",
        }],
    })
    ctx.generator.client = script
    ctx.verifier.client = script

    result = await VerifyStage(ctx).run(repo.get_item(key), repo.latest_evaluation(key))

    assert result.queued is None                      # 没有进入修复队列
    assert result.status is ItemStatus.NEEDS_MANUAL
    assert result.verifier_result is not None
    assert result.verifier_result.f2p_satisfied is False
    assert "不可靠" in result.verifier_result.conclusion
    assert repo.get_item_row(key).status == ItemStatus.NEEDS_MANUAL.value
