"""流水线阶段：把「分类 → 评测 → 验证 → 入队」串起来（Q1 的四条流程）。

四条流程
--------
1. **Issue·BUG**：评测难度 → 生成验证器 → 沙盒跑 base（期望失败）→ 入 verify 队列。
   批量 flush 后打「有效性/重要性」标签；低难度高重要性 → 交给修复器。
2. **Issue·FEATURE**：评测可行性/难度/必要性 → 打标签 → 等开发者（不入修复队列）。
3. **PR·BUG**：判定提交者 → 找关联 Issue → 生成验证器 → 沙盒合并 PR 并验证 → 入 fix_bug 队列。
4. **PR·FEATURE**：同 PR·BUG，但入 fix_feature 队列（共用沙盒）。

每条流程都不自己 flush，flush 由 :mod:`fissue.pipeline.queue` 与常驻服务统筹。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..logging_setup import get_logger
from ..models import (
    AuthorKind,
    Category,
    Evaluation,
    ItemStatus,
    ItemType,
    Priority,
    QueueName,
    RawItem,
    VerifierKind,
    VerifierOutcome,
    VerifierResult,
    VerifierRun,
)
from ..sandbox.protocol import MountSpec
from ..verifier.runner import CONTAINER_WORKDIR, VerifyContext, judge_regression
from ..workspace import RepoWorkspace
from .context import RuntimeContext
from .queue import QueueManager

log = get_logger(__name__)


@dataclass
class StageResult:
    """一个条目跑完某阶段的结果。"""

    key: str
    category: Category = Category.UNKNOWN
    evaluation: Evaluation | None = None
    verifier_kind: VerifierKind | None = None
    verifier_result: VerifierResult | None = None
    regression_run: VerifierRun | None = None      # 回归门结论（base 或 fix 阶段）
    queued: QueueName | None = None
    status: ItemStatus | None = None
    skipped_reason: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped_reason is None


@dataclass
class BatchResult:
    """一批条目的处理结果。"""

    repo: str = ""
    evaluated: int = 0
    verified: int = 0
    queued: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[StageResult] = field(default_factory=list)
    error: str | None = None

    def add(self, r: StageResult) -> None:
        self.results.append(r)
        if r.error:
            self.failed += 1
        elif r.skipped_reason:
            self.skipped += 1
        if r.evaluation is not None:
            self.evaluated += 1
        if r.verifier_result is not None:
            self.verified += 1
        if r.queued is not None:
            self.queued += 1

    @property
    def summary(self) -> str:
        return (
            f"{self.repo}：评测 {self.evaluated}，验证 {self.verified}，"
            f"入队 {self.queued}，跳过 {self.skipped}，失败 {self.failed}"
        )


# ---------------------------------------------------------------------------
# 阶段一：评测
# ---------------------------------------------------------------------------


class EvalStage:
    """分类 + 四维评测。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings

    async def run(
        self,
        item: RawItem,
        *,
        deep: bool = False,
        similar: Sequence[dict[str, Any]] | None = None,
    ) -> Evaluation:
        """评测一个条目；``deep=True`` 时对 PR 追加 diff 深度评审（Q1 分层）。"""
        evaluation = await self.ctx.evaluator.evaluate(item, similar=similar)

        if deep and item.item_type is ItemType.PR:
            try:
                adapter = self.ctx.adapter_for_platform(item.platform)
                from ..platforms.registry import parse_repo_ref

                ref = parse_repo_ref(item.repo, item.platform)
                diff = ""
                fetcher = getattr(adapter, "fetch_diff", None)
                if fetcher is not None:
                    diff = await adapter.fetch_diff(ref, item.number)
                if not diff and getattr(adapter, "fetch_files", None) is not None:
                    files = await adapter.fetch_files(ref, item.number, with_patch=True)
                    diff = "\n".join(f"--- {f.path} ---\n{f.patch or ''}" for f in files)
                if diff:
                    deep_eval = await self.ctx.evaluator.deep_pr_review(item, diff=diff)
                    if deep_eval is not None:
                        evaluation = deep_eval
            except Exception as exc:
                log.warning("PR 深度评审失败（保留基础评测）%s：%s", item.key, exc)

        return evaluation

    async def run_batch(
        self,
        items: Sequence[RawItem],
        *,
        deep: bool = False,
        concurrency: int = 3,
    ) -> BatchResult:
        """并发评测一批条目。"""
        result = BatchResult(repo=items[0].repo if items else "")
        sem = asyncio.Semaphore(max(1, concurrency))
        pool = self._similar_pool(items)

        async def one(item: RawItem) -> StageResult:
            async with sem:
                try:
                    similar = _similar_to(item, pool) if pool else None
                    evaluation = await self.run(item, deep=deep, similar=similar)
                    return StageResult(
                        key=item.key,
                        category=evaluation.category,
                        evaluation=evaluation,
                        status=ItemStatus.EVALUATED,
                    )
                except Exception as exc:
                    log.exception("评测失败 %s", item.key)
                    return StageResult(key=item.key, error=f"{type(exc).__name__}: {exc}")

        for r in await asyncio.gather(*(one(i) for i in items)):
            result.add(r)
        log.info(result.summary)
        return result

    def _similar_pool(self, batch: Sequence[RawItem]) -> list[RawItem]:
        """本批条目 + 同仓库已入库条目，作为重复检测的候选池。

        包含同批条目，否则「同一批里的两条重复提交」永远互相看不见
        （而 DESIGN.md 正是要靠批量上下文来识别成批重复）。
        """
        pool = list(batch)
        slug = batch[0].repo if batch else ""
        if not slug:
            return pool
        try:
            existing = self.ctx.repo.list_items(repo_slug=slug, limit=200)
        except Exception as exc:
            log.warning("加载相似条目候选失败（本次跳过重复检测）：%s", exc)
            return pool
        seen = {i.key for i in pool}
        pool.extend(e for e in existing if e.key not in seen)
        return pool


