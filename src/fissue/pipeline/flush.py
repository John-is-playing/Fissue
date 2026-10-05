"""批量 flush 处理器与修复派发（Q1 的批量环节）。

衔接关系
--------
::

    verify 队列 flush  ──► LLM 批量结论 ──► 打标签 + 定优先级 ──┐
                                                              ├─► tier1/tier2 → 自动修复
    fix_bug / fix_feature 队列 flush ──► LLM 批量结论 ──► 打标签（等人工合并）

要点
----
* 打标签是**写操作**，失败不能影响流水线（记日志继续）。
* 只有 ``verdict`` 明确为 ``fix`` 且优先级为 tier1/tier2 的 Issue 才进入自动修复。
* PR 的结论**只做建议**（``merge``），永不自动合并——最终由维护者手动合并。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..logging_setup import get_logger
from ..models import (
    Category,
    ItemStatus,
    ItemType,
    Priority,
    QueueName,
    RawItem,
)
from ..platforms.registry import parse_repo_ref
from .context import RuntimeContext
from .queue import QueueManager

log = get_logger(__name__)

# 我方自动打的标签（用于识别 AI 产物与状态）
LABEL_VERIFIED = "ai-verified"
LABEL_REPRODUCED = "reproduced"
LABEL_NEEDS_REVIEW = "needs-review"
LABEL_INVALID = "invalid"
LABEL_DUPLICATE = "duplicate"


@dataclass
class FlushReport:
    """一次 flush 的处理明细。"""

    queue: str = ""
    batch_id: str = ""
    labeled: list[str] = field(default_factory=list)
    fix_candidates: list[tuple[str, Priority]] = field(default_factory=list)
    label_errors: list[str] = field(default_factory=list)
    verdicts: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return (
            f"[{self.queue}] 结论 {len(self.verdicts)} 条，"
            f"打标 {len(self.labeled)}，待修复 {len(self.fix_candidates)}，"
            f"打标失败 {len(self.label_errors)}"
        )


class FlushProcessor:
    """把 flush 结论落到平台（打标签）与流水线（派发修复）。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.queues = QueueManager(ctx)

    # -- 提供给 QueueManager 的 handler -----------------------------------

    def make_handler(self):
        """构造 ``(queue_kind, entries) -> conclusions`` 回调。"""

        async def handler(queue_kind: str, entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
            conclusions = await self.ctx.evaluator.conclude_batch(
                queue_kind="verify" if queue_kind == QueueName.VERIFY.value else "fix",
                entries=entries,
            )
            report = await self.apply(queue_kind, entries, conclusions)
            log.info(report.summary)
            return conclusions

        return handler

    async def flush_once(self, *, force: bool = True, queue: QueueName | None = None):
        """便捷入口：手动 flush（CLI / API 用）。"""
        handler = self.make_handler()
        if queue is not None:
            return [await self.queues.flush(queue, handler=handler, force=force)]
        return await self.queues.flush_all(handler=handler, force=force)

    # -- 应用结论 ---------------------------------------------------------

    async def apply(
        self,
        queue_kind: str,
        entries: Sequence[dict[str, Any]],
        conclusions: Sequence[dict[str, Any]],
    ) -> FlushReport:
        report = FlushReport(queue=queue_kind, batch_id="")
        by_key = {str(e.get("key")): e for e in entries}

        for c in conclusions:
            key = str(c.get("key") or "")
            payload = by_key.get(key)
            if payload is None:
                continue
            verdict = str(c.get("verdict") or "").lower()
            report.verdicts[key] = verdict
            priority = c.get("priority") if isinstance(c.get("priority"), Priority) else Priority.NONE

            # 优先级**一律以规则结论为准**，LLM 的 priority 仅作参考。否则模型随手给个
            # tier2 就能绕过 fix_policy 的阈值与 only_issues，把高难度条目派去自动修复；
            # 对 PR 更会写出「items.priority=tier1」这类与规则（only_issues 下 PR 恒为
            # none）自相矛盾的值，污染报告 / 看板 / API。
            priority = self._rule_priority(key, payload, fallback=priority)
            # 回写结论：queue 落库的 batch_conclusions 与 items.priority 必须与派发口径一致。
            c["priority"] = priority

            labels = self._decide_labels(queue_kind, verdict, c)
            if labels:
                ok, err = await self._safe_label(payload, labels)
                if ok:
                    report.labeled.append(key)
                else:
                    report.label_errors.append(f"{key}: {err}")

            if queue_kind == QueueName.VERIFY.value:
                # Issue-BUG：只有明确 fix 且优先级为 tier1/tier2 才派发自动修复
                if verdict == "fix" and priority in (Priority.TIER1, Priority.TIER2):
                    report.fix_candidates.append((key, priority))
                    self.ctx.repo.set_item_status(key, ItemStatus.FIX_QUEUED, priority=priority)
                elif verdict == "skip":
                    self.ctx.repo.set_item_status(key, ItemStatus.SKIPPED)
                else:
                    self.ctx.repo.set_item_status(key, ItemStatus.NEEDS_MANUAL)
            else:
                # PR：只建议，不自动合并
                if verdict == "merge":
                    self.ctx.repo.set_item_status(key, ItemStatus.LABELED)
                elif verdict in ("reject", "skip"):
                    self.ctx.repo.set_item_status(key, ItemStatus.NEEDS_MANUAL)
                else:
                    self.ctx.repo.set_item_status(key, ItemStatus.NEEDS_MANUAL)

        return report

    # -- 优先级 -----------------------------------------------------------

    def _rule_priority(
        self, key: str, payload: dict[str, Any], *, fallback: Priority = Priority.NONE
    ) -> Priority:
        """按 ``fix_policy`` 规则重算派发优先级（权威来源）。

        评测阶段算出的优先级就是 ``compute_priority`` 的结论，这里用同一把尺子，
        保证「报告里的优先级」与「实际派发去修什么」完全一致。取不到评测结论时
        才退回 LLM 给的参考值。
        """
        ev = self.ctx.repo.latest_evaluation(key)
        if ev is None:
            return fallback

        item_type = None
        item = None
        try:
            item = self.ctx.repo.get_item(key)
        except Exception as exc:  # pragma: no cover - 仅防御
            log.warning("读取条目失败 %s：%s", key, exc)
        if item is not None:
            item_type = item.item_type
        else:
            try:
                item_type = ItemType(payload.get("item_type") or "issue")
            except ValueError:
                item_type = None

        from ..ai.evaluator import compute_priority

        rule = compute_priority(
            ev, category=ev.category, policy=self.settings.fix_policy, item_type=item_type
        )
        if rule is not fallback:
            log.debug("派发优先级以规则为准 %s：模型 %s → 规则 %s", key, fallback.value, rule.value)
        return rule

    # -- 标签 -------------------------------------------------------------

    def _decide_labels(self, queue_kind: str, verdict: str, conclusion: dict[str, Any]) -> list[str]:
        """合并「LLM 建议标签」与「我方状态标签」。"""
        labels: list[str] = [str(x) for x in (conclusion.get("labels") or []) if str(x).strip()]

        if queue_kind == QueueName.VERIFY.value:
            mapping = {
                "fix": LABEL_REPRODUCED,
                "merge": LABEL_VERIFIED,
                "reject": LABEL_INVALID,
                "skip": LABEL_NEEDS_REVIEW,
                "needs_review": LABEL_NEEDS_REVIEW,
            }
        else:
            mapping = {
                "merge": LABEL_VERIFIED,
                "fix": LABEL_REPRODUCED,
                "reject": LABEL_NEEDS_REVIEW,
                "skip": LABEL_NEEDS_REVIEW,
                "needs_review": LABEL_NEEDS_REVIEW,
            }
        status_label = mapping.get(verdict)
        if status_label and status_label not in labels:
            labels.append(status_label)
        labels.append("fissue")          # 统一来源标记
        return _dedupe(labels)

    async def _safe_label(self, payload: dict[str, Any], labels: list[str]) -> tuple[bool, str]:
        """给条目打标签（失败只记录，不抛出）。"""
        try:
            from ..config import RepoConfig

            repo_slug = str(payload.get("repo") or "")
            item_type = ItemType(payload.get("item_type") or "issue")
            platform = None
            try:
                repo_cfg = self.ctx.settings.repo(repo_slug)
            except Exception:
                from ..models import Platform as _P

                owner, _, name = repo_slug.partition("/")
                repo_cfg = RepoConfig(platform=_P.GITHUB, owner=owner, name=name)
                platform = repo_cfg.platform

            adapter = await self.ctx.adapter_for_repo(repo_cfg)
            ref = parse_repo_ref(repo_slug, repo_cfg.platform if platform is None else platform)
            for lbl in labels[:6]:          # 先确保标签存在，再打
                await adapter.ensure_label(ref, lbl)
            await adapter.add_labels(ref, int(payload.get("number") or 0), labels, item_type)
            return True, ""
        except Exception as exc:
            log.warning("打标签失败 %s：%s", payload.get("key"), exc)
            return False, str(exc)[:200]

    # -- 修复优先级批量规划 ----------------------------------------------

    async def plan_priorities(self, items: Sequence[RawItem]) -> dict[str, Priority]:
        """对一批已评测的 Issue-BUG 批量规划修复优先级（Q1 规则）。"""
        rows: list[dict[str, Any]] = []
        for item in items:
            if item.item_type is not ItemType.ISSUE:
                continue
            ev = self.ctx.repo.latest_evaluation(item.key)
            if ev is None or ev.category is not Category.BUG:
                continue
            verifier = self.ctx.repo.latest_verifier(item.key)
            runs = self.ctx.repo.verifier_runs(item.key, stage="base", limit=1)
            rows.append(
                {
                    "key": item.key,
                    "title": item.title,
                    "difficulty": ev.difficulty,
                    "importance": ev.importance,
                    "feasibility": ev.scores.feasibility.score if ev.scores.feasibility else 0,
                    "authenticity": ev.authenticity,
                    "verified": bool(runs and runs[0].outcome.value == "fail"),
                    "verifier_kind": verifier[1].kind.value if verifier else "",
                }
            )
        if not rows:
            return {}

        planned = await self.ctx.evaluator.plan_fix_priority(rows)
        out: dict[str, Priority] = {}
        for key, info in planned.items():
            prio = info.get("priority") if isinstance(info.get("priority"), Priority) else Priority.NONE
            out[key] = prio
            self.ctx.repo.set_item_status(key, ItemStatus.FIX_QUEUED, priority=prio)
        return out

    def fix_candidates(self, *, repo_slug: str | None = None, limit: int = 20) -> list[RawItem]:
        """取出可以自动修复的条目（优先级 tier1 / tier2）。

        只取仍在 ``FIX_QUEUED`` 的条目：``priority`` 是评测阶段落的「值不值得修」，
        修完之后不会清掉，所以不能拿它当「还没修过」的依据。否则一条已转
        ``needs_manual`` / ``pr_created`` 的条目会因为 priority 还在而被反复选中——
        每轮都从第一批开始重修，后面的条目永远轮不到（envkit 实测：四轮都在
        重复处理同一批 #9/#8/#7）。

        与常驻服务 ``daemon._dispatch_fixes`` 的口径保持一致，两处都按
        ``FIX_QUEUED`` 取候选。
        """
        out: list[RawItem] = []
        for prio in (Priority.TIER1, Priority.TIER2):
            out.extend(
                self.ctx.repo.list_items(
                    repo_slug=repo_slug,
                    item_type=ItemType.ISSUE,
                    status=ItemStatus.FIX_QUEUED,
                    priority=prio,
                    limit=limit,
                )
            )
        # 保持 tier1 在前（优先级更高）
        order = {Priority.TIER1: 0, Priority.TIER2: 1, Priority.NONE: 2}
        out.sort(key=lambda i: order.get(i.priority, 3))
        return out[:limit]


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        norm = str(it).strip().lower().replace(" ", "-")
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out[:8]
