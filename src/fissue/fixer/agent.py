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
from ..verifier.runner import VerifierRunner
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
                # 写完立刻跑一次验证器，拿到反馈给下一轮
                last_output = await self._run_verifier(
                    item, workspace, spec, trace, verifier_id=verifier_id, round_index=round_index
                )
                continue

            if action == "run":
                last_output = await self._run_verifier(
                    item, workspace, spec, trace, verifier_id=verifier_id, round_index=round_index
                )
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
                trace.note("模型声称完成但验证器未通过，继续修复")
                continue

            trace.note(f"未识别的动作：{action}")
            last_output = "你的上一轮输出缺少合法的 action 字段，请严格按 JSON 格式回复。"

        # 轮次耗尽
        attempt.outcome = FixOutcome.FAILED
        attempt.rounds = trace.rounds
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
        return output[:6000]

    def _last_passed(self, trace: AgentTrace) -> bool:
        if not trace.test_outputs:
            return False
        last = trace.test_outputs[-1]
        return last.startswith("[exit=0") or "outcome=pass" in last[:60]

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
