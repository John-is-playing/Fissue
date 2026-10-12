"""自动修复 Agent 循环（Q10 选 B）。

工作方式
--------
不是让模型一次性吐一个大 patch，而是把它放进**受控的循环**里：

::

    读文件 ──► 模型决定下一步 ──► 写文件 / 跑验证器 ──► 看结果 ──┐
       ▲                                                       │
       └─────────────────── 未通过则继续 ◄──────────────────────┘

关键约束（都在宿主侧强制执行，模型说了不算）
* **文件写入**：只允许仓库内相对路径；命中 ``protected_paths`` 直接拒绝；
  改动文件数与 diff 行数超限即中止。
* **验证器不可篡改**：验证器文件在修复期间被锁定，模型写入请求会被丢弃。
* **失败降级**：达到最大轮次仍不通过 → 按 ``auto_fix.on_failure`` 生成
  「需人工」报告（Q12 选 B），绝不硬提 PR。
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..ai import prompts
from ..ai.client import LLMClient, Usage
from ..config import AutoFixConfig, Settings
from ..errors import FissueError, PolicyError
from ..logging_setup import get_logger
from ..models import (
    FixAttempt,
    FixOutcome,
    ItemStatus,
    RawItem,
    VerifierOutcome,
    VerifierSpec,
)
from ..store.repository import Repository
from ..verifier.runner import VerifierRunner, failed_test_ids_from_output
from ..workspace import RepoWorkspace

log = get_logger(__name__)

AGENT_STAGE = "agent"


class FixPolicyViolation(PolicyError):
    """违反修复策略（改动过大 / 触碰保护路径等）。"""


@dataclass
class AgentTrace:
    """Agent 循环的可审计轨迹。"""

    rounds: int = 0
    actions: list[str] = field(default_factory=list)
    written_files: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    test_outputs: list[str] = field(default_factory=list)
    #: 上一轮验证器输出的指纹，与 :attr:`identical_output_streak` 一起用于给出提示。
    last_verifier_digest: str | None = None
    #: 验证器输出**逐字节**相同的轮数（首次出现记 1）。
    identical_output_streak: int = 0
    #: 最近一轮失败的用例集合（pytest 的 ``FAILED ...::id``）。
    failed_tests: tuple[str, ...] = ()
    #: 迄今见到的最少失败用例数（``None`` 表示还没跑过失败的验证器）。
    best_failure_count: int | None = None
    #: 连续多少轮**没有刷新最少失败数**。这是判定停滞的主判据。
    no_progress_streak: int = 0
    usage: Usage = field(default_factory=Usage)
    summary: str = ""
    give_up_reason: str | None = None

    def note(self, text: str) -> None:
        self.actions.append(text)
        if len(self.actions) > 200:
            del self.actions[:100]


class FixAgent:
    """在受控沙盒循环里让模型修复代码。"""

    def __init__(
        self,
        client: LLMClient,
        repo: Repository,
        runner: VerifierRunner,
        settings: Settings,
    ) -> None:
        self.client = client
        self.repo = repo
        self.runner = runner
        self.settings = settings
        self.cfg: AutoFixConfig = settings.auto_fix

    # -- 主流程 -----------------------------------------------------------

    async def run(
        self,
        *,
        item: RawItem,
        workspace: RepoWorkspace,
        spec: VerifierSpec,
        repo_context: str,
        verifier_id: int | None = None,
        max_rounds: int | None = None,
    ) -> tuple[FixAttempt, AgentTrace]:
        """跑 Agent 循环，直到验证器通过或放弃。"""
        rounds_limit = max_rounds or self.cfg.agent_max_rounds
        trace = AgentTrace()
        attempt = FixAttempt(item_key=item.key, outcome=FixOutcome.SKIPPED)

        if not self.cfg.enabled:
            trace.summary = "自动修复未启用（auto_fix.enabled=false）"
            return attempt, trace

        locked = set(spec.files.keys())          # 验证器文件在修复期间锁定
        read_files: dict[str, str] = {}
        last_output: str | None = None

        for round_index in range(1, rounds_limit + 1):
            trace.rounds = round_index
            read_files = self._pick_context(workspace, read_files)

            data, usage = await self.client.chat_json(
                prompts.fix_agent_step_prompt(
                    item,
                    repo_context=repo_context,
                    verifier_command=spec.command or "(无)",
                    last_test_output=last_output,
                    round_index=round_index,
                    max_rounds=rounds_limit,
                    read_files=read_files,
                ),
                purpose=f"agent@{item.repo}",
                default={},
            )
            self._accumulate(trace, usage)

            action = str(data.get("action") or "").lower()
            trace.note(f"r{round_index}: {action}")

            if action == "read":
                paths = [str(p) for p in (data.get("paths") or []) if str(p).strip()]
                for p in paths[:10]:
                    content = self._safe_read(workspace, p, trace)
                    if content is not None:
                        read_files[p] = content
                continue

            if action == "write":
                files = data.get("files")
                if not isinstance(files, dict) or not files:
                    trace.note("write 动作未提供 files")
                    continue
                written, err = self._apply_writes(workspace, files, locked=locked, trace=trace)
                if err:
                    last_output = f"写入被拒绝：{err}"
                    continue
                trace.written_files.extend(written)
                # 写完立刻跑一次验证器；**通过即收工**，不必等模型再回 done
                last_output = await self._run_verifier(
                    item, workspace, spec, trace, verifier_id=verifier_id, round_index=round_index
                )
                finished, last_output = self._after_verifier_run(
                    attempt, trace, workspace, round_index, last_output
                )
                if finished is not None:
                    return finished
                continue

            if action == "run":
                last_output = await self._run_verifier(
                    item, workspace, spec, trace, verifier_id=verifier_id, round_index=round_index
                )
                finished, last_output = self._after_verifier_run(
                    attempt, trace, workspace, round_index, last_output
                )
                if finished is not None:
                    return finished
                continue

            if action == "give_up":
                trace.give_up_reason = str(data.get("summary") or data.get("reason") or "模型主动放弃")
                trace.summary = trace.give_up_reason
                attempt.outcome = FixOutcome.FAILED
                attempt.rounds = round_index
                attempt.error = trace.give_up_reason
                return self._finalize(attempt, trace, workspace)

            if action == "done":
                last_output = await self._run_verifier(
                    item, workspace, spec, trace, verifier_id=verifier_id, round_index=round_index
                )
                passed = self._last_passed(trace)
                if passed:
                    trace.summary = str(data.get("summary") or "验证器通过")
                    attempt.outcome = FixOutcome.SUCCESS
                    attempt.rounds = round_index
                    return self._finalize(attempt, trace, workspace)
                stalled = self._stall_stop(attempt, trace, workspace)
                if stalled is not None:
                    return stalled
                trace.note("模型声称完成但验证器未通过，继续修复")
                last_output = self._maybe_escalate(trace, last_output)
                continue

            trace.note(f"未识别的动作：{action}")
            last_output = "你的上一轮输出缺少合法的 action 字段，请严格按 JSON 格式回复。"

        # 轮次耗尽
        attempt.outcome = FixOutcome.FAILED
        attempt.rounds = trace.rounds
        if self._last_passed(trace):
            # 验证器最后一跑是过的，只是模型没回 done 就耗尽了轮次。
            # 这不是「修复失败」——不可报「仍未通过验证器」误导人工复核。
            attempt.error = f"达到最大轮次（{rounds_limit}），验证器已通过但模型未给出 done"
        else:
            attempt.error = f"达到最大轮次（{rounds_limit}）仍未通过验证器"
        trace.summary = attempt.error
        return self._finalize(attempt, trace, workspace)

    # -- 写入控制 ---------------------------------------------------------

    def _apply_writes(
        self,
        workspace: RepoWorkspace,
        files: dict[str, Any],
        *,
        locked: set[str],
        trace: AgentTrace,
    ) -> tuple[list[str], str | None]:
        """执行模型的文件写入请求，带全部策略检查。"""
        written: list[str] = []

        if not self.cfg.allow_file_write:
            # 配置层禁止改动工作区：一个文件都不写。补丁仍由 workspace.diff() 产出，
            # 所以「只出补丁、不动工作区」这条配置依然能给人一份可采用的产物。
            reason = "auto_fix.allow_file_write=false：按配置不改动工作区"
            trace.rejected.append(reason)
            return [], reason

        for raw_path, content in files.items():
            path = str(raw_path).replace("\\", "/").lstrip("/")
            if not isinstance(content, str):
                trace.rejected.append(f"{path}: 内容非字符串")
                continue

            # 1) 验证器文件锁定
            if path in locked or self._is_verifier_path(path, locked):
                trace.rejected.append(f"{path}: 验证器文件已锁定")
                continue

            # 2) 保护路径
            if self._is_protected(path):
                trace.rejected.append(f"{path}: 命中保护路径")
                continue

            # 3) 单文件过大
            if len(content) > 1_000_000:
                trace.rejected.append(f"{path}: 文件过大（{len(content)} 字符）")
                continue

            try:
                workspace.write(path, content)
                written.append(path)
            except FissueError as exc:
                trace.rejected.append(f"{path}: {exc}")

        if not written:
            return [], "没有任何文件被写入（" + "；".join(trace.rejected[-3:]) + "）"

        # 4) 改动规模限制
        changed = workspace.changed_files()
        if len(changed) > self.cfg.max_changed_files:
            removed = self._rollback_new_files(workspace, written)
            return [], (
                f"改动文件数 {len(changed)} 超过上限 {self.cfg.max_changed_files}"
                + (f"（已回滚本轮新增的 {len(removed)} 个文件）" if removed else "（已回滚本轮写入）")
            )

        lines = self._diff_lines(workspace)
        if lines > self.cfg.max_diff_lines:
            self._rollback_new_files(workspace, written)
            return [], f"diff 行数 {lines} 超过上限 {self.cfg.max_diff_lines}（已回滚本轮写入）"

        return written, None

    def _rollback_new_files(self, workspace: RepoWorkspace, written: list[str]) -> list[str]:
        """回滚本轮写入的**新增**文件。

        已存在的文件被覆盖时无法简单还原（git 里仍可 checkout），但新增的未跟踪文件
        会污染后续 diff 与统计，必须删掉；否则超限检查会不断被自己的残留触发。
        """
        removed: list[str] = []
        for path in written:
            try:
                # 只删「未被 git 跟踪」的路径，避免误删仓库原有文件
                tracked = workspace._git(["git", "ls-files", "--error-unmatch", path])
                if tracked.ok:
                    workspace._git(["git", "checkout", "--", path])
                    continue
                if workspace.delete(path):
                    removed.append(path)
            except Exception as exc:  # pragma: no cover
                log.debug("回滚 %s 失败：%s", path, exc)
        return removed

    def _is_verifier_path(self, path: str, locked: set[str]) -> bool:
        """判断是否在写验证器目录（防篡改兜底）。"""
        for lk in locked:
            base = lk.rsplit("/", 1)[0] if "/" in lk else ""
            if base and path.startswith(base + "/"):
                return True
        return any(k in path.lower() for k in ("fissue_verifier", "verify_f2p"))

    def _is_protected(self, path: str) -> bool:
        for pattern in self.cfg.protected_paths:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch("/" + path, pattern):
                return True
            # 目录形式（``.github/**`` 也匹配 ``.github/x``）
            if pattern.endswith("/**") and path.startswith(pattern[:-3].rstrip("/") + "/"):
                return True
        return False

    def _diff_lines(self, workspace: RepoWorkspace) -> int:
        """统计当前 diff 的新增+删除行数（含未跟踪文件）。"""
        try:
            workspace._git(["git", "add", "-N", "."])      # intent-to-add，让未跟踪文件进 diff
            r = workspace._git(["git", "diff", "--numstat"], timeout=120)
            if not r.ok:
                return 0
            total = 0
            for line in r.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                for cell in parts[:2]:
                    try:
                        total += int(cell)
                    except ValueError:
                        continue          # 二进制文件显示为 '-'
            return total
        except Exception:  # pragma: no cover
            return 0

    # -- 上下文选择 -------------------------------------------------------

    def _pick_context(self, workspace: RepoWorkspace, current: dict[str, str], *, limit: int = 6) -> dict[str, str]:
        """给模型一个最小上下文：优先 README 与变更相关文件。"""
        if len(current) >= limit:
            return dict(list(current.items())[:limit])

        chosen = dict(current)
        for name in ("README.md", "README.rst", "CONTRIBUTING.md", "pyproject.toml", "setup.py", "package.json"):
            if name in chosen or len(chosen) >= limit:
                continue
            content = self._safe_read(workspace, name, None)
            if content:
                chosen[name] = content[:6000]
        return chosen

    def _safe_read(self, workspace: RepoWorkspace, path: str, trace: AgentTrace | None) -> str | None:
        try:
            if not workspace.exists(path):
                if trace is not None:
                    trace.note(f"读文件失败：{path} 不存在")
                return None
            return workspace.read(path, max_bytes=120_000)
        except Exception as exc:
            if trace is not None:
                trace.note(f"读文件失败 {path}：{exc}")
            return None

    # -- 验证器执行 -------------------------------------------------------

    async def _run_verifier(
        self,
        item: RawItem,
        workspace: RepoWorkspace,
        spec: VerifierSpec,
        trace: AgentTrace,
        *,
        verifier_id: int | None,
        round_index: int,
    ) -> str:
        run = await self.runner.run_once(
            item_key=item.key,
            workspace=workspace,
            spec=spec,
            stage=AGENT_STAGE,
            label=f"{item.key}:agent:r{round_index}",
            verifier_id=verifier_id,
        )
        output = f"[exit={run.exit_code} outcome={run.outcome.value}]\n{run.stdout}\n{run.stderr}"
        trace.test_outputs.append(output[:4000])
        self._note_verifier_progress(trace, output, passed=self._last_passed(trace))
        return output[:6000]

    def _note_verifier_progress(self, trace: AgentTrace, output: str, *, passed: bool) -> None:
        """记录本轮验证器进展，用于识别「原地打转」的停滞。

        判据不是「输出逐字节相同」，而是**失败用例数有没有刷新更少**。
        否则会漏掉真实的停滞形态：输出里带容器临时路径（``/tmp/...``）与耗时，
        每轮字节都不同，但失败的是**同一个用例**——demo 实测 pipekit #3 连跑
        11 轮，输出 sha 每轮都变，失败用例却始终是 ``test_count_batches_endpoint_overlap``。
        """
        digest = hashlib.sha1(output.encode("utf-8")).hexdigest()
        if digest == trace.last_verifier_digest:
            trace.identical_output_streak += 1
        else:
            trace.last_verifier_digest = digest
            trace.identical_output_streak = 1

        if passed:
            trace.no_progress_streak = 0
            return

        failed = failed_test_ids_from_output(output)
        trace.failed_tests = failed
        count = len(failed)

        # 「有进展」= 失败用例数刷新更少。拿不到用例名时（非 pytest 形态、
        # 输出被截断），退回「输出是否发生变化」——判据必须保守，没有证据
        # 说明「没进展」就不能掐断循环。
        improved = trace.best_failure_count is None or count < trace.best_failure_count
        if count == 0:
            improved = improved or trace.identical_output_streak == 1

        if improved:
            trace.best_failure_count = count
            trace.no_progress_streak = 0
        else:
            trace.no_progress_streak += 1

    def _last_passed(self, trace: AgentTrace) -> bool:
        if not trace.test_outputs:
            return False
        last = trace.test_outputs[-1]
        return last.startswith("[exit=0") or "outcome=pass" in last[:60]

    def _stall_rounds(self) -> int:
        """停滞闸门的轮数阈值（``<=1`` 表示关闭）。"""
        try:
            return int(getattr(self.cfg, "stall_rounds", 4))
        except (TypeError, ValueError):  # pragma: no cover - 配置异常时用默认值
            return 4

    def _maybe_escalate(self, trace: AgentTrace, last_output: str) -> str:
        """出现停滞迹象时，给模型一次**改变思路**的明确提示。

        仅在 ``no_progress_streak == 2`` 时提示一次：否则会把同一段话重复塞进
        每一轮上下文。提示里带上仍然失败的用例名——那正是「没有进展」的具体证据。

        这条提示是 C（调用链注入）的**兜底**：只把调用链摊在上下文里是「被动信息」，
        模型可能视而不见（pipekit #3 实测：链就在上下文里，Agent 仍连续 5 轮只改
        同一个文件）。这里把「换文件」说成一条**必须执行的动作**，并明确禁止
        重复同一个改动。
        """
        if trace.no_progress_streak != 2:
            return last_output
        stuck = "、".join(trace.failed_tests[:3]) or "（未能识别用例名）"
        return (
            last_output
            + "\n\n⚠️ 注意：连续两轮验证器输出没有任何进展"
            f"（仍然失败的用例：{stuck}）。说明你上一轮的改动**没有产生效果**。\n"
            "请立刻换一个**不同的文件**去改，不要在同一个文件上重复同一个改动：\n"
            "1) 在这个失败的用例里找到它**实际调用**的函数（不是测试文件本身）；\n"
            "2) 沿着仓库上下文给出的『跨模块调用链』往下一层走，检查**被调用方**的\n"
            "   输入输出是否符合它的文档语义（例如：它问的到底是分片的**起点**还是**终点**？\n"
            "   区间端点算不算在内？）；\n"
            "3) 如果链上多处都存在同类语义问题，**每一处都要分别修正**——\n"
            "   改好一处并不能顺带修好另一处。"
        )

    def _stall_stop(
        self,
        attempt: FixAttempt,
        trace: AgentTrace,
        workspace: RepoWorkspace,
    ) -> tuple[FixAttempt, AgentTrace] | None:
        """连续多轮「失败用例数没有刷新更少」→ 判定停滞，提前收工。

        选这个判据而不是「输出逐字节相同」：输出里带容器临时路径与耗时，
        逐字节比较会被噪声骗过。demo 实测 pipekit #3 连跑 11 轮，每轮输出 sha
        都不同，失败的却始终是同一个用例 ``test_count_batches_endpoint_overlap``。

        与轮次耗尽的区别必须体现在 ``error`` 里：停滞是「没有进展」，
        不是「修了很多次没修好」。把两者混为一谈会误导人工复核。
        """
        limit = self._stall_rounds()
        if limit <= 1 or trace.no_progress_streak < limit:
            return None
        stuck = "、".join(trace.failed_tests[:3]) or "（未能识别用例名）"
        reason = (
            f"连续 {trace.no_progress_streak} 轮没有任何进展（失败用例数未再减少，"
            f"仍为 {len(trace.failed_tests)} 条：{stuck}），判定为停滞"
            f"（第 {trace.rounds} 轮提前收工，上限 {limit} 轮）"
        )
        trace.give_up_reason = reason
        trace.summary = reason
        attempt.outcome = FixOutcome.FAILED
        attempt.rounds = trace.rounds
        attempt.error = reason
        return self._finalize(attempt, trace, workspace)

    def _after_verifier_run(
        self,
        attempt: FixAttempt,
        trace: AgentTrace,
        workspace: RepoWorkspace,
        round_index: int,
        last_output: str,
    ) -> tuple[tuple[FixAttempt, AgentTrace] | None, str]:
        """验证器刚跑完后的统一处置：先看是否通过，再看是否停滞。

        通过优先于停滞——「已经修好」永远是最该收工的理由。
        两者都没有时，若输出与上一轮完全一致，额外给模型一次改变思路的提示
        （否则模型会拿到同一份输出、做出同一动作，一路空转）。
        """
        finished = self._success_if_passed(attempt, trace, workspace, round_index)
        if finished is not None:
            return finished, last_output
        stalled = self._stall_stop(attempt, trace, workspace)
        if stalled is not None:
            return stalled, last_output
        return None, self._maybe_escalate(trace, last_output)

    def _success_if_passed(
        self,
        attempt: FixAttempt,
        trace: AgentTrace,
        workspace: RepoWorkspace,
        round_index: int,
    ) -> tuple[FixAttempt, AgentTrace] | None:
        """验证器刚跑过且通过 → 立即收工（否则要等模型额外回一次 done）。

        为什么不等 done：模型在 write/run 之后常继续反复写同一个文件（它只被告知
        「第 N/12 轮」，没人告诉它已经过了），一路撞到轮次上限，最后报出与事实相反的
        「仍未通过验证器」。demo 实测 #1 就是 11 次验证器全 pass 却报失败。
        验证器才是权威判据，且提 PR 前还有一道完整 F2P 复核兜底。
        """
        if not self._last_passed(trace):
            return None
        trace.summary = "验证器通过"
        attempt.outcome = FixOutcome.SUCCESS
        attempt.rounds = round_index
        return self._finalize(attempt, trace, workspace)

    # -- 收尾 -------------------------------------------------------------

    def _finalize(self, attempt: FixAttempt, trace: AgentTrace, workspace: RepoWorkspace) -> tuple[FixAttempt, AgentTrace]:
        try:
            attempt.diff = workspace.diff()
            if not attempt.diff:
                # 未跟踪文件不进 git diff，用 add -N 后再取
                workspace._git(["git", "add", "-N", "."])
                attempt.diff = workspace.diff()
            attempt.changed_files = workspace.changed_files()
        except Exception as exc:  # pragma: no cover
            log.warning("收集 diff 失败：%s", exc)
        attempt.rounds = trace.rounds
        return attempt, trace

    def _accumulate(self, trace: AgentTrace, usage: Usage) -> None:
        trace.usage.purpose = "agent"
        trace.usage.model = usage.model
        trace.usage.prompt_tokens += usage.prompt_tokens
        trace.usage.completion_tokens += usage.completion_tokens
        trace.usage.cost_usd += usage.cost_usd


def branch_name(prefix: str, item: RawItem, *, salt: str = "") -> str:
    """生成分支名（可读 + 防冲突）。"""
    digest = hashlib.sha1(f"{item.key}:{salt}".encode("utf-8")).hexdigest()[:6]
    slug = item.repo.split("/")[-1][:20]
    return f"{prefix}{item.number}-{slug}-{digest}"
