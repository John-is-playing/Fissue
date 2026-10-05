"""验证器执行器：在沙盒里跑验证器并做 F2P 判定（Q7）。

F2P（fail-to-pass）判定逻辑
---------------------------
* **base 阶段**：在**未修复**的代码上跑验证器 → 期望**失败**（exit≠0），证明问题可复现。
* **fix 阶段**：在**已修复/已合并**的代码上跑验证器 → 期望**通过**（exit=0）。
* 两者都满足才算 ``f2p_satisfied``。

验证器本身不可靠时（base 就通过）会触发 :meth:`VerifierRunner.refine_and_retry`，
带着证据让 LLM 修正验证器，最多 ``verifier.max_rounds`` 轮。

清单型验证器（``kind=checklist``，无测试框架时的降级路径）无法用退出码判定，
改为：把执行输出交给 LLM 逐条判断（Q16）。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ai import prompts
from ..ai.client import LLMClient, Usage
from ..config import Settings
from ..errors import VerifierError
from ..logging_setup import get_logger
from ..models import (
    ItemStatus,
    ItemType,
    RawItem,
    VerifierKind,
    VerifierOutcome,
    VerifierResult,
    VerifierRun,
    VerifierSpec,
)
from ..sandbox.forwarder import SandboxManager
from ..sandbox.protocol import ExecRequest, MountSpec
from ..store.repository import Repository
from ..workspace import RepoWorkspace
from .generator import GeneratedVerifier, VerifierGenerator

log = get_logger(__name__)

CONTAINER_WORKDIR = "/workspace"


# ---------------------------------------------------------------------------
# 结果判定
# ---------------------------------------------------------------------------


def classify_outcome(result: Any, *, timed_out: bool = False, link_ok: bool = True) -> VerifierOutcome:
    """把沙盒执行结果映射成验证器结论。"""
    if not link_ok or not getattr(result, "ok", False):
        if timed_out or getattr(result, "timed_out", False):
            return VerifierOutcome.TIMEOUT
        return VerifierOutcome.ERROR
    if getattr(result, "timed_out", False):
        return VerifierOutcome.TIMEOUT
    code = getattr(result, "exit_code", None)
    if code is None:
        return VerifierOutcome.ERROR
    return VerifierOutcome.PASS if code == 0 else VerifierOutcome.FAIL


def judge_f2p(
    spec: VerifierSpec,
    base: VerifierRun,
    fix: VerifierRun | None,
    *,
    require_f2p: bool = True,
) -> VerifierResult:
    """F2P 总判定。"""
    result = VerifierResult(kind=spec.kind, base_run=base, fix_run=fix)

    if not require_f2p:
        result.f2p_satisfied = bool(fix and fix.outcome is VerifierOutcome.PASS)
        result.conclusion = "未要求 F2P，" + ("修复后通过" if result.f2p_satisfied else "修复后未通过")
        return result

    if base.outcome is VerifierOutcome.ERROR or base.outcome is VerifierOutcome.TIMEOUT:
        result.conclusion = f"验证器在 base 阶段无法正常运行（{base.outcome.value}），结论不可信"
        return result

    if spec.expect_fail_on_base and base.outcome is not VerifierOutcome.FAIL:
        result.conclusion = "验证器在未修复代码上就已通过，无法证明问题存在（验证器不可靠）"
        return result

    if fix is None:
        result.conclusion = "已确认问题可复现，但尚未执行修复阶段验证"
        return result

    if fix.outcome is VerifierOutcome.PASS:
        result.f2p_satisfied = True
        result.conclusion = "fail-to-pass 成立：修复前失败、修复后通过"
    elif fix.outcome in (VerifierOutcome.ERROR, VerifierOutcome.TIMEOUT):
        result.conclusion = f"修复阶段验证未跑通（{fix.outcome.value}），需人工确认"
    else:
        result.conclusion = "修复后验证仍失败：修复无效或验证器不适用"
    return result


#: 验证器「自身写坏」的失败特征：测试代码压根没跑起来。
_VERIFIER_BROKEN_HINTS = (
    "errors during collection",
    "error collecting",
    "collected 0 items",
    "no tests ran",
    "interrupted: ",
)

#: 失败输出里出现这些异常、且**完全没有** AssertionError 时，基本可判定是
#: 验证器自己调用出错（如引用了库里不存在的方法），而非断言到错误行为。
_VERIFIER_FAULT_EXC = ("AttributeError", "ImportError", "ModuleNotFoundError", "SyntaxError")


def verifier_self_error(run: VerifierRun) -> str | None:
    """判断这次失败是否由**验证器自身**引起；是则返回一句说明，否则 None。

    base 阶段失败只说明「验证器没通过」，并不等于「问题已复现」：验证器引用了
    库里并不存在的 API、导入失败、语法错误，或 pytest 根本没收集到用例，
    都会得到同样的非零退出码。把这类失败当成复现，会把一条真问题误判为
    无效请求（envkit #12 实测：测试调用 `TTLCache.put`，而库里只有 `set`）。
    """
    if run.outcome is not VerifierOutcome.FAIL:
        return None

    text = f"{run.stdout or ''}\n{run.stderr or ''}"
    lowered = text.lower()

    for hint in _VERIFIER_BROKEN_HINTS:
        if hint in lowered:
            return f"验证器未能正常执行（{hint.strip()}）"

    # 出现断言失败 → 验证器确实在断言目标行为，按「复现」处理
    if "AssertionError" in text:
        return None

    for exc in _VERIFIER_FAULT_EXC:
        if exc in text:
            return f"验证器自身抛出 {exc}（疑似引用了不存在的 API）"

    return None


# ---------------------------------------------------------------------------
# 既有测试回归门判定（见 docs/REGRESSION-GATE.md §3.3）
# ---------------------------------------------------------------------------

#: 回归门落库用的 stage 标识定义在 ``verifier/regression.py``
#: （常量与其使用者同处一处，避免跨模块仅为一个字符串而互相依赖）。


def extract_failed_tests(run: VerifierRun) -> list[str]:
    """从测试输出里尽量提取失败的用例名（strict 拒绝时要点名，否则人工无从复核）。

    覆盖 pytest（``FAILED tests/...::test_x``）、go（``--- FAIL: TestX``）、
    jest（``✕ name`` / ``● suite › test``）。
    """
    seen: list[str] = []
    for line in (run.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED ") and "::" in stripped:
            seen.append(stripped[len("FAILED "):].strip())
        elif stripped.startswith("--- FAIL:"):
            seen.append(stripped[len("--- FAIL:"):].strip().split(" ")[0])
        elif stripped.startswith("✕") or stripped.startswith("✗"):
            seen.append(stripped[1:].strip())
        elif stripped.startswith("● ") and "›" in stripped:
            seen.append(stripped[2:].strip())
    named: list[str] = []
    for name in seen:
        if name and name not in named:
            named.append(name)
    return named


def judge_regression(
    base_reg: VerifierRun | None,
    fix_reg: VerifierRun | None,
    *,
    mode: str,
    f2p_satisfied: bool,
) -> tuple[bool, str]:
    """回归门总判定，严格按 REGRESSION-GATE.md §3.3 的判定矩阵。

    返回 ``(是否通过, 说明)``。``mode`` 为 ``off`` / ``warn`` / ``strict``。

    **安全底线**（最容易做错的地方）：base 既有测试不绿、或跑不通
    （ERROR/TIMEOUT），一律视为「本门不可信」，记 warn 并放行——绝不据此拒绝。
    否则老仓库里本来就失败/跳过的测试会把**所有**条目误杀。
    """
    if mode == "off" or base_reg is None:
        return True, ""

    # base 跑不通 / 跳过 → 本门不可信，放行
    if base_reg.outcome in (VerifierOutcome.SKIPPED, VerifierOutcome.ERROR):
        return True, f"回归门未产生结论（base {base_reg.outcome.value}），放行"
    if base_reg.outcome is VerifierOutcome.TIMEOUT:
        return True, "回归门在 base 阶段超时，不可归咎于修复，放行"

    # 仓库本身就不绿 → 本门不可信，放行（§3.3 第 3 行的安全底线）
    if base_reg.outcome is VerifierOutcome.FAIL:
        failures = extract_failed_tests(base_reg)
        detail = ("（如：" + "、".join(failures[:3]) + "）") if failures else ""
        return True, f"⚠️ 仓库既有测试在 base 阶段就不通过{detail}，回归门不可信，未阻断"

    # base 绿。fix 阶段只是提醒；未跑 fix 时无法判定
    if fix_reg is None:
        return True, "既有测试在 base 全绿（修复阶段将再次检查）"

    if fix_reg.outcome is VerifierOutcome.PASS:
        return True, "既有测试在 base 与 fix 阶段均全绿"

    failures = extract_failed_tests(fix_reg)
    detail = ("，失败用例：" + "、".join(failures[:5])) if failures else ""

    if fix_reg.outcome is VerifierOutcome.FAIL:
        message = f"修复破坏了既有测试{detail}"
        if mode == "strict":
            return False, message
        return True, f"⚠️ {message}（未阻断）"

    # fix 阶段 ERROR/TIMEOUT → 环境问题，不可归咎于修复
    return True, f"修复阶段既有测试未跑通（{fix_reg.outcome.value}），不可归咎于修复，放行"


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


@dataclass
class VerifyContext:
    """一次验证所需的全部外部依赖。"""

    workspace: RepoWorkspace
    item: RawItem
    spec: VerifierSpec
    repo_slug: str = ""
    label: str = ""
    extra_mounts: list[MountSpec] | None = None
    test_hint: str | None = None


class VerifierRunner:
    """在沙盒中执行验证器，产出 F2P 结论。"""

    def __init__(
        self,
        sandbox: SandboxManager,
        repo: Repository,
        settings: Settings,
        *,
        client: LLMClient | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.repo = repo
        self.settings = settings
        self.client = client

    # -- 单次执行 ---------------------------------------------------------

    async def run_once(
        self,
        *,
        item_key: str,
        workspace: RepoWorkspace,
        spec: VerifierSpec,
        stage: str,
        extra_mounts: list[MountSpec] | None = None,
        label: str = "",
        verifier_id: int | None = None,
    ) -> VerifierRun:
        """在沙盒里执行一次验证器。"""
        started = time.monotonic()
        mounts = list(extra_mounts or [])
        mounts.append(
            MountSpec(source=str(workspace.root), target=CONTAINER_WORKDIR, read_only=False)
        )

        if spec.kind is VerifierKind.CHECKLIST:
            # 清单型：跑「尽可能接近」的命令，或直接空跑，然后交 LLM 判定
            run = VerifierRun(
                verifier_id=verifier_id,
                item_key=item_key,
                stage=stage,
                outcome=VerifierOutcome.SKIPPED,
                error="checklist 型验证器不在沙盒中以退出码判定",
                duration_seconds=round(time.monotonic() - started, 3),
            )
            self.repo.save_verifier_run(run)
            return run

        req = ExecRequest(
            image=self.settings.sandbox.image,
            command=spec.command or "",
            workdir=CONTAINER_WORKDIR,
            files=dict(spec.files),
            mounts=mounts,
            cpus=self.settings.sandbox.limits.cpus,
            memory_mb=self.settings.sandbox.limits.memory_mb,
            pids=self.settings.sandbox.limits.pids,
            timeout_seconds=min(spec.timeout_seconds, self.settings.sandbox.limits.timeout_seconds * 2),
            network=self.settings.sandbox.network,
            read_only_root=self.settings.sandbox.read_only_root,
            user=self.settings.sandbox.user,
            label=label or f"{item_key}:{stage}",
        )

        exec_result = await self.sandbox.run(req)
        outcome = classify_outcome(exec_result, link_ok=True)

        artifacts_dir = None
        if self.settings.sandbox.keep_artifacts:
            artifacts_dir = str(self._save_artifacts(item_key, stage, exec_result))

        run = VerifierRun(
            verifier_id=verifier_id,
            item_key=item_key,
            stage=stage,
            outcome=outcome,
            exit_code=getattr(exec_result, "exit_code", None),
            stdout=getattr(exec_result, "stdout", "") or "",
            stderr=(getattr(exec_result, "stderr", "") or "") + (
                f"\n[{exec_result.error}]" if getattr(exec_result, "error", None) else ""
            ),
            duration_seconds=round(time.monotonic() - started, 3),
            artifacts_dir=artifacts_dir,
            error=getattr(exec_result, "error", None),
        )
        self.repo.save_verifier_run(run)
        log.info(
            "验证器执行 %s [%s] -> %s（exit=%s，%.1fs）",
            item_key, stage, outcome.value, run.exit_code, run.duration_seconds,
        )
        return run

    # -- 完整 F2P 流程 ----------------------------------------------------

    async def verify(
        self,
        ctx: VerifyContext,
        *,
        verifier_id: int | None = None,
    ) -> VerifierResult:
        """对「未修复的 base」执行验证器，判定问题是否可复现。"""
        base = await self.run_once(
            item_key=ctx.item.key,
            workspace=ctx.workspace,
            spec=ctx.spec,
            stage="base",
            extra_mounts=ctx.extra_mounts,
            label=f"{ctx.item.key}:base",
            verifier_id=verifier_id,
        )
        result = judge_f2p(
            ctx.spec, base, None, require_f2p=self.settings.verifier.require_f2p
        )
        self._persist(ctx, result, verifier_id)
        return result

    async def verify_fix(
        self,
        ctx: VerifyContext,
        *,
        verifier_id: int | None = None,
        base_run: VerifierRun | None = None,
    ) -> VerifierResult:
        """在**已修复/已合并**的工作区上执行验证器，得出完整 F2P 结论。"""
        if base_run is None:
            runs = self.repo.verifier_runs(ctx.item.key, stage="base", limit=1)
            base_run = runs[0] if runs else None

        fix = await self.run_once(
            item_key=ctx.item.key,
            workspace=ctx.workspace,
            spec=ctx.spec,
            stage="fix",
            extra_mounts=ctx.extra_mounts,
            label=f"{ctx.item.key}:fix",
            verifier_id=verifier_id,
        )
        if base_run is None:
            result = VerifierResult(
                kind=ctx.spec.kind,
                base_run=None,
                fix_run=fix,
                f2p_satisfied=False,
                conclusion="缺少 base 阶段结果，无法构成 fail-to-pass 判定",
            )
        else:
            result = judge_f2p(
                ctx.spec, base_run, fix, require_f2p=self.settings.verifier.require_f2p
            )
        self._persist(ctx, result, verifier_id)
        return result

    async def verify_checklist(
        self,
        *,
        item_key: str,
        item: RawItem,
        spec: VerifierSpec,
        output: str,
        stage: str = "base",
    ) -> VerifierRun:
        """清单型验证器：把输出交 LLM 逐条判定（Q16 的降级路径）。"""
        if self.client is None:
            raise VerifierError("清单型验证器需要 LLM 客户端做判定")

        data, usage = await self.client.chat_json(
            prompts.checklist_judge_prompt(item, checklist=spec.checklist, output=output),
            purpose="checklist_judge",
            default={},
        )
        passed = bool(data.get("passed"))
        conclusion = str(data.get("conclusion") or "")
        items = data.get("items") or []
        detail = "\n".join(
            f"- [{i.get('satisfied')}] {i.get('reason', '')}" for i in items if isinstance(i, dict)
        )
        run = VerifierRun(
            item_key=item_key,
            stage=stage,
            outcome=VerifierOutcome.PASS if passed else VerifierOutcome.FAIL,
            stdout=detail,
            stderr="",
            f2p_ok=passed,
            error=None if passed else "清单未全部满足",
        )
        run.stdout = f"{conclusion}\n{detail}"
        self.repo.save_verifier_run(run)
        self._record_usage(usage, item_key)
        return run

    # -- 修正循环 ---------------------------------------------------------

    async def generate_and_validate(
        self,
        *,
        item: RawItem,
        workspace: RepoWorkspace,
        repo_context: str,
        generator: VerifierGenerator,
        test_hint: str | None = None,
        linked_context: str | None = None,
    ) -> tuple[int | None, VerifierSpec, VerifierResult]:
        """生成验证器 → 跑 base → 不可靠则修正，最多 max_rounds 轮。

        ``linked_context``：被本 PR 修复的原 Issue 正文（见 ``verifier_prompt``）。

        返回 ``(verifier_id, 最终 spec, base 阶段结果)``。
        """
        max_rounds = max(1, self.settings.verifier.max_rounds)
        generated: GeneratedVerifier = await generator.generate(
            item, repo_context=repo_context, test_hint=test_hint, linked_context=linked_context
        )
        spec = generated.spec
        best_result: VerifierResult | None = None

        for round_index in range(1, max_rounds + 1):
            verifier_id = self.repo.save_verifier(item.key, spec, rounds=round_index)
            if spec.kind is VerifierKind.CHECKLIST:
                # 清单型只能由人工/LLM 判定，这里直接给出「待判定」
                result = VerifierResult(
                    kind=spec.kind,
                    f2p_satisfied=False,
                    conclusion="清单型验证器：需执行后由 LLM 逐条判定",
                )
                self.repo.set_verifier_f2p(verifier_id, False)
                self.repo.set_item_status(item.key, ItemStatus.VERIFIER_READY)
                return verifier_id, spec, result

            # 可执行验证器却没有 command：跑不出任何结论。此前只记一条 warning 就
            # 继续拿它跑 base，白白浪费一次沙盒执行，最终落个 f2p=false 的
            # 「可执行」验证器（ratekit #6 实测如此）。这里改为带明确反馈让模型
            # 补一条命令，复用既有的修正回路；最后一轮仍缺命令则直接判不可靠转人工。
            if spec.kind.is_executable and not (spec.command or "").strip():
                if round_index < max_rounds:
                    log.warning("验证器 %s 缺少 command，第 %d 轮重新生成", item.key, round_index)
                    refined = await generator.refine(
                        spec,
                        item=item,
                        repo_context=repo_context,
                        failure_output=(
                            "你生成的是可执行验证器，但没有给出 command。没有运行命令就"
                            "无法执行任何验证。请补上一条可直接运行的命令"
                            "（例如 `python -m pytest test_x.py -q`）。"
                        ),
                        round_index=round_index + 1,
                        test_hint=test_hint,
                        linked_context=linked_context,
                    )
                    spec = refined.spec
                    continue
                self.repo.set_verifier_f2p(verifier_id, False)
                result = VerifierResult(
                    kind=spec.kind,
                    f2p_satisfied=False,
                    conclusion=f"可执行验证器缺少 command（重试 {max_rounds} 轮仍未给出），无法运行",
                )
                log.warning("验证器 %s 缺少 command 且已到最后一轮，判定不可靠", item.key)
                return verifier_id, spec, result

            base = await self.run_once(
                item_key=item.key,
                workspace=workspace,
                spec=spec,
                stage="base",
                label=f"{item.key}:base:r{round_index}",
                verifier_id=verifier_id,
            )
            result = judge_f2p(spec, base, None, require_f2p=self.settings.verifier.require_f2p)
            best_result = result

            # 验证器自身写坏（引用不存在的 API / 导入失败 / 没收集到用例）也会
            # 让 base 非零退出，与「问题已复现」的退出码一模一样。这里必须把
            # 两者分开，否则一条真问题会被判成「无法复现」而转人工甚至误标无效。
            self_error = verifier_self_error(base)
            if self_error is not None:
                log.warning("验证器自身有误 %s（第 %d 轮）：%s", item.key, round_index, self_error)
                result.conclusion = self_error
                if round_index >= max_rounds:
                    self.repo.set_verifier_f2p(verifier_id, False)
                    # 自错 ≠ 复现：必须摘掉 base_run，否则 reproducible 仍为真，
                    # 下游又会把这条真问题当成「已验证」推进。
                    result.base_run = None
                    return verifier_id, spec, result
                evidence = (
                    f"{self_error}\n\n"
                    "上面的失败来自验证器代码本身，而不是被测项目的行为。请检查你"
                    "所调用的 API 是否真实存在于该库中（方法名、参数、导入路径），"
                    "修正后重新给出验证器。\n\n"
                    f"=== 原始输出 ===\n{(base.stdout or '')}\n{(base.stderr or '')}"
                )
                refined = await generator.refine(
                    spec,
                    item=item,
                    repo_context=repo_context,
                    failure_output=evidence,
                    round_index=round_index + 1,
                    test_hint=test_hint,
                    linked_context=linked_context,
                )
                spec = refined.spec
                continue

            if result.reproducible:
                self.repo.set_verifier_f2p(verifier_id, False)   # 仅 base 通过，完整 F2P 待修复阶段
                log.info("验证器可用（问题已复现）%s，第 %d 轮", item.key, round_index)
                return verifier_id, spec, result

            if round_index >= max_rounds:
                log.warning("验证器 %s 经 %d 轮仍未复现问题：%s", item.key, max_rounds, result.conclusion)
                self.repo.set_verifier_f2p(verifier_id, False)
                return verifier_id, spec, result

            log.info("验证器不可靠（%s），第 %d 轮修正", result.conclusion, round_index)
            evidence = (base.stdout or "") + "\n" + (base.stderr or "")
            refined = await generator.refine(
                spec,
                item=item,
                repo_context=repo_context,
                failure_output=evidence,
                round_index=round_index + 1,
                test_hint=test_hint,
                linked_context=linked_context,
            )
            spec = refined.spec

        assert best_result is not None
        return None, spec, best_result

    # -- 内部 -------------------------------------------------------------

    def _persist(self, ctx: VerifyContext, result: VerifierResult, verifier_id: int | None) -> None:
        if verifier_id is not None:
            self.repo.set_verifier_f2p(verifier_id, result.f2p_satisfied)
        if result.reproducible:
            self.repo.set_item_status(ctx.item.key, ItemStatus.VERIFIED)
        elif result.f2p_satisfied:
            self.repo.set_item_status(ctx.item.key, ItemStatus.VERIFIED)

    def _save_artifacts(self, item_key: str, stage: str, exec_result: Any) -> Path:
        """把执行日志落盘，便于出报告与人工复核。"""
        safe_key = item_key.replace(":", "_").replace("/", "_")
        out_dir = self.settings.data_dir / "artifacts" / safe_key / stage
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "stdout.log").write_text(getattr(exec_result, "stdout", "") or "", encoding="utf-8")
        (out_dir / "stderr.log").write_text(getattr(exec_result, "stderr", "") or "", encoding="utf-8")
        (out_dir / "meta.json").write_text(
            _json_dump(
                {
                    "exit_code": getattr(exec_result, "exit_code", None),
                    "ok": getattr(exec_result, "ok", None),
                    "timed_out": getattr(exec_result, "timed_out", None),
                    "duration_seconds": getattr(exec_result, "duration_seconds", None),
                    "error": getattr(exec_result, "error", None),
                }
            ),
            encoding="utf-8",
        )
        return out_dir

    def _record_usage(self, usage: Usage, item_key: str) -> None:
        repo_key = item_key.split(":")[1] if ":" in item_key else item_key
        self.repo.record_usage(
            repo_key=repo_key,
            purpose=usage.purpose or "checklist_judge",
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
        )


def _json_dump(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)


def prepare_fix_workspace(
    base_workspace: RepoWorkspace,
    *,
    label: str,
) -> RepoWorkspace:
    """为「修复阶段」复制一份工作区，避免污染 base 副本。"""
    dest = base_workspace.root.parent / f"fix-{label}"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(base_workspace.root, dest)
    return RepoWorkspace(
        root=dest,
        slug=base_workspace.slug,
        token=base_workspace.token,
        default_branch=base_workspace.default_branch,
    )
