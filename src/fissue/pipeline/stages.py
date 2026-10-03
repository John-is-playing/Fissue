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
)
from ..sandbox.protocol import MountSpec
from ..verifier.runner import CONTAINER_WORKDIR, VerifyContext
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

        category = evaluation.category if evaluation else Category.FEATURE
        result.category = category
        queue = QueueName.FIX_BUG if category is Category.BUG else QueueName.FIX_FEATURE

        repo_cfg = self._repo_config(item)
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
            if r.evaluation.category is not Category.BUG:
                # FEATURE：只打标签，等开发者
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
_TITLE_TOKEN = re.compile(r"[0-9a-z_]+|[\u4e00-\u9fff]")


def _title_tokens(title: str) -> set[str]:
    """把标题切成用于相似度比较的 token（ASCII 词 + 中文单字）。"""
    out: set[str] = set()
    for tok in _TITLE_TOKEN.findall((title or "").lower()):
        if tok[0].isascii():
            if len(tok) >= 3 and tok not in _TITLE_STOPWORDS:
                out.add(tok)
        else:
            out.add(tok)
    return out


def _similar_to(item: RawItem, pool: Sequence[RawItem], *, limit: int = 5) -> list[dict[str, Any]]:
    """挑出标题上最可能重复的条目，作为**候选**交给 LLM 判断。

    只与**同类型、更早**的条目比较：

    * 同类型：Issue 的重复对象是别的 Issue，**不是「修它的那个 PR」**——否则
      #1 会被判成 #6（修 #1 的 PR）的重复，纯属假阳性。
    * 更早：重复关系单向指向更早的那条。否则两条互为候选、互相标记重复，
      而其中更早的往往才是该保留的正主。

    这是**面向召回**的预筛（宁可多给几条，绝不代替判断）：提示词已明确要求
    「只是标题相似，需结合内容判断，不要仅凭标题就判定重复」。共享有辨识度的
    ASCII 词（如 ``slugify``）是最强的词面重复信号，故直接抬到阈值以上。
    """
    base = _title_tokens(item.title)
    if not base:
        return []
    scored: list[tuple[float, RawItem]] = []
    for other in pool:
        if other.key == item.key:
            continue
        if other.item_type is not item.item_type or other.number >= item.number:
            continue
        toks = _title_tokens(other.title)
        if not toks:
            continue
        inter, union = base & toks, base | toks
        score = len(inter) / len(union) if union else 0.0
        if any(t.isascii() and t in toks for t in base):
            score = max(score, 0.5)
        if score >= 0.25:
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
