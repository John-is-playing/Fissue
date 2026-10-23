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

import asyncio
import re
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ai import prompts
from ..ai.client import LLMClient
from ..config import Settings
from ..errors import VerifierError
from ..logging_setup import get_logger
from ..models import (
    ItemStatus,
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


def workspace_mounts(workspace: RepoWorkspace, *, git_readonly: bool = True) -> list[MountSpec]:
    """把工作区挂进容器：整体可写，但 `.git` **只读**（`index` 除外）。

    为什么要动 `.git`：容器对工作区可写，而 `.git` 是**宿主的执行入口**——hooks、`config`
    里的 `filter.*.clean` / `diff.*.textconv` / `credential.helper`，以及 `objects` / `refs`
    （能伪造提交与引用，把内容混进面向上游的补丁）。宿主侧已有几道软闸门（见
    ``RepoWorkspace.harden_git``），这一条是从**挂载层**掐掉：写不进去就没有东西可执行、
    也没有东西可伪造。

    为什么把 `.git/index` 单独留成可写：不少仓库的测试套件会顺手跑 `git status`/`git diff`，
    它们要刷新索引——整块只读会让这类套件直接报 `Read-only file system`（响亮但无谓的失败）。
    索引被改也无害：宿主提交前会 ``reset_index()`` 拉回 HEAD，再按白名单重新 stage。

    真容器验证过（docker 29.8 + `fissue/sandbox-base`）：工作区写 OK、`.git/index` 写 OK、
    `git status` OK；而 `.git/config`、`.git/hooks`、`.git/refs`、`.git/objects` 一律
    `Read-only file system`。确需容器写 git 时把 ``sandbox.git_readonly`` 关掉。
    """
    mounts = [MountSpec(source=str(workspace.root), target=CONTAINER_WORKDIR, read_only=False)]
    git_dir = workspace.root / ".git"
    if not git_readonly or not git_dir.is_dir():
        return mounts
    mounts.append(
        MountSpec(source=str(git_dir), target=f"{CONTAINER_WORKDIR}/.git", read_only=True)
    )
    index = git_dir / "index"
    if index.exists():
        mounts.append(
            MountSpec(source=str(index), target=f"{CONTAINER_WORKDIR}/.git/index", read_only=False)
        )
    return mounts


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


def run_broken_reason(
    exit_code: int | None, command: str, stdout: str | None, stderr: str | None
) -> str | None:
    """判断一次执行是否「压根没跑起来」；是则返回原因，否则 None。

    这类失败与「断言失败」退出码一样非零，但语义完全不同：pytest 用法错误
    （exit=4：目标文件不存在 / conftest 加载失败）、测试收集失败。把它当成
    「问题已复现」或「修复破坏了既有测试」都是误判——实测（Pi）先后把
    「文件找不到」当复现、把 conftest 缺依赖当回归失败，导致 12 轮空转与
    通过后被反复返修。
    """
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    cmd = (command or "").lower()
    if exit_code == 4 and "pytest" in cmd:
        return "命令用法错误（pytest exit=4）：目标文件不存在或 conftest 加载失败"
    if "file or directory not found" in text:
        return "命令的目标文件不存在"
    if any(
        hint in text
        for hint in ("errors during collection", "error collecting", "while loading conftest")
    ):
        return "测试收集失败（conftest/import 错误）"
    return None


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


#: 回归断言的**用例名前缀约定**。生成器被要求把「修复前就已成立」的断言以此开头命名，
#: 我们才能机械地校验「自称回归断言的东西在未修复代码上确实通过」。
#: 见 :func:`regression_assertion_conflict`。
REGRESSION_TEST_PREFIX = "test_regression"


def _leaf_test_name(test_id: str) -> str:
    """``tests/a.py::Cls::test_x`` → ``test_x``（取最后一段）。"""
    return (test_id or "").rsplit("::", 1)[-1].strip()


def regression_assertion_conflict(run: VerifierRun) -> str | None:
    """检查「自称回归断言」的用例是否在**未修复代码上就失败**——那是自相矛盾。

    为什么需要它：pipekit #8 实测，自动生成的验证器里有一条
    ``test_regression_standard_drop_behavior``，断言
    ``drop_ranges([(1, 3), (4, 6)], [(2, 5)]) == []``。这条断言在**未修复的 main 上
    同样失败**，而且与该验证器自己的其它断言互斥（任何「按单个分片判定剔除」的
    自然实现都不可能同时满足，我实测两种语义都试过）——**数学上不可满足**。
    它被标成「回归断言（保持向后兼容）」，实际却在描述一个从未存在过的行为。

    后果很隐蔽：Agent 把两处缺陷都改对了、仓库自带基线 61 项全绿，**仍然永远过不了
    这个验证器**（12 轮空转到上限）。而 ``verifier_self_error`` 拦不住——那只判
    「异常抛在验证器自己的文件里」，这是一条**正常失败的断言**，看起来与
    「问题已复现」毫无区别。

    判据：名字带回归前缀、却在 base 阶段失败 → 它描述的不是「既有行为」，
    按定义不是回归断言。
    """
    if run.outcome is not VerifierOutcome.FAIL:
        return None
    failed = failed_test_ids_from_output(run.stdout or "")
    offenders = [t for t in failed if _leaf_test_name(t).startswith(REGRESSION_TEST_PREFIX)]
    if not offenders:
        return None
    names = "、".join(offenders[:3])
    return (
        f"被命名为回归断言的用例在**未修复代码上就失败**：{names}。"
        "回归断言描述的是「修复前就已成立的既有行为」，它在 base 阶段**必须通过**；"
        "在 base 就失败有两种成因，请按这个顺序排查："
        "① **夹具不自洽**——沙盒里只有仓库快照（没有 `config.yaml` / `.env` / 宿主已登记的仓库），"
        "如果你的用例依赖这些，失败的是夹具：请自造配置（`Settings(...)` / `RepoConfig(...)` / "
        "`load_settings(overrides={{...}})`）并用真实类型打桩；"
        "② 期望值与库的真实语义不符——那就改正这条断言的期望值，或删掉它。"
        "不要为了让它通过去改被测代码。"
    )


#: 验证器「自身写坏」的失败特征：测试代码压根没跑起来。
_VERIFIER_BROKEN_HINTS = (
    "errors during collection",
    "error collecting",
    "collected 0 items",
    "no tests ran",
    "interrupted: ",
    # pytest 的用法错误：命令里的目标文件不存在（``ERROR: file or directory not
    # found: tests/test_reproduce.py``，exit=4）。实测（Pi 部署）：沙盒的
    # ``files`` 通道没把验证器文件送进容器可见的位置，base 阶段就把「跑都没跑
    # 起来」当成了「问题已复现」——假阳性必须堵死。
    "file or directory not found",
    # 仓库自己的 conftest 在沙盒里加载失败（缺第三方依赖，如 ``import yaml``）：
    # 同样是「测试压根没跑」，不能算复现。
    "while loading conftest",
)

#: 这些异常若**抛在验证器自己的文件里**，基本可判定是验证器调用了不存在的
#: API（方法名 / 属性 / 导入路径写错，或用错了语法），而不是被测项目的行为。
#:
#: ``TypeError`` 也要算：envkit #12 实测，验证器对 ``TTLCache`` 用了下标赋值
#: ``cache["a"] = 1``（该库只提供 ``set()``），pytest 报
#: ``TypeError: 'TTLCache' object does not support item assignment``，
#: 定位在验证器自己的文件里——此前不在集合中，被当成「问题已复现」放行，
#: 最终靠结论阶段的 LLM 才识破，条目被误标 invalid。
#: 判定依据是**抛出位置**（见 ``verifier_self_error``），所以库自身抛 TypeError
#: 的真缺陷不受影响：那种情况的定位行落在被测库的文件里，会被放行。
_VERIFIER_FAULT_EXC = frozenset(
    {"AttributeError", "ImportError", "ModuleNotFoundError", "SyntaxError", "TypeError"}
)

#: pytest 每个失败用例的定位行：``<路径>.py:<行号>: <异常名>``。
#: 只匹配这种短摘要行；``...: in test_x`` 这类栈帧行与 ``::test_y`` 都不会命中。
_LOCATION_RE = re.compile(r"^([^\s:]+\.py):(\d+):\s*([A-Za-z_][\w.]*)\s*$", re.MULTILINE)


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def verifier_self_error(run: VerifierRun, *, verifier_files: Iterable[str] = ()) -> str | None:
    """判断这次失败是否由**验证器自身**引起；是则返回一句说明，否则 None。

    base 阶段失败只说明「验证器没通过」，并不等于「问题已复现」。两者必须分开，
    否则会出现双向误判：

    * **验证器自己写错**：测试调用了库里并不存在的方法（envkit #12 调用
      ``TTLCache.put``，而库里只有 ``set``），pytest 因 AttributeError 非零退出，
      与「断言到错误行为」的退出码一模一样 —— 会被当成复现，把真问题误标无效。
    * **被测库自己抛异常**：Issue 报的就是「调用某个 API 会抛 AttributeError」，
      这时 AttributeError 正是**合法证据**，若一律当成验证器写错，
      会把真缺陷反复退回去重生成，永远验不出来。

    区分依据是**异常抛在哪里**，而不是异常叫什么名字：抛在验证器自己的文件里
    → 验证器写错；抛在被测库的文件里 → 真缺陷。``verifier_files`` 传该验证器
    写入工作区的文件路径（``spec.files`` 的键）。
    """
    if run.outcome is VerifierOutcome.ERROR:
        # 沙盒层判定的「跑都没跑起来」（pytest 用法错误 / conftest 加载失败 /
        # 目标文件不存在）同样是验证器自身问题，必须走修正回路，不能当作复现证据。
        if run.error and any(
            key in run.error for key in ("用法错误", "目标文件不存在", "收集失败")
        ):
            return run.error
        return None
    if run.outcome is not VerifierOutcome.FAIL:
        return None

    text = f"{run.stdout or ''}\n{run.stderr or ''}"
    lowered = text.lower()

    # 测试压根没跑起来（收集失败 / 一个用例都没有）——与断言无关，直接算自错
    for hint in _VERIFIER_BROKEN_HINTS:
        if hint in lowered:
            return f"验证器未能正常执行（{hint.strip()}）"

    fault_locs = [(_norm_path(p), exc) for p, _, exc in _LOCATION_RE.findall(text) if exc in _VERIFIER_FAULT_EXC]
    if not fault_locs:
        # 只有断言失败（或形态未知）→ 按「问题已复现」处理
        return None

    files = {_norm_path(f) for f in verifier_files}
    if not files:
        # 不知道验证器写了哪些文件，就无从判断异常归属。宁可放行（按复现处理），
        # 也不要把一条真缺陷误判成验证器写错、白白退回重生成。
        return None

    if all(path in files for path, _ in fault_locs):
        excs = ", ".join(sorted({exc for _, exc in fault_locs}))
        return f"验证器自身抛出 {excs}（疑似引用了不存在的 API）"

    # 异常落在被测库的文件里 → 这是真缺陷，不是验证器的锅
    return None


# ---------------------------------------------------------------------------
# 既有测试回归门判定（见 docs/REGRESSION-GATE.md §3.3）
# ---------------------------------------------------------------------------

#: 回归门落库用的 stage 标识定义在 ``verifier/regression.py``
#: （常量与其使用者同处一处，避免跨模块仅为一个字符串而互相依赖）。


def failed_test_ids_from_output(text: str) -> tuple[str, ...]:
    """从测试输出文本里提取失败用例名（顺序保留、去重）。

    覆盖 pytest（``FAILED tests/...::test_x``）、go（``--- FAIL: TestX``）、
    jest（``✕ name`` / ``● suite › test``）。Agent 循环用它判断「是否还在推进」，
    故返回**元组**（可哈希、可比较，便于放进 dataclass 与集合运算）。
    """
    seen: list[str] = []
    for line in (text or "").splitlines():
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
    return tuple(named)


def extract_failed_tests(run: VerifierRun) -> list[str]:
    """从测试输出里尽量提取失败的用例名（strict 拒绝时要点名，否则人工无从复核）。

    覆盖 pytest（``FAILED tests/...::test_x``）、go（``--- FAIL: TestX``）、
    jest（``✕ name`` / ``● suite › test``）。
    """
    return list(failed_test_ids_from_output(run.stdout or ""))


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
        mounts = [
            *list(extra_mounts or []),
            *workspace_mounts(workspace, git_readonly=self.settings.sandbox.git_readonly),
        ]

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

        materialized = self._materialize_verifier_files(workspace, spec)
        try:
            exec_result = await self.sandbox.run(req)
        finally:
            # 验证器自带文件跑完即收，免得被 PR 提交流程捡进改动集。
            self._restore_verifier_files(workspace, materialized)
            # 沙盒刚在**同一个工作区**里跑过不可信代码（PR 代码 + 模型生成的验证器），而宿主
            # 随后要在这个工作区里 add/commit/push。动宿主 git 之前先把 `.git` 收回可信状态
            # ——钩子与 config 里的 filter/textconv/credential.helper 都是执行入口。
            await asyncio.to_thread(workspace.harden_git)
        outcome = classify_outcome(exec_result, link_ok=True)

        # 「跑都没跑起来」不等于「问题已复现」（pytest 用法错误 / conftest 加载
        # 失败 / 目标文件不存在）。实测（Pi）先撞文件不存在、后撞 conftest 缺
        # 依赖，base 阶段都把环境问题当成了复现证据，12 轮修复全程空转。
        broken_reason = run_broken_reason(
            getattr(exec_result, "exit_code", None),
            spec.command or "",
            getattr(exec_result, "stdout", ""),
            getattr(exec_result, "stderr", ""),
        )
        if broken_reason and outcome is not VerifierOutcome.ERROR:
            outcome = VerifierOutcome.ERROR

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
            error=getattr(exec_result, "error", None) or broken_reason,
        )
        self.repo.save_verifier_run(run)
        log.info(
            "验证器执行 %s [%s] -> %s（exit=%s，%.1fs）",
            item_key, stage, outcome.value, run.exit_code, run.duration_seconds,
        )
        return run

    # -- 验证器文件物化 ----------------------------------------------------

    @staticmethod
    def _materialize_verifier_files(
        workspace: RepoWorkspace, spec: VerifierSpec
    ) -> list[tuple[str, str | None]]:
        """把验证器自带文件写进**宿主工作区**，返回可回滚的备份。

        为什么不只依赖沙盒的 ``files`` 通道：实测（Pi 部署）容器看到的
        ``/workspace`` 与宿主工作区不是同一份内容，``files`` 写进去的文件
        在容器里根本不存在，pytest 直接
        ``ERROR: file or directory not found: tests/test_reproduce.py``（exit 4）
        ——12 轮修复全部撞在这上面。宿主落盘后，工作区挂进容器即可见，
        与部署形态无关。跑完由 :meth:`_restore_verifier_files` 还原。
        """
        backups: list[tuple[str, str | None]] = []
        for rel, content in spec.files.items():
            try:
                backup = workspace.read(rel, max_bytes=200_000) if workspace.exists(rel) else None
            except Exception:
                backup = None
            try:
                workspace.write(rel, content)
            except Exception as exc:
                log.warning("验证器文件落盘失败 %s：%s", rel, exc)
                continue
            backups.append((rel, backup))
        return backups

    @staticmethod
    def _restore_verifier_files(
        workspace: RepoWorkspace, backups: list[tuple[str, str | None]]
    ) -> None:
        """还原/删除宿主侧物化的验证器文件（agent 在同一路径写过也不受影响）。"""
        for rel, backup in backups:
            try:
                if backup is None:
                    workspace.delete(rel)
                else:
                    workspace.write(rel, backup)
            except Exception as exc:
                log.warning("恢复验证器文件失败 %s：%s", rel, exc)

    # -- 完整 F2P 流程 ----------------------------------------------------

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
        verifier_id: int | None = None,
        force: bool = False,
    ) -> VerifierRun:
        """清单型验证器：把输出交 LLM 逐条判定（Q16 的降级路径）。

        ``force``：强制修复条目改用强制性措辞（见
        :func:`~fissue.ai.prompts.checklist_judge_prompt`）。
        """
        if self.client is None:
            raise VerifierError("清单型验证器需要 LLM 客户端做判定")

        data, _ = await self.client.chat_json(
            prompts.checklist_judge_prompt(
                item, checklist=spec.checklist, output=output, force=force
            ),
            purpose=f"checklist_judge:{item.repo}",
            default={},
        )
        conclusion = str(data.get("conclusion") or "")
        items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
        runtime = [i for i in items if i.get("runtime_required")]
        code_items = [i for i in items if not i.get("runtime_required")]
        # 闸门只卡「代码可验证」项：需要真实外部服务（MySQL/网络/性能基准）的条目
        # 在沙盒里造不出运行证据，按「实现是否到位」判定、只作人工验证提示——
        # 否则功能类修复会被永远卡死（实测 MySQL 清单多条要求真实实例输出）。
        passed = (
            all(bool(i.get("satisfied")) for i in code_items)
            if code_items
            else bool(data.get("passed"))
        )
        # 退化提示：过半条目「证据不足」时，这次判定基本没有判别力——明说仅供参考，
        # 免得下游把它当成确定结论。
        if items:
            insufficient = sum(
                1 for i in items if "证据不足" in str(i.get("reason") or "")
            )
            if insufficient * 2 >= len(items):
                conclusion = f"{conclusion}；多数条目证据不足，判定仅供参考".strip("；")
        if runtime:
            idx = "、".join(str(i.get("index") or "?") for i in runtime)
            conclusion = (
                f"{conclusion}；{len(runtime)} 项需人工运行验证（第 {idx} 项）"
            ).strip("；")
        detail = "\n".join(
            (
                f"- [{i.get('satisfied')}] (需人工运行) {i.get('reason', '')}"
                if i.get("runtime_required")
                else f"- [{i.get('satisfied')}] {i.get('reason', '')}"
            )
            for i in items
        )
        run = VerifierRun(
            verifier_id=verifier_id,
            item_key=item_key,
            stage=stage,
            outcome=VerifierOutcome.PASS if passed else VerifierOutcome.FAIL,
            stdout=detail,
            stderr="",
            f2p_ok=passed,
            error=None if passed else "清单中代码可验证项未全部满足",
        )
        run.stdout = f"{conclusion}\n{detail}"
        self.repo.save_verifier_run(run)
        return run

    # -- 清单判定证据 -------------------------------------------------------

    #: 清单条目里可能出现的路径文本（**不要求 backtick**）：生成端把结构化
    #: 条目 str() 成 dict 文本后，文件名往往是裸词（pyproject.toml、
    #: docs/ENV.md），只认 backtick 会让抽取完全落空。
    _REF_PATH_RE = re.compile(
        r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.(?:toml|yaml|yml|md|txt|cfg|ini|json|lock|env"
        r"|py|ts|tsx|js|jsx|go|rs|java|kt|rb|php|cs|c|h|cpp|hpp|sh)(?![\w])"
    )
    #: 清单条目里可能被引用的代码符号（Settings / resolve_dialect …）。
    _REF_SYMBOL_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9]{2,}|[a-z][a-z0-9]*_[a-z0-9_]{2,})\b")
    _SRC_EXTS = (
        ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
        ".rb", ".php", ".cs", ".c", ".h", ".cpp",
    )

    @staticmethod
    def _item_text(entry: object) -> str:
        """清单条目可能是纯文本，也可能是生成端 ``str()`` 过的 dict 文本。"""
        return entry if isinstance(entry, str) else str(entry)

    def _checklist_text(self, spec: VerifierSpec) -> str:
        return " ".join(self._item_text(c) for c in spec.checklist)

    @staticmethod
    def _read_chunk(workspace: RepoWorkspace, rel: str, *, per_file: int = 6000) -> str:
        try:
            content = workspace.read(rel, max_bytes=per_file).strip()[:per_file]
        except Exception as exc:
            log.warning("读清单引用文件失败 %s：%s", rel, exc)
            return ""
        return f"=== {rel} ===\n{content}" if content else ""

    def _resolve_refs(self, workspace: RepoWorkspace, names: Iterable[str]) -> list[str]:
        """把路径文本解析成工作区真实文件（先精确，再唯一 basename）。"""
        files = workspace.list_files()
        by_name: dict[str, list[str]] = {}
        for f in files:
            by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)
        out: list[str] = []
        seen: set[str] = set()
        for name in names:
            name = name.strip()
            if not name or len(name) > 160 or " " in name or "://" in name:
                continue
            rel = name if name in files else None
            if rel is None:
                hits = by_name.get(name.rsplit("/", 1)[-1], [])
                rel = hits[0] if len(hits) == 1 else None
            if rel is not None and rel not in seen:
                seen.add(rel)
                out.append(rel)
        return out

    def _symbol_chunks(
        self,
        spec: VerifierSpec,
        workspace: RepoWorkspace,
        *,
        seen: set[str],
        max_files: int = 4,
        max_scanned: int = 300,
        max_bytes: int = 3_000_000,
    ) -> list[str]:
        """清单里提到的代码符号（Settings / resolve_dialect…）→ 找到定义它的文件。

        这类引用没有路径可抽取，但恰恰是「源码在哪」的答案。只认定义
        （def/class/func/function/…）不认引用，避免把每个调用点都读进来；
        扫描有文件数与总字节预算，大仓库不会被拖垮。
        """
        if not spec.checklist:
            return []
        symbols = list(dict.fromkeys(self._REF_SYMBOL_RE.findall(self._checklist_text(spec))))[:30]
        if not symbols:
            return []
        chunks: list[str] = []
        scanned = 0
        budget = max_bytes
        for rel in workspace.list_files():
            if len(chunks) >= max_files or scanned >= max_scanned or budget <= 0:
                break
            if rel in seen or not rel.lower().endswith(self._SRC_EXTS):
                continue
            try:
                content = workspace.read(rel, max_bytes=200_000)
            except Exception:
                continue
            scanned += 1
            budget -= len(content)
            for sym in symbols:
                pat = re.compile(
                    rf"(?:^|\n)\s*(?:async\s+def|def|class|func|function|interface"
                    rf"|struct|fn|pub\s+fn)\s+{re.escape(sym)}\b"
                )
                if pat.search(content):
                    chunk = self._read_chunk(workspace, rel)
                    if chunk:
                        chunks.append(chunk)
                    break
        return chunks

    def _evidence_file_chunks(
        self, spec: VerifierSpec, workspace: RepoWorkspace
    ) -> list[str]:
        """路径引用（backtick 或裸文本）优先，其次符号定义文件。"""
        if not spec.checklist:
            return []
        text = self._checklist_text(spec)
        candidates = re.findall(r"`([^`]+)`", text) + self._REF_PATH_RE.findall(text)
        chunks: list[str] = []
        seen: set[str] = set()
        for rel in self._resolve_refs(workspace, candidates)[:8]:
            chunk = self._read_chunk(workspace, rel)
            if chunk:
                chunks.append(chunk)
                seen.add(rel)
        chunks.extend(self._symbol_chunks(spec, workspace, seen=seen))
        return chunks

    def checklist_evidence(
        self,
        spec: VerifierSpec,
        workspace: RepoWorkspace | None,
        repo_context: str,
        *,
        diff: str | None = None,
    ) -> str:
        """清单判定的证据：仓库上下文 + 清单引用的文件内容 +（修复后）diff。

        清单条目大多指向具体文件内容（pyproject.toml 的依赖声明、某个解析函数
        等）。只给文件树 + README 时 LLM 只能逐条回「证据不足」，判定退化成
        常量——实测（MySQL 功能请求 16 项全部「证据不足」）即此因。抽取两条腿：
        路径文本（含裸文件名）与代码符号定义文件；修复后（fix 阶段）判定再附上
        diff，LLM 才能看到「到底改了什么」。
        """
        parts: list[str] = []
        if repo_context:
            parts.append(repo_context)
        if workspace is not None:
            chunks = self._evidence_file_chunks(spec, workspace)
            if chunks:
                parts.append("清单引用的文件（相关摘录）：\n" + "\n\n".join(chunks))
        if diff:
            parts.append("本次改动 diff：\n" + diff[:8000])
        return "\n\n".join(parts) if parts else "（无证据）"

    async def _judge_checklist(
        self,
        item: RawItem,
        verifier_id: int,
        spec: VerifierSpec,
        repo_context: str,
        workspace: RepoWorkspace | None = None,
    ) -> VerifierResult:
        """清单型：把证据交 LLM 逐条判定（Q16 的降级路径）。

        证据由 :meth:`checklist_evidence` 组装——仓库上下文之外，再把清单
        条目引用的文件内容读进来（只给文件树时 LLM 只能逐条回「证据不足」，
        判定退化成常量）。base 上判「未全部满足」（FAIL）符合
        ``expect_fail_on_base`` 的预期方向；base 上判「全部满足」（PASS）
        说明问题可能已解决，下游按「未复现」对待。逐条明细已由
        :meth:`verify_checklist` 持久化，可在验证记录里查看。
        """
        if self.client is None:
            return VerifierResult(
                kind=spec.kind,
                f2p_satisfied=False,
                conclusion="清单型验证器：无 LLM 客户端，无法逐条判定，转人工复核",
            )
        evidence = self.checklist_evidence(spec, workspace, repo_context)
        force = bool(item.force_fix and self.settings.verifier.checklist_force_prompt)
        run = await self.verify_checklist(
            item_key=item.key,
            item=item,
            spec=spec,
            output=evidence,
            stage="base",
            force=force,
            verifier_id=verifier_id,
        )
        llm_conclusion = (run.stdout or "").split("\n", 1)[0]
        if run.outcome is VerifierOutcome.FAIL:
            conclusion = f"清单判定（base）：未全部满足 — {llm_conclusion}"
        else:
            conclusion = f"清单判定（base）：全部满足 — {llm_conclusion}"
        return VerifierResult(
            kind=spec.kind,
            base_run=run,
            f2p_satisfied=False,
            conclusion=conclusion,
        )

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
                # 清单型没有可执行命令：把证据（仓库上下文 + 清单引用的文件内容）
                # 交 LLM 逐条判定。判定结论只是人工复核的参考，不构成 F2P 证明
                # （f2p 仍为 false）。
                result = await self._judge_checklist(
                    item, verifier_id, spec, repo_context, workspace
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
            self_error = verifier_self_error(base, verifier_files=tuple(spec.files))
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

            # 自称「回归断言」却在未修复代码上就失败 = 自相矛盾。必须在判定
            # 「可复现」**之前**拦下：它的 base 确实失败了，不拦就会被当成一条
            # 合格的复现证据（pipekit #8 实测：该验证器数学上不可满足，
            # Agent 把两处缺陷都改对了、仓库基线全绿，仍永远过不了，空转到上限）。
            conflict = regression_assertion_conflict(base)
            if conflict is not None:
                log.warning("验证器回归断言自相矛盾 %s（第 %d 轮）：%s", item.key, round_index, conflict)
                result.conclusion = conflict
                if round_index >= max_rounds:
                    self.repo.set_verifier_f2p(verifier_id, False)
                    # 自相矛盾 ≠ 复现：必须摘掉 base_run，否则 reproducible 仍为真，
                    # 下游会把一条不可满足的验证器当成「已复现」推进。
                    result.base_run = None
                    return verifier_id, spec, result
                refined = await generator.refine(
                    spec,
                    item=item,
                    repo_context=repo_context,
                    failure_output=(
                        f"{conflict}\n\n=== 原始输出 ===\n{(base.stdout or '')}\n{(base.stderr or '')}"
                    ),
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
        if result.reproducible or result.f2p_satisfied:
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