# ---------------------------------------------------------------------------
# 阶段二：验证（Issue-BUG）
# ---------------------------------------------------------------------------


class VerifyStage:
    """Issue-BUG：生成验证器 → 沙盒 base 验证 → 入 verify 队列。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.queues = QueueManager(ctx)

    async def run(
        self,
        item: RawItem,
        evaluation: Evaluation | None = None,
        *,
        workspace: RepoWorkspace | None = None,
    ) -> StageResult:
        result = StageResult(key=item.key, evaluation=evaluation)
        evaluation = evaluation or self.ctx.repo.latest_evaluation(item.key)
        result.evaluation = evaluation

        if item.item_type is not ItemType.ISSUE:
            result.skipped_reason = "非 Issue，不走 Issue 验证流程"
            return result
        if evaluation is not None and evaluation.category is not Category.BUG:
            result.skipped_reason = f"分类为 {evaluation.category.value}，不走 BUG 验证流程"
            return result

        # 策略闸门：疑似刷量/重复的条目在**生成验证器之前**就要排除。
        # 否则会为一条刷量请求白跑一轮沙盒（envkit #14 实测：评测已判 spam，
        # 验证阶段仍生成验证器并给出 fix 结论），也会为重复条目重复建验证器。
        if evaluation is not None and evaluation.spam.suspicious:
            kinds = "".join(
                [
                    "刷量" if evaluation.spam.is_spam else "",
                    "重复" if evaluation.spam.is_duplicate else "",
                ]
            )
            self.ctx.repo.set_item_status(item.key, ItemStatus.SKIPPED)
            result.status = ItemStatus.SKIPPED
            result.skipped_reason = f"疑似{kinds or '无效'}，不生成验证器"
            return result

        repo_cfg = self._repo_config(item)
        owns_workspace = workspace is None
        ws = workspace
        try:
            if ws is None:
                ws = await self.ctx.clone_workspace(repo_cfg)

            repo_context = self._repo_context(ws, repo_cfg)
            verifier_id, spec, base_result = await self.ctx.verifier.generate_and_validate(
                item=item,
                workspace=ws,
                repo_context=repo_context,
                generator=self.ctx.generator,
                test_hint=repo_cfg.test_hint,
            )
            result.verifier_kind = spec.kind
            result.verifier_result = base_result

            if spec.kind is VerifierKind.CHECKLIST:
                self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
                result.status = ItemStatus.NEEDS_MANUAL
                result.notes.append("无法自动化验证，已标记需人工，清单已生成供复核")
                return result

            if not base_result.reproducible:
                self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
                result.status = ItemStatus.NEEDS_MANUAL
                result.notes.append(f"问题未能复现：{base_result.conclusion}")
                return result

            # 既有测试回归门：base 阶段先记一条基线（仓库本身绿不绿）。
            # 成本只随运行时间增长，与 Issue 数、LLM token 均无关。
            if self.settings.verifier.regression_gate != "off":
                base_reg = await self.ctx.regression_gate.run(
                    item_key=item.key,
                    workspace=ws,
                    spec=spec,
                    repo_cfg=repo_cfg,
                    stage="base",
                    verifier_id=verifier_id,
                )
                result.regression_run = base_reg
                passed, why = judge_regression(
                    base_reg, None,
                    mode=self.settings.verifier.regression_gate,
                    f2p_satisfied=base_result.f2p_satisfied,
                )
                if why:
                    result.notes.append(why)
                if not passed:                       # 目前 strict 下不会发生，留作防御
                    self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
                    result.status = ItemStatus.NEEDS_MANUAL
                    result.skipped_reason = why
                    return result

            # 已验证可复现 → 入验证队列，等批量 flush 打标签
            self.ctx.repo.set_item_status(item.key, ItemStatus.QUEUED)
            entry_id = self.queues.enqueue(
                QueueName.VERIFY,
                item.key,
                payload={
                    "stage": "verify",
                    "verifier_id": verifier_id,
                    "base_outcome": base_result.base_run.outcome.value if base_result.base_run else "",
                },
            )
            self.queues.repo.finish_queue_entry(
                entry_id,
                status="done",
                result={
                    "verified": True,
                    "conclusion": base_result.conclusion,
                    "output": _run_output(base_result.base_run),
                },
            )
            result.queued = QueueName.VERIFY
            result.status = ItemStatus.QUEUED
            result.notes.append(base_result.conclusion)
            return result
        except Exception as exc:
            log.exception("验证流程失败 %s", item.key)
            result.error = f"{type(exc).__name__}: {exc}"
            self.ctx.repo.set_item_status(item.key, ItemStatus.FAILED)
            return result
        finally:
            if owns_workspace and ws is not None:
                _safe_cleanup(ws)

    # -- 内部 -------------------------------------------------------------

    def _repo_config(self, item: RawItem):
        try:
            return self.ctx.settings.repo(item.repo, item.platform)
        except Exception:
            from ..config import RepoConfig

            owner, _, name = item.repo.partition("/")
            return RepoConfig(platform=item.platform, owner=owner, name=name)

    def _repo_context(self, ws: RepoWorkspace, repo_cfg) -> str:
        from ..ai.prompts import render_repo_context

        return render_repo_context(
            test_hint=repo_cfg.test_hint
            or self.settings.repos[0].test_hint
            if self.settings.repos
            else None,
            language_hint=ws.detect_language(),
            file_tree=ws.file_tree(limit=150),
            readme=ws.readme(),
        )


# ---------------------------------------------------------------------------
# 阶段三：PR 验证（PR-BUG / PR-FEATURE）
# ---------------------------------------------------------------------------


class PRVerifyStage:
    """PR：判定提交者 → 找关联 Issue → 生成验证器 → 沙盒合并验证 → 入 fix 队列。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.queues = QueueManager(ctx)

    async def run(
        self,
        item: RawItem,
        evaluation: Evaluation | None = None,
    ) -> StageResult:
        result = StageResult(key=item.key, evaluation=evaluation)
        evaluation = evaluation or self.ctx.repo.latest_evaluation(item.key)
        result.evaluation = evaluation

        if item.item_type is not ItemType.PR:
            result.skipped_reason = "非 PR"
            return result
        if item.merged:
            result.skipped_reason = "PR 已合并，无需验证"
            return result
        if item.state == "closed":
            result.skipped_reason = "PR 已关闭"
            return result

        # 与 VerifyStage 同口径：疑似刷量/重复的 PR 也不进沙盒验证与合并流程。
        if evaluation is not None and evaluation.spam.suspicious:
            kinds = "".join(
                [
                    "刷量" if evaluation.spam.is_spam else "",
                    "重复" if evaluation.spam.is_duplicate else "",
                ]
            )
            self.ctx.repo.set_item_status(item.key, ItemStatus.SKIPPED)
            result.status = ItemStatus.SKIPPED
            result.skipped_reason = f"疑似{kinds or '无效'}，不做 PR 验证"
            return result

        category = evaluation.category if evaluation else Category.FEATURE
        result.category = category
        queue = QueueName.FIX_BUG if category is Category.BUG else QueueName.FIX_FEATURE

        repo_cfg = self._repo_config(item)

        # 纯文档改动的 PR 无法做功能验证：验证器要断言的「新 API / 新行为」
        # 在代码里根本不存在，跑出来只会是 ModuleNotFoundError / TypeError 之类的
        # 导入期错误——而那与「验证器自己写错」在输出上无法区分（envkit #18 实测：
        # 只改 README 宣称 envkit/files.py，验证器报 No module named 'envkit.files'，
        # 被判成「验证器自身有误」，结论含糊且没有明确的不合并建议）。
        # 这里用确定性规则先判掉：没有代码改动 → 给不出功能证据 → 明确不推荐合并。
        docs_only = self._docs_only_paths(item)
        if docs_only is not None:
            self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
            result.status = ItemStatus.NEEDS_MANUAL
            result.skipped_reason = (
                f"仅改动文档（{', '.join(docs_only[:3])}），未包含代码实现，"
                "无法提供功能证据，不建议合并"
            )
            result.notes.append("无代码变更：明确不推荐合并")
            return result

        ws: RepoWorkspace | None = None
        try:
            who = await self._author_kind(item)
            result.notes.append(f"提交者身份：{who.value}")
            if item.linked_issues:
                result.notes.append(f"关联 Issue：{item.linked_issues}")

            ws = await self.ctx.clone_workspace(repo_cfg)

            # base 阶段必须跑在**未修复**的代码上（此刻工作区是 main）。
            # 若先合并 PR 再跑 base，验证器会在“已修复”的代码上通过，被误判为
            # 「验证器不可靠」——那样任何正确的 PR 都无法被推荐合并。
            repo_context = self._repo_context(ws, repo_cfg)
            verifier_id, spec, base_result = await self.ctx.verifier.generate_and_validate(
                item=item,
                workspace=ws,
                repo_context=repo_context,
                generator=self.ctx.generator,
                test_hint=repo_cfg.test_hint,
                linked_context=self._linked_issue_context(item, repo_cfg),
            )
            result.verifier_kind = spec.kind
            result.verifier_result = base_result

            if spec.kind is VerifierKind.CHECKLIST:
                self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
                result.status = ItemStatus.NEEDS_MANUAL
                result.notes.append("清单型验证器：转人工复核")
                return result

            if not _base_reproduced(spec, base_result):
                # 未修复代码上就通过 → 该验证器证明不了这个 PR 修的是什么。
                # 再合并 PR 跑 fix 只会得到“必然通过”的假结论，故直接转人工。
                self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
                result.status = ItemStatus.NEEDS_MANUAL
                result.skipped_reason = base_result.conclusion
                result.notes.append("PR 合并验证转人工：" + base_result.conclusion)
                return result

            # 回归门 base：**必须在 _merge_pr 之前**，否则基线跑在“已修复”的代码上，
            # 拿不到「仓库原本绿不绿」的判据，strict 就无从区分「修复破坏」与「本就红」。
            gate_mode = self.settings.verifier.regression_gate
            base_reg: VerifierRun | None = None
            if gate_mode != "off":
                base_reg = await self.ctx.regression_gate.run(
                    item_key=item.key,
                    workspace=ws,
                    spec=spec,
                    repo_cfg=repo_cfg,
                    stage="base",
                    verifier_id=verifier_id,
                )
                result.regression_run = base_reg

            # base 已确认复现 → 现在才把 PR 的改动合并进工作副本
            merged, merge_note = await self._merge_pr(ws, item, repo_cfg)
            result.notes.append(merge_note)
            if not merged:
                self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
                result.status = ItemStatus.NEEDS_MANUAL
                result.skipped_reason = f"无法合并 PR：{merge_note}"
                return result

            # 在**已合并**的工作区上跑 fix 阶段，构成完整 F2P
            finished = await self.ctx.verifier.verify_fix(
                VerifyContext(
                    workspace=ws,
                    item=item,
                    spec=spec,
                    repo_slug=repo_cfg.slug,
                    test_hint=repo_cfg.test_hint,
                ),
                verifier_id=verifier_id,
                base_run=base_result.base_run,
            )
            result.verifier_result = finished

            # 回归门 fix：在**已合并**的代码上再跑一次，与 base 组成回归判定。
            # 两者都成立才允许推荐合并（§3.1）。
            if gate_mode != "off":
                fix_reg = await self.ctx.regression_gate.run(
                    item_key=item.key,
                    workspace=ws,
                    spec=spec,
                    repo_cfg=repo_cfg,
                    stage="fix",
                    verifier_id=verifier_id,
                )
                result.regression_run = fix_reg
                reg_ok, reg_why = judge_regression(
                    base_reg, fix_reg, mode=gate_mode, f2p_satisfied=finished.f2p_satisfied
                )
                if reg_why:
                    result.notes.append(reg_why)
                if not reg_ok:
                    # strict：修复破坏了既有测试 → 转人工，结论文案已点名失败用例
                    self.ctx.repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
                    result.status = ItemStatus.NEEDS_MANUAL
                    result.skipped_reason = reg_why
                    result.notes.append("回归门拒绝推荐合并：" + reg_why)
                    return result

            self.ctx.repo.set_item_status(item.key, ItemStatus.QUEUED)
            entry_id = self.queues.enqueue(
                queue,
                item.key,
                payload={
                    "stage": "pr_verify",
                    "verifier_id": verifier_id,
                    "category": category.value,
                    "author_kind": who.value,
                    "linked_issues": item.linked_issues,
                },
            )
            self.queues.repo.finish_queue_entry(
                entry_id,
                status="done",
                result={
                    "merge_recommended": finished.f2p_satisfied,
                    "conclusion": finished.conclusion,
                    "output": _run_output(finished.fix_run),
                },
            )
            result.queued = queue
            result.status = ItemStatus.QUEUED
            result.notes.append(finished.conclusion)
            return result
        except Exception as exc:
            log.exception("PR 验证流程失败 %s", item.key)
            result.error = f"{type(exc).__name__}: {exc}"
            self.ctx.repo.set_item_status(item.key, ItemStatus.FAILED)
            return result
        finally:
            if ws is not None:
                _safe_cleanup(ws)

    # -- 内部 -------------------------------------------------------------

    async def _author_kind(self, item: RawItem) -> AuthorKind:
        """在已抓取启发式判断基础上，用 LLM 复核一次（Q1 要求先分辨提交者）。"""
        if item.author_kind in (AuthorKind.AI_AGENT, AuthorKind.BOT):
            return item.author_kind
        return item.author_kind if item.author_kind is not AuthorKind.UNKNOWN else AuthorKind.COMMUNITY

    def _docs_only_paths(self, item: RawItem) -> list[str] | None:
        """PR 是否**只改了文档**。是则返回文档路径列表，否则（含代码变更 / 取不到）返回 None。

        只读 ``item.files``（抓取阶段已落库），**不发任何请求**——验证阶段必须能在
        离线环境跑；此前在这里现拉平台文件清单，会让单测卡在网络重试上。
        没有文件清单时一律返回 None：宁可放行让流程照常验证，
        也不要凭空下「不建议合并」的结论。
        """
        paths = [f.path for f in (item.files or []) if getattr(f, "path", "")]
        if not paths:
            return None

        docs = [p for p in paths if _is_doc_path(p)]
        return docs if len(docs) == len(paths) else None

    async def _merge_pr(
        self, ws: RepoWorkspace, item: RawItem, repo_cfg
    ) -> tuple[bool, str]:
        """把 PR 的改动合并到工作副本。

        优先用平台 diff 打成补丁应用（避免依赖 fork 远端权限）；失败则尝试
        直接 fetch 源分支合并。
        """
        adapter = await self.ctx.adapter_for_repo(repo_cfg)
        from ..platforms.registry import parse_repo_ref

        ref = parse_repo_ref(item.repo, item.platform)

        diff = ""
        try:
            diff = await adapter.fetch_diff(ref, item.number)
        except Exception as exc:
            log.warning("拉取 PR diff 失败 %s：%s", item.key, exc)

        if diff:
            ok, err = ws.apply_patch(diff, three_way=True)
            if ok:
                return True, "已用 PR diff 应用改动"
            log.warning("应用 PR diff 失败 %s：%s", item.key, err[:200])

        # 退路：直接抓源分支
        if item.head_repo and item.head_branch:
            fetched = ws.fetch("origin", f"refs/heads/{item.head_branch}") or await asyncio.to_thread(
                ws._git, ["git", "fetch", "--depth", "50", adapter.clone_url(ref), item.head_branch]
            )
            if not isinstance(fetched, bool):
                fetched = bool(getattr(fetched, "ok", False))
            if fetched:
                ws.reset_hard("FETCH_HEAD")
                return True, "已 fetch 并切换到 PR 源分支"
        return False, "无法获取 PR 改动（diff 应用失败且源分支不可达）"

    def _repo_config(self, item: RawItem):
        try:
            return self.ctx.settings.repo(item.repo, item.platform)
        except Exception:
            from ..config import RepoConfig

            owner, _, name = item.repo.partition("/")
            return RepoConfig(platform=item.platform, owner=owner, name=name)

    def _repo_context(self, ws: RepoWorkspace, repo_cfg) -> str:
        from ..ai.prompts import render_repo_context

        return render_repo_context(
            test_hint=repo_cfg.test_hint,
            language_hint=ws.detect_language(),
            file_tree=ws.file_tree(limit=150),
            readme=ws.readme(),
        )

    def _linked_issue_context(self, item: RawItem, repo_cfg) -> str:
        """取回被本 PR 修复的原 Issue 正文。

        PR 作者往往只在正文里复述自己修的那**一个**场景，而问题完整的复现用例写在
        原 Issue 里（例如 PR #7 只说 ``Hello   World``，而 issue #2 还列了
        ``Hello - World``、``a  -  b``）。不喂这段材料，验证器只会覆盖 PR 自己
        提到的场景，从而把「只修一半」的修复判成通过。
        """
        if not item.linked_issues:
            return ""
        parts: list[str] = []
        for number in item.linked_issues[:5]:
            try:
                linked = self.ctx.repo.get_item(f"{item.platform.value}:{repo_cfg.slug}#{number}")
            except Exception as exc:  # pragma: no cover - 仅防御
                log.debug("读取关联 Issue 失败 %s#%s：%s", repo_cfg.slug, number, exc)
                continue
            if linked is None:
                continue
            parts.append(f"### Issue #{linked.number}：{linked.title}\n{linked.body}")
        if not parts:
            return ""
        log.debug("已注入 %d 条关联 Issue 正文供验证器生成 %s", len(parts), item.key)
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------


