"""自动修复编排：修复 → 验证 → 提 PR（Q11/Q12/Q13）。

策略（按用户选择）
------------------
* ``pr_strategy.mode = fork``（Q11 选 A）：优先 fork 上游 → 在 fork 上开分支推送 →
  向上游提 PR。
* ``auto_submit = true``（Q13 选 C）：修复成功即全自动提交，靠 **AI 标签** 标识身份；
  失败/超轮次则**不提 PR**，转而生成「需人工」报告（Q12 选 B）。
* 保护路径、改动规模、验证器 F2P 全部通过才允许提 PR。

安全细节
--------
* 推送用**一次性内嵌令牌的 URL**，绝不写入 git remote 配置，输出统一过 :meth:`sanitize`。
* 分支名带内容指纹，避免重复推送冲突。
* PR 描述明确标注「AI 自动生成 + 验证方式 + 验证结果」，不夸大。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ai import prompts
from ..config import RepoConfig, Settings
from ..errors import PolicyError, FissueError
from ..logging_setup import get_logger
from ..models import (
    FixAttempt,
    FixOutcome,
    ItemStatus,
    ItemType,
    Priority,
    RawItem,
    VerifierKind,
    VerifierSpec,
)
from ..platforms.registry import parse_repo_ref
from ..verifier.regression import REGRESSION_STAGE_BASE
from ..verifier.runner import VerifyContext, judge_regression
from ..workspace import RepoWorkspace
from ..pipeline.context import RuntimeContext
from .agent import AgentTrace, FixAgent, branch_name

log = get_logger(__name__)


def _write_patch(path: Path, diff: str) -> None:
    """以**二进制、纯 LF** 落盘补丁。

    ``.patch`` 是给 ``git apply`` 消费的字节级产物，不是给人读的文本：
    Windows 上 ``write_text`` 会把 ``\\n`` 转成 ``\\r\\n``，补丁每行多一个 CR，
    上下文行匹配不上 → ``patch does not apply``（远端 diff 本身是纯 LF）。
    """
    data = diff.encode("utf-8").replace(b"\r\n", b"\n")
    path.write_bytes(data)


@dataclass
class FixReport:
    """一批修复的结果。

    分开计数，避免把「没成功」「策略跳过」「dry-run」统统塞进「失败」——
    那会让报告里的数字完全不可信（dry-run 下成功必然被报成失败，因为 dry-run
    本来就不提 PR）。

    dry-run 下「补丁已产出」的尝试会**同时**计入 ``succeeded`` 与
    ``dry_run_patches``：汇总以「成功」表述，明细则以同一口径显示为成功态，
    避免出现「汇总说成功 6、明细说需人工 6」的自相矛盾。
    """

    attempted: int = 0
    succeeded: int = 0
    needs_manual: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run_patches: int = 0                                       # 其中「dry-run 仅产出补丁」的条数
    prs: list[tuple[str, str]] = field(default_factory=list)      # (item_key, pr_url)
    attempts: list[FixAttempt] = field(default_factory=list)

    def record(self, attempt: FixAttempt, *, dry_run: bool = False) -> None:
        """按 outcome 归类一次尝试。"""
        if attempt.outcome is FixOutcome.SUCCESS:
            self.succeeded += 1
        elif attempt.outcome is FixOutcome.SKIPPED:
            self.skipped += 1
        elif attempt.outcome is FixOutcome.NEEDS_MANUAL:
            # dry-run 下「未提 PR」是**设计如此**：补丁已产出即说明修复本身成功
            # （genuine 失败走 _handle_failure，产出的是 report_path 而非 patch_path）
            if dry_run and attempt.patch_path:
                self.succeeded += 1
                self.dry_run_patches += 1
            else:
                self.needs_manual += 1
        else:
            self.failed += 1

    @property
    def summary(self) -> str:
        text = (
            f"修复尝试 {self.attempted}，成功 {self.succeeded}，需人工 {self.needs_manual}，"
            f"跳过 {self.skipped}，失败 {self.failed}，产出 PR {len(self.prs)}"
        )
        if self.dry_run_patches:
            text += f"（其中 dry-run 产出补丁 {self.dry_run_patches}）"
        return text


class PRCreator:
    """负责推送分支与创建 PR。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings

    async def submit(
        self,
        *,
        attempt: FixAttempt,
        trace: AgentTrace,
        item: RawItem,
        workspace: RepoWorkspace,
        repo_cfg: RepoConfig,
        spec: VerifierSpec,
        f2p_ok: bool,
        diff: str,
    ) -> FixAttempt:
        """把修复推送并提 PR。"""
        strategy = self.settings.auto_fix.pr_strategy
        adapter = await self.ctx.adapter_for_repo(repo_cfg)
        ref = parse_repo_ref(item.repo, item.platform)

        # 生成 PR 文案
        title, body = await self._compose_pr_text(
            item, trace=trace, spec=spec, f2p_ok=f2p_ok
        )

        if strategy.mode == "patch_only":
            path = self._save_patch(item, diff)
            attempt.outcome = FixOutcome.NEEDS_MANUAL
            attempt.patch_path = path
            attempt.error = "pr_strategy.mode=patch_only：只产出补丁，不自动提 PR"
            return attempt

        branch = branch_name(strategy.branch_prefix, item)

        # 1) 决定推送目标
        if strategy.mode == "fork":
            fork = await self._ensure_fork(item, repo_cfg)
            head_repo = fork or repo_cfg
            head_prefix = f"{head_repo.owner}:{branch}"
        else:
            head_repo = repo_cfg
            head_prefix = branch

        # 2) 提交并推送
        pushed, push_err = await self._commit_and_push(
            workspace, item=item, branch=branch, head_repo=head_repo, trace=trace
        )
        if not pushed:
            attempt.outcome = FixOutcome.NEEDS_MANUAL
            attempt.branch = branch
            attempt.error = f"推送失败：{push_err}"
            return attempt

        # 3) 提 PR
        try:
            result = await adapter.create_pr(
                ref,
                head=head_prefix,
                base=item.base_branch or repo_cfg.base_branch or "main",
                title=title,
                body=body,
                draft=strategy.draft,
            )
        except Exception as exc:
            attempt.outcome = FixOutcome.NEEDS_MANUAL
            attempt.branch = branch
            attempt.fork = f"{head_repo.owner}/{head_repo.name}"
            attempt.error = f"创建 PR 失败（分支已推送）：{exc}"
            log.error("创建 PR 失败 %s：%s", item.key, exc)
            return attempt

        attempt.branch = branch
        attempt.fork = f"{head_repo.owner}/{head_repo.name}" if head_repo.slug != repo_cfg.slug else None
        attempt.pr_number = result.get("number")
        attempt.pr_url = result.get("url")
        attempt.outcome = FixOutcome.SUCCESS

        # 4) 打 AI 标签（标识这是 AI 产物）
        await self._label_pr(item, repo_cfg, result.get("number"), adapter)
        log.info("已提 PR %s → %s", item.key, attempt.pr_url)
        return attempt

    # -- fork -------------------------------------------------------------

    async def _ensure_fork(self, item: RawItem, repo_cfg: RepoConfig) -> RepoConfig | None:
        """获取/创建 fork 仓库。"""
        adapter = await self.ctx.adapter_for_repo(repo_cfg)
        ref = parse_repo_ref(item.repo, item.platform)
        try:
            fork_ref = await adapter.fork_repo(ref)
            # fork 可能需要一点时间才可用
            await asyncio.sleep(2)
            return RepoConfig(
                platform=fork_ref.platform,
                owner=fork_ref.owner,
                name=fork_ref.name,
                base_branch=repo_cfg.base_branch,
                api_base=repo_cfg.api_base,
            )
        except Exception as exc:
            log.warning("fork 失败（回退为直接推送上游分支）：%s", exc)
            return None

    # -- git 操作 ---------------------------------------------------------

    async def _commit_and_push(
        self,
        workspace: RepoWorkspace,
        *,
        item: RawItem,
        branch: str,
        head_repo: RepoConfig,
        trace: AgentTrace,
    ) -> tuple[bool, str]:
        """在工作区分支上提交并推送。"""
        adapter = await self.ctx.adapter_for_repo(head_repo)
        ref = parse_repo_ref(f"{head_repo.owner}/{head_repo.name}", head_repo.platform)

        ok = await asyncio.to_thread(workspace.checkout, branch, create=True)
        if not ok:
            return False, f"创建分支 {branch} 失败"

        staged = await asyncio.to_thread(workspace.stage_all)
        if not staged:
            return False, "git add 失败（没有可提交的改动？）"

        message = (
            f"fix: 修复 #{item.number} {item.title[:60]}\n\n"
            f"由 Fissue AI 自动修复生成。\n"
            f"验证方式：{trace.summary[:200] or '验证器 fail-to-pass'}\n"
            f"改动文件：{', '.join(trace.written_files[:10]) or '(见 diff)'}\n"
        )
        commit = await asyncio.to_thread(workspace.commit, message)
        if not commit.ok:
            if "nothing to commit" in (commit.stdout + commit.stderr).lower():
                return False, "没有产生任何改动，无法提交"
            return False, f"提交失败：{commit.stderr[:300]}"

        push_url = adapter.clone_url(ref, use_token=True)
        pushed = await asyncio.to_thread(workspace.push, push_url, branch, force=True)
        if not pushed.ok:
            return False, adapter.sanitize(pushed.stderr)[:400]
        return True, ""

    # -- 文案 -------------------------------------------------------------

    async def _compose_pr_text(
        self,
        item: RawItem,
        *,
        trace: AgentTrace,
        spec: VerifierSpec,
        f2p_ok: bool,
    ) -> tuple[str, str]:
        """生成 PR 标题与正文（LLM 优先，失败则用模板）。"""
        default_title = f"fix: 修复 #{item.number} {item.title[:70]}"
        try:
            data, _usage = await self.ctx.llm.chat_json(
                prompts.fix_pr_body_prompt(
                    item,
                    summary=trace.summary or "自动修复",
                    changed_files=trace.written_files,
                    verifier_command=spec.command,
                    f2p_ok=f2p_ok,
                ),
                purpose="fix_pr_body",
                default={},
            )
            title = str(data.get("title") or default_title)[:120]
            body = str(data.get("body") or "")
            if body.strip():
                return title, self._decorate_body(body, item, spec, f2p_ok)
        except Exception as exc:
            log.warning("生成 PR 文案失败（用模板兜底）：%s", exc)

        body = self._template_body(item, trace=trace, spec=spec, f2p_ok=f2p_ok)
        return default_title, body

    def _decorate_body(self, body: str, item: RawItem, spec: VerifierSpec, f2p_ok: bool) -> str:
        """确保 AI 标识与验证信息一定出现在正文里（不依赖模型）。"""
        footer = (
            "\n\n---\n"
            "> 🤖 本 PR 由 **Fissue** 自动生成，请人工复核后再合并。\n"
            f"> 关联 Issue：#{item.number}\n"
            f"> 验证器：`{spec.name}`（{spec.kind.value}）\n"
            f"> fail-to-pass：{'✅ 通过' if f2p_ok else '❌ 未通过'}\n"
        )
        if "Fissue" in body:
            return body
        return body + footer

    def _template_body(self, item: RawItem, *, trace: AgentTrace, spec: VerifierSpec, f2p_ok: bool) -> str:
        lines = [
            f"## 修复内容\n\n自动修复了 #{item.number}：{item.title}\n",
            "## 验证\n",
            f"- 验证器：`{spec.name}`（{spec.kind.value}）",
            f"- 运行命令：`{spec.command or '(清单型)'}`",
            f"- fail-to-pass：{'✅ 通过' if f2p_ok else '❌ 未通过'}",
        ]
        if trace.written_files:
            lines.append("\n## 改动文件\n")
            lines.extend(f"- `{p}`" for p in trace.written_files[:20])
        if trace.summary:
            lines.append(f"\n## 说明\n\n{trace.summary}\n")
        lines.append(
            "\n---\n> 🤖 本 PR 由 **Fissue** 自动生成，请人工复核后再合并。\n"
        )
        return "\n".join(lines)

    async def _label_pr(self, item: RawItem, repo_cfg: RepoConfig, pr_number: Any, adapter) -> None:
        """给 PR 打 AI 标签（失败不影响主流程）。"""
        if pr_number is None:
            return
        strategy = self.settings.auto_fix.pr_strategy
        labels = [strategy.label, *strategy.extra_labels]
        try:
            ref = parse_repo_ref(item.repo, item.platform)
            for lbl in labels:
                await adapter.ensure_label(ref, lbl)
            await adapter.add_labels(ref, int(pr_number), labels, ItemType.PR)
        except Exception as exc:
            log.warning("给 PR #%s 打标签失败：%s", pr_number, exc)

    def _save_patch(self, item: RawItem, diff: str) -> str:
        safe = item.key.replace(":", "_").replace("/", "_")
        path = self.settings.data_dir / "patches" / f"{safe}.patch"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_patch(path, diff)
        return str(path)