class Pipeline:
    """把各阶段按条目类型路由到对应流程。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.eval_stage = EvalStage(ctx)
        self.verify_stage = VerifyStage(ctx)
        self.pr_stage = PRVerifyStage(ctx)
        self.queues = QueueManager(ctx)

    async def evaluate_items(
        self, items: Sequence[RawItem], *, deep: bool = False, concurrency: int = 3
    ) -> BatchResult:
        return await self.eval_stage.run_batch(items, deep=deep, concurrency=concurrency)

    async def process_repo(
        self,
        repo_slug: str,
        *,
        limit: int = 50,
        deep: bool = False,
        verify: bool = True,
    ) -> BatchResult:
        """对一个仓库跑「评测 + 验证」全流程。

        注意：按 Q1，只对 BUG 类条目生成验证器；FEATURE 只评测打标签。
        """
        items = self.ctx.repo.list_items(repo_slug=repo_slug, status=ItemStatus.NEW, limit=limit)
        if not items:
            items = self.ctx.repo.list_items(repo_slug=repo_slug, limit=limit)

        eval_result = await self.evaluate_items(items, deep=deep)
        combined = BatchResult(repo=repo_slug)
        combined.evaluated = eval_result.evaluated
        combined.results = list(eval_result.results)
        combined.failed = 0

        if not verify:
            return combined

        for r in eval_result.results:
            if r.error or r.evaluation is None:
                combined.add(StageResult(key=r.key, error=r.error or "无评测结果"))
                continue
            item = self.ctx.repo.get_item(r.key)
            if item is None:
                continue
            # Issue 的 FEATURE 没有缺陷可复现 → 只打标签等开发者。
            #
            # 但 **PR-FEATURE 必须验证**（DESIGN §2：PR|FEATURE 入 fix_feature 队列）。
            # PRVerifyStage 本身是分类无关的——它按 category 自行选 FIX_BUG/FIX_FEATURE，
            # 所以在这里按 category 一刀切会把功能类 PR 全部挡在门外，导致
            # 「只改文档却宣称实现了功能」这类 PR 无人识破（envkit #18 实测：
            # 停在 labeled、没有任何结论，fix_feature 队列一条都没有）。
            if r.evaluation.category is not Category.BUG and item.item_type is ItemType.ISSUE:
                self.ctx.repo.set_item_status(item.key, ItemStatus.LABELED)
                combined.add(
                    StageResult(
                        key=item.key,
                        category=Category.FEATURE,
                        evaluation=r.evaluation,
                        status=ItemStatus.LABELED,
                        skipped_reason="FEATURE 不验证，已打标签等开发者处理",
                    )
                )
                continue

            if item.item_type is ItemType.ISSUE:
                combined.add(await self.verify_stage.run(item, r.evaluation))
            else:
                combined.add(await self.pr_stage.run(item, r.evaluation))

        log.info(combined.summary)
        return combined


# 标题相似度预筛用：过泛的词命中也不算强信号
_TITLE_STOPWORDS = frozenset({
    "the", "and", "for", "not", "but", "with", "you", "are", "can", "does", "issue",
    "bug", "fix", "error", "test", "add", "use", "new", "get", "set", "url", "api",
})

#: 预筛阈值。标题词面相似度与「正文标识符」相似度都以此为界。
_SIMILARITY_THRESHOLD = 0.22

#: 正文里「贴代码 / 贴日志」必然出现的样板词：任意两条报告之间都会重合，
#: 不能当重复信号（否则 import/print/python/return 就能把不相干的两条连起来）。
#: 剔除后剩下的才是「在说同一处代码」的证据。
_BODY_STOPWORDS = frozenset({
    "import", "print", "python", "python3", "return", "from", "def", "self", "class",
    "none", "true", "false", "assert", "value", "result", "items", "item", "args",
    "kwargs", "example", "code", "output", "input", "error", "test", "tests", "expect",
    "expected", "actual", "version", "readme", "pip", "install", "bash", "shell",
    "http", "https", "json", "traceback", "raise", "typeerror", "valueerror", "line",
})

#: 正文标识符参与比较的最小长度与最小共享个数。单个共享词（如两条都提到
#: ``coverage``）不足以说明同源，容易把同一模块下的不同问题混为一谈。
_BODY_MIN_LEN = 5
_BODY_MIN_SHARED = 2

#: 正文「高频词」的判定：文档频超过池子这个比例的词算到处都是的模块名，
#: 不具区分度（如 pipekit 里的 ``slices``）。用比例而非绝对数，随仓库规模自适应。
_BODY_DF_RATIO = 0.35

#: 正文通道的折算权重。正文远长于标题、词面必然更杂，故按包含度打折后再与
#: 标题分数取较大者——正文只做「补召回」，不改变标题已经能定的判断。
_BODY_SIM_WEIGHT = 1.5
_ASCII_TOKEN = re.compile(r"[0-9a-z_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _title_tokens(title: str) -> set[str]:
    """把标题切成用于相似度比较的 token（ASCII 词 + 中文 bigram）。

    中文若切成**单字**，字符会被严重稀释：两条语义高度重合的中文标题
    （「含税总价反推出来的不含税价好像不对」与「remove_tax 公式错误，含税价
    反推净额严重偏低」）单字交集虽不小，但分母同样被撑大，达不到预筛阈值，
    重复条目连候选都进不去——中文仓库上的系统性漏检（英文标题有 ``slugify``
    这类 ASCII 词直接抬分，所以此前没暴露）。

    改切**字符二元组**后，短语级重合（``含税``/``税价``/``反推``…）能被保留，
    单字版则可看作 bigram 版在长标题上退化的下界。与 ``_similar_to`` 的
    包含度配合，长标题对短标题的重复才不会被长度差淹没。

    本函数只用于**标题**；正文的重复证据由 :func:`_body_evidence` 单独提取
    （正文要剔除样板词与高频词，规则不同）。
    """
    text = (title or "").lower()
    out: set[str] = set()
    for tok in _ASCII_TOKEN.findall(text):
        if len(tok) >= 3 and tok not in _TITLE_STOPWORDS:
            out.add(tok)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i : i + 2] for i in range(len(run) - 1))
    return out


def _body_evidence(raw: set[str], df: dict[str, int], cutoff: int) -> set[str]:
    """从一条正文的 token 里取「可当重复证据」的标识符。

    只留 ASCII 标识符，并剔除两类干扰：

    * 样板词（``_BODY_STOPWORDS``）：贴代码必然出现的 ``import`` / ``print`` 等。
    * 高频词（df 超过 ``cutoff``）：在本仓库里到处都是的模块名，区分度低。

    剩下的才近似「这条报告具体在说哪处代码」。
    """
    return {
        tok
        for tok in raw
        if tok.isascii() and len(tok) >= _BODY_MIN_LEN and tok not in _BODY_STOPWORDS
        and df.get(tok, 0) <= cutoff
    }


def _containment(a: set[str], b: set[str]) -> float:
    """包含度：交集 / 较短一侧。对「一条啰嗦、一条精炼」的重复对不敏感。"""
    smaller = min(len(a), len(b))
    return len(a & b) / smaller if smaller else 0.0


def _similar_to(item: RawItem, pool: Sequence[RawItem], *, limit: int = 5) -> list[dict[str, Any]]:
    """挑出最可能重复的条目，作为**候选**交给 LLM 判断。

    只与**同类型、更早**的条目比较：

    * 同类型：Issue 的重复对象是别的 Issue，**不是「修它的那个 PR」**——否则
      #1 会被判成 #6（修 #1 的 PR）的重复，纯属假阳性。
    * 更早：重复关系单向指向更早的那条。否则两条互为候选、互相标记重复，
      而其中更早的往往才是该保留的正主。

    这是**面向召回**的预筛（宁可多给几条，绝不代替判断）：提示词已明确要求
    「只是内容相似，需结合内容判断，不要仅凭标题就判定重复」。共享有辨识度的
    ASCII 词（如 ``slugify``）是最强的词面重复信号，故直接抬到阈值以上。

    相似度用**包含度**（交集 / 较短一侧）而非 Jaccard：重复条目之间常是
    「一条啰嗦、一条精炼」，长度差会把 Jaccard 的分母撑大、相似度压低。
    包含度回答的是「较短一侧是否基本被较长一侧覆盖」，对长度差不敏感；
    又因只与**更早**条目单向比较，不存在互标重复的问题。

    阈值 0.22 在 ratekit 夹具上标定：真重复对（#8「含税总价反推出来的不含税价
    好像不对」~ #2「remove_tax 公式错误，含税价反推净额严重偏低」）得 0.286，
    而仅同属 ``parse_amount``、实为设计偏好而非重复的 #3~#9 只得 0.154。

    **标题之外还要看正文**：pipekit 实测 #12「count_batches 有时候会少算一个分片」
    与 #3「in_ranges 漏掉区间右端点，进而算错剩余分片数」是同一问题（后者是根因
    所在），但两条标题的标识符完全不同、词面包含度只有 0.10，仅靠标题连候选都
    进不去。而它们正文里都贴着同一段过滤逻辑，共享 ``count_batches`` / ``ranges``
    / ``stats`` 这些**辨识性标识符**。故正文按「剔除样板词与高频词后，共享的
    ASCII 标识符个数 >= ``_BODY_MIN_SHARED``」作为补召回通道——单个共享词
    （两条都提到 ``coverage``）不作数，避免把同模块下的不同问题混为一谈。
    """
    base = _title_tokens(item.title)
    if not base:
        return []

    # 正文证据需要文档频：先切出候选池各条的正文 token，再算每个词的 df。
    raw_body = {o.key: _title_tokens(getattr(o, "body", "") or "") for o in pool}
    df: dict[str, int] = {}
    for toks in raw_body.values():
        for tok in toks:
            df[tok] = df.get(tok, 0) + 1
    cutoff = max(_BODY_MIN_SHARED, int(len(pool) * _BODY_DF_RATIO))
    my_body = _body_evidence(raw_body.get(item.key, set()), df, cutoff)

    scored: list[tuple[float, RawItem]] = []
    for other in pool:
        if other.key == item.key:
            continue
        if other.item_type is not item.item_type or other.number >= item.number:
            continue
        toks = _title_tokens(other.title)
        if not toks:
            continue
        score = _containment(base, toks)
        if any(t.isascii() and t in toks for t in base):
            score = max(score, 0.5)
        # 正文补召回：共享足够多的辨识性标识符，说明在说同一处代码。
        other_body = _body_evidence(raw_body.get(other.key, set()), df, cutoff)
        if len(my_body & other_body) >= _BODY_MIN_SHARED:
            score = max(score, _BODY_SIM_WEIGHT * _containment(my_body, other_body))
        if score >= _SIMILARITY_THRESHOLD:
            scored.append((score, other))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "key": o.key,
            "number": o.number,
            "title": o.title,
            "state": o.state,
            "similarity": round(s, 2),
        }
        for s, o in scored[:limit]
    ]


#: 纯文档的文件名（大小写不敏感），及文档目录
_DOC_FILENAMES = frozenset({
    "readme", "readme.md", "readme.rst", "readme.txt",
    "changelog", "changelog.md", "changes", "changes.md",
    "license", "license.md", "licence", "copying",
    "contributing.md", "code_of_conduct.md", "authors", "notice",
})
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_DOC_DIRS = ("docs/", "doc/", "documentation/", "changelog.d/", ".github/")


#: 这些后缀是代码/配置，即便落在 docs/ 目录下也不算「纯文档」
_CODE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh", ".ps1", ".rb", ".go", ".rs", ".java",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift", ".kt",
    ".toml", ".ini", ".cfg", ".yaml", ".yml", ".json", ".lock",
)


def _is_doc_path(path: str) -> bool:
    """路径是否属于「文档」。用于判定「只改文档、没有代码实现」的 PR。"""
    p = (path or "").replace("\\", "/").lstrip("./").lower()
    if not p:
        return False
    if p.endswith(_CODE_SUFFIXES):
        # docs/conf.py、docs/build.sh 这类是代码，不是文档
        return False
    name = p.rsplit("/", 1)[-1]
    if name in _DOC_FILENAMES:
        return True
    if p.startswith(_DOC_DIRS):
        return True
    return p.endswith(_DOC_SUFFIXES)


def _base_reproduced(spec: Any, base_result: VerifierResult) -> bool:
    """base 阶段是否确认了「问题可复现」。

    与 ``judge_f2p`` 的判定保持一致：ERROR/TIMEOUT 视为结论不可信；
    ``expect_fail_on_base`` 为真时 base 必须**失败**（exit≠0）才算复现。
    """
    base = base_result.base_run
    if base is None or base.outcome in (VerifierOutcome.ERROR, VerifierOutcome.TIMEOUT):
        return False
    expect_fail = getattr(spec, "expect_fail_on_base", True)
    return (base.outcome is VerifierOutcome.FAIL) if expect_fail else True


def _run_output(run: Any) -> str:
    if run is None:
        return ""
    return f"[exit={run.exit_code} outcome={run.outcome.value}]\n{run.stdout}\n{run.stderr}"[:8000]


def _safe_cleanup(ws: RepoWorkspace) -> None:
    try:
        ws.cleanup()
    except Exception as exc:  # pragma: no cover
        log.warning("清理工作区失败：%s", exc)