class AutoFixer:
    """自动修复的总编排：挑条目 → Agent 修复 → 验证 → 提 PR / 出报告。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.agent = FixAgent(ctx.llm, ctx.repo, ctx.verifier, ctx.settings)
        self.pr_creator = PRCreator(ctx)

    # -- 单条 -------------------------------------------------------------

    async def fix_item(
        self,
        item: RawItem,
        *,
        dry_run: bool = False,
        max_rounds: int | None = None,
    ) -> FixAttempt:
        """修复单个 Issue 并（可选）提 PR。"""
        # 策略闸门：只有 BUG 类、优先级 tier1/tier2 才修
        evaluation = self.ctx.repo.latest_evaluation(item.key)
        if evaluation is None:
            return self._skip(item, "缺少评测结果")
        if item.item_type is not ItemType.ISSUE and self.settings.fix_policy.only_issues:
            return self._skip(item, "策略只自动修复 Issue")
        if item.priority not in (Priority.TIER1, Priority.TIER2):
            return self._skip(item, f"优先级为 {item.priority.value}，不自动修复")
        if evaluation.spam.suspicious:
            return self._skip(item, "疑似刷量/重复，不自动修复")
        if self._has_open_attempt(item.key):
            return self._skip(item, "已存在修复尝试，跳过以免重复提 PR")

        verifier = self.ctx.repo.latest_verifier(item.key)
        if verifier is None:
            return self._skip(item, "缺少验证器，无法验证修复")
        verifier_id, spec = verifier
        if spec.kind is VerifierKind.CHECKLIST:
            return self._skip(item, "清单型验证器无法自动判定修复结果")

        repo_cfg = self._repo_config(item)
        self.ctx.repo.set_item_status(item.key, ItemStatus.FIXING)
        workspace: RepoWorkspace | None = None

        try:
            workspace = await self.ctx.clone_workspace(repo_cfg)
            repo_context = self._repo_context(workspace, repo_cfg)

            # 1) Agent 循环修复
            attempt, trace = await self.agent.run(
                item=item,
                workspace=workspace,
                spec=spec,
                repo_context=repo_context,
                verifier_id=verifier_id,
                max_rounds=max_rounds,
            )
            self._record_usage(item, trace)

            if attempt.outcome is not FixOutcome.SUCCESS:
                return await self._handle_failure(item, attempt, trace, spec)

            # 2) 提 PR 前再跑一次完整 F2P（防止 Agent 自说自话）
            ctx_verify = VerifyContext(
                workspace=workspace,
                item=item,
                spec=spec,
                repo_slug=repo_cfg.slug,
                test_hint=repo_cfg.test_hint,
            )
            final = await self.ctx.verifier.verify_fix(
                ctx_verify, verifier_id=verifier_id
            )
            if not final.f2p_satisfied and self.settings.verifier.require_f2p:
                attempt.outcome = FixOutcome.NEEDS_MANUAL
                attempt.error = f"最终 F2P 未通过：{final.conclusion}"
                return await self._handle_failure(item, attempt, trace, spec)

            # 既有测试回归门（fix 阶段）：F2P 只证明目标用例被修好，证明不了
            # 「没弄坏别的」。strict 下 base 绿而 fix 红 → 拒绝本次修复。
            reg_ok, reg_why = await self._run_regression_gate(
                item=item, workspace=workspace, spec=spec, repo_cfg=repo_cfg,
                verifier_id=verifier_id,
            )
            if not reg_ok:
                attempt.outcome = FixOutcome.NEEDS_MANUAL
                attempt.error = f"回归门未通过：{reg_why}"
                return await self._handle_failure(item, attempt, trace, spec, regression_note=reg_why)

            attempt.diff = attempt.diff or workspace.diff()

            if dry_run:
                path = self.pr_creator._save_patch(item, attempt.diff)
                attempt.outcome = FixOutcome.NEEDS_MANUAL
                attempt.dry_run = True
                attempt.patch_path = path
                attempt.pr_url = None
                attempt.error = "dry-run：仅产出补丁，未提交 PR"
                self.ctx.repo.save_fix_attempt(attempt)
                # 干跑没产出 PR，条目等于还没修：放回待修队列，否则一趟 dry-run
                # 就把待修队列消费掉（条目会卡在 fixing/needs_manual），
                # 之后去掉 --dry-run 的真跑反而取不到任何候选。
                self.ctx.repo.set_item_status(item.key, ItemStatus.FIX_QUEUED)
                return attempt

            # 3) 提交 PR
            attempt = await self.pr_creator.submit(
                attempt=attempt,
                trace=trace,
                item=item,
                workspace=workspace,
                repo_cfg=repo_cfg,
                spec=spec,
                f2p_ok=final.f2p_satisfied,
                diff=attempt.diff,
            )
            self.ctx.repo.save_fix_attempt(attempt)
            return attempt
        except Exception as exc:
            log.exception("自动修复失败 %s", item.key)
            attempt = FixAttempt(item_key=item.key, outcome=FixOutcome.FAILED, error=f"{type(exc).__name__}: {exc}")
            self.ctx.repo.save_fix_attempt(attempt)
            return attempt
        finally:
            if workspace is not None:
                try:
                    workspace.cleanup()
                except Exception:  # pragma: no cover
                    pass

    # -- 批量 -------------------------------------------------------------

    async def fix_candidates(
        self,
        *,
        repo_slug: str | None = None,
        limit: int = 10,
        dry_run: bool = False,
        max_rounds: int | None = None,
    ) -> FixReport:
        """修复一批「低难度」Issue，按 tier1 → tier2 顺序。"""
        from ..pipeline.flush import FlushProcessor

        report = FixReport()
        candidates = FlushProcessor(self.ctx).fix_candidates(repo_slug=repo_slug, limit=limit)
        for item in candidates:
            attempt = await self.fix_item(item, dry_run=dry_run, max_rounds=max_rounds)
            report.attempted += 1
            report.attempts.append(attempt)
            report.record(attempt, dry_run=dry_run)
            if attempt.outcome is FixOutcome.SUCCESS and attempt.pr_url:
                report.prs.append((item.key, attempt.pr_url))
        log.info(report.summary)
        return report

    # -- 失败降级 ---------------------------------------------------------

    async def _run_regression_gate(
        self,
        *,
        item: RawItem,
        workspace: RepoWorkspace,
        spec: VerifierSpec,
        repo_cfg: RepoConfig,
        verifier_id: int | None = None,
    ) -> tuple[bool, str]:
        """在**已修复**的工作区上跑既有测试回归门。返回 ``(是否通过, 说明)``。

        base 阶段的基线从库里取（``VerifyStage`` 已落 ``regression:base``）；
        取不到时 ``judge_regression`` 会因 ``base_reg is None`` 直接放行。
        """
        mode = self.settings.verifier.regression_gate
        if mode == "off":
            return True, ""

        base_runs = self.ctx.repo.verifier_runs(item.key, stage=REGRESSION_STAGE_BASE, limit=1)
        base_reg = base_runs[0] if base_runs else None

        if verifier_id is None:
            latest = self.ctx.repo.latest_verifier(item.key)
            verifier_id = latest[0] if latest else None
        if verifier_id is None:
            return True, "缺少验证器记录，回归门跳过"

        fix_reg = await self.ctx.regression_gate.run(
            item_key=item.key,
            workspace=workspace,
            spec=spec,
            repo_cfg=repo_cfg,
            stage="fix",
            verifier_id=verifier_id,
        )
        return judge_regression(base_reg, fix_reg, mode=mode, f2p_satisfied=True)

    async def _handle_failure(
        self,
        item: RawItem,
        attempt: FixAttempt,
        trace: AgentTrace,
        spec: VerifierSpec,
        *,
        regression_note: str = "",
    ) -> FixAttempt:
        """按 ``on_failure`` 处置失败（Q12 选 report_manual）。"""
        mode = self.settings.auto_fix.on_failure
        if mode == "discard":
            attempt.outcome = FixOutcome.FAILED
            self.ctx.repo.save_fix_attempt(attempt)
            return attempt

        # report_manual / retry_then_report：生成「需人工」报告
        attempt.outcome = FixOutcome.NEEDS_MANUAL
        try:
            path = self._write_manual_report(
                item, attempt, trace, spec, regression_note=regression_note
            )
            attempt.report_path = path
        except Exception as exc:
            log.warning("写人工报告失败：%s", exc)
        self.ctx.repo.save_fix_attempt(attempt)
        return attempt

    def _write_manual_report(
        self,
        item: RawItem,
        attempt: FixAttempt,
        trace: AgentTrace,
        spec: VerifierSpec,
        *,
        regression_note: str = "",
    ) -> str:
        """落盘一份给人工复核的报告（含轨迹与失败证据）。"""
        safe = item.key.replace(":", "_").replace("/", "_")
        out_dir = self.settings.data_dir / "manual" / safe
        out_dir.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# 需人工处理：{item.repo} #{item.number}",
            "",
            f"- 标题：{item.title}",
            f"- 链接：{self._item_url(item)}",
            f"- 优先级：`{item.priority.value}`",
            f"- 失败原因：{attempt.error or '未知'}",
            f"- Agent 轮次：{trace.rounds}",
            f"- 验证器：`{spec.name}`（{spec.kind.value}）",
            f"- 验证器命令：`{spec.command or '(清单型)'}`",
            "",
            "## Agent 动作轨迹",
            "",
            "```text",
            *trace.actions[-60:],
            "```",
        ]
        if regression_note:
            lines += [
                "",
                "## 回归门（既有测试）",
                "",
                regression_note,
                "",
                "> 回归门完整输出见 `data/artifacts/<item>/regression:fix/`。",
            ]
        if trace.rejected:
            lines += ["", "## 被拒绝的写入", "", *[f"- {r}" for r in trace.rejected[-20:]]]
        if trace.written_files:
            lines += ["", "## 已改动文件", "", *[f"- `{p}`" for p in trace.written_files[:30]]]
        if trace.test_outputs:
            lines += ["", "## 最后一次验证器输出", "", "```text", trace.test_outputs[-1][:4000], "```"]

        report_path = out_dir / "report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")

        if attempt.diff:
            _write_patch(out_dir / "attempt.patch", attempt.diff)
        (out_dir / "trace.json").write_text(
            json.dumps(
                {
                    "rounds": trace.rounds,
                    "actions": trace.actions,
                    "rejected": trace.rejected,
                    "written_files": trace.written_files,
                    "usage": {
                        "prompt_tokens": trace.usage.prompt_tokens,
                        "completion_tokens": trace.usage.completion_tokens,
                        "cost_usd": trace.usage.cost_usd,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(report_path)

    # -- 内部 -------------------------------------------------------------

    def _skip(self, item: RawItem, reason: str) -> FixAttempt:
        log.info("跳过自动修复 %s：%s", item.key, reason)
        attempt = FixAttempt(item_key=item.key, outcome=FixOutcome.SKIPPED, error=reason)
        return attempt

    def _has_open_attempt(self, item_key: str) -> bool:
        """是否已有**成功**的修复尝试（有则不再重复提 PR）。

        只拦 SUCCESS：它才意味着「已经提过 PR」，重复跑会撞已有分支/PR。
        NEEDS_MANUAL / FAILED 表示这轮**没成功**，拦它们等于把一次失败当永久封禁——
        修复失败的原因可能出在验证器质量或模型能力上，允许多次尝试、每轮留 trace
        记录，比一失败就拉黑更合理。真正的成本控制在预算闸门（BudgetGuard）那边。
        """
        return any(a.outcome is FixOutcome.SUCCESS for a in self.ctx.repo.fix_attempts(item_key))

    def _repo_config(self, item: RawItem) -> RepoConfig:
        try:
            return self.ctx.settings.repo(item.repo, item.platform)
        except Exception:
            owner, _, name = item.repo.partition("/")
            return RepoConfig(platform=item.platform, owner=owner, name=name)

    def _repo_context(self, ws: RepoWorkspace, repo_cfg: RepoConfig) -> str:
        from ..ai.prompts import render_repo_context

        return render_repo_context(
            test_hint=repo_cfg.test_hint,
            language_hint=ws.detect_language(),
            file_tree=ws.file_tree(limit=200),
            readme=ws.readme(),
            api_signatures=ws.api_signatures(),
        )

    def _item_url(self, item: RawItem) -> str:
        try:
            adapter_cls = None
            from ..platforms.registry import get_adapter_class

            adapter_cls = get_adapter_class(item.platform)
            adapter = adapter_cls(token="")
            return adapter.item_url(parse_repo_ref(item.repo, item.platform), item.number, item.item_type)
        except Exception:  # pragma: no cover
            return item.key

    def _record_usage(self, item: RawItem, trace: AgentTrace) -> None:
        if not trace.usage.prompt_tokens and not trace.usage.completion_tokens:
            return
        repo_key = item.repo
        self.ctx.repo.record_usage(
            repo_key=repo_key,
            purpose="agent",
            model=trace.usage.model,
            prompt_tokens=trace.usage.prompt_tokens,
            completion_tokens=trace.usage.completion_tokens,
            cost_usd=trace.usage.cost_usd,
        )
