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

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

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
    #: 本次 flush 因条目已在修复流程中（或已修过）而放弃派发决定的条目
    skipped_in_fix: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"[{self.queue}] 结论 {len(self.verdicts)} 条，"
            f"打标 {len(self.labeled)}，待修复 {len(self.fix_candidates)}，"
            f"打标失败 {len(self.label_errors)}，修复流程中跳过 {len(self.skipped_in_fix)}"
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

        prepared: list[dict[str, Any]] = []
        for c in conclusions:
            key = str(c.get("key") or "")
            payload = by_key.get(key)
            if payload is None:
                continue
            verdict = str(c.get("verdict") or "").lower()
            report.verdicts[key] = verdict
            raw_priority = c.get("priority")
            priority: Priority = (
                raw_priority if isinstance(raw_priority, Priority) else Priority.NONE
            )

            # 优先级**一律以规则结论为准**，LLM 的 priority 仅作参考。否则模型随手给个
            # tier2 就能绕过 fix_policy 的阈值与 only_issues，把高难度条目派去自动修复；
            # 对 PR 更会写出「items.priority=tier1」这类与规则（only_issues 下 PR 恒为
            # none）自相矛盾的值，污染报告 / 看板 / API。
            priority = self._rule_priority(key, payload, fallback=priority)
            # 回写结论：queue 落库的 batch_conclusions 与 items.priority 必须与派发口径一致。
            c["priority"] = priority

            prepared.append(
                {
                    "key": key,
                    "payload": payload,
                    "verdict": verdict,
                    "priority": priority,
                    "labels": self._decide_labels(queue_kind, verdict, c),
                }
            )

        # 打标签并发跑：每条一次 add_labels，彼此独立（无共享状态、无顺序依赖）。
        # 串行会把「每条一次 HTTP」直接叠加成 N 倍墙钟时间——实测 10 条要 39s。
        # 结果按结论顺序回填，保证报告里 labeled / label_errors 的顺序稳定。
        if prepared:
            results = await self._label_all(
                [(p["payload"], p["labels"]) for p in prepared]
            )
            for p, (ok, err) in zip(prepared, results, strict=True):
                if not p["labels"]:
                    continue
                if ok:
                    report.labeled.append(p["key"])
                else:
                    report.label_errors.append(f'{p["key"]}: {err}')

        for p in prepared:
            key = p["key"]
            verdict = p["verdict"]
            priority = p["priority"]

            if queue_kind == QueueName.VERIFY.value:
                it = self.ctx.repo.get_item(key)
                if (
                    it is not None
                    and it.status in (
                        ItemStatus.FIXING,
                        ItemStatus.PR_CREATED,
                        ItemStatus.FIX_QUEUED,
                    )
                ):
                    # 条目已在修复流程中（或已提过 PR）：这条队列条目是残留/迟到者。
                    # 再落回 fix_queued 会给 pr_created 的条目开第二个 PR、或抢走别的
                    # 进程正在修的条目——派发决定弃掉（条目本身照常标记已 flush）。
                    log.info(
                        "flush 跳过条目 %s 的派发：条目已在修复流程中（status=%s）",
                        key, it.status.value,
                    )
                    report.skipped_in_fix.append(key)
                    continue
                # 强制修复：结论已人拍板，优先于 LLM 的（含 skip）。也覆盖「强制前已在
                # 验证队列里」的残留条目——它就该直落待修，不能被裁决判成 SKIPPED / 需人工。
                if it is not None and it.force_fix:
                    if self.ctx.repo.fix_attempts(key):
                        # 已尝试过修复（无论成败）：强制决定已被执行。失败的尝试不挡
                        # _has_open_attempt（它只拦 SUCCESS），残留条目每被 flush 一次
                        # 就把条目重新拉回待修——常驻服务会反复重修同一条目、每轮
                        # 烧一整轮 agent。
                        log.info("flush 跳过 %s 的重新入队：已有修复尝试（残留条目）", key)
                        report.skipped_in_fix.append(key)
                        continue
                    report.fix_candidates.append((key, Priority.TIER1))
                    self.ctx.repo.set_item_status(
                        key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1
                    )
                # Issue-BUG：只有明确 fix 且优先级为 tier1/tier2 才派发自动修复
                elif verdict == "fix" and priority in (Priority.TIER1, Priority.TIER2):
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

    async def _label_all(
        self, items: Sequence[tuple[dict[str, Any], list[str]]]
    ) -> list[tuple[bool, str]]:
        """并发给多条打标签，返回与输入**同序**的 ``(是否成功, 错误)``。

        条目之间彼此独立（不同 issue 编号、各自的标签调用），并发是安全的；
        标签本身的「是否已存在」由适配器层的缓存 + 锁保证，不会重复创建。

        标签列表的对齐（见 :meth:`_refresh_labels`）放在 :meth:`_safe_label`
        里，而不是这里统一预跑一遍——那样会把「打标签」这一步从既有的
        打桩缝里挤出去，且每个仓库都要先走一次真实请求。
        """
        if not items:
            return []
        # 打标签是纯平台 HTTP、不消耗 token：优先用平台侧自己的并发上限，
        # 未配置时才退回 llm.concurrency（避免把纯 HTTP 与 LLM 限流绑死）。
        configured = getattr(self.settings.platforms, "label_concurrency", None)
        try:
            limit = max(1, int(configured if configured is not None else self.settings.llm.concurrency))
        except Exception:  # pragma: no cover - 配置异常时退化为串行
            limit = 1
        sem = asyncio.Semaphore(limit)

        async def one(payload: dict[str, Any], labels: list[str]) -> tuple[bool, str]:
            async with sem:
                return await self._safe_label(payload, labels)

        return list(await asyncio.gather(*(one(p, labels) for p, labels in items)))

    def _repo_config_for(self, payload: dict[str, Any]) -> Any:
        """从队列载荷解析出仓库配置（未登记则按载荷里的平台临时构造）。"""
        from ..config import RepoConfig

        repo_slug = str(payload.get("repo") or "")
        try:
            return self.ctx.settings.repo(repo_slug)
        except Exception:
            owner, _, name = repo_slug.partition("/")
            platform = None
            if payload.get("platform"):
                from ..models import Platform as _P

                try:
                    platform = _P(str(payload["platform"]).lower())
                except ValueError:
                    platform = None
            if platform is None:
                # 未登记仓库 + 载荷未带平台：沿用配置里的默认平台（与旧行为一致）
                platform = self.ctx.settings.repos[0].platform if self.ctx.settings.repos else None
            if platform is None:
                raise ValueError(f"无法确定仓库平台：{repo_slug}") from None
            return RepoConfig(platform=platform, owner=owner, name=name)

    async def _safe_label(self, payload: dict[str, Any], labels: list[str]) -> tuple[bool, str]:
        """给条目打标签（失败只记录，不抛出）。

        打标签前先把目标仓库的标签列表对齐一次（见 :meth:`_refresh_labels`）：
        ``ensure_labels`` 判「标签是否已存在」靠的就是这份缓存，缓存在常驻
        服务里迟早会陈旧，而它一旦判错就会走到创建、吃到 422 并抛异常。
        """
        try:
            repo_cfg = self._repo_config_for(payload)
            adapter = await self.ctx.adapter_for_repo(repo_cfg)
            ref = parse_repo_ref(str(payload.get("repo") or ""), repo_cfg.platform)
            await self._refresh_labels(adapter, ref, key=str(payload.get("key") or ""))
            # 一次批量确保标签存在：原来逐个 ensure_label 会为**每个标签**各拉一遍
            # 全量标签列表（还有分页），是 flush 打标阶段的主要耗时来源。
            await adapter.ensure_labels(ref, labels[:6])
            # 编号按平台原样传：Gitee 的 Issue 编号是字母编码（`IKKIOZ`），int() 会直接崩，
            # 而那个编码恰好就是它打标签端点要的标识。
            await adapter.add_labels(
                ref,
                payload.get("number") or 0,
                labels,
                ItemType(payload.get("item_type") or "issue"),
            )
            # 写成功之后把结果并回本地快照。少了这一步，下一次抓取必然读到「平台 ≠ 库里」
            # 的标签集 → 判成内容实质变化 → 把我们**刚做出的结论**作废并重评，每打一次标
            # 就白烧一轮 classify + evaluate + verify（详见 Repository.merge_item_labels）。
            self.ctx.repo.merge_item_labels(str(payload.get("key") or ""), labels)
            return True, ""
        except Exception as exc:
            log.warning("打标签失败 %s：%s", payload.get("key"), exc)
            return False, str(exc)[:200]

    async def _refresh_labels(self, adapter: Any, ref: Any, *, key: str = "") -> bool:
        """打标签前对齐一次目标仓库的标签列表。

        仅在缓存缺失或已过 TTL 时才真正发请求；同一仓库在一轮 flush 里
        至多对齐一次（对齐后写入时刻被推到现在，后续条目都命中缓存）。
        ``TTL <= 0``（关缓存）时不需要对齐——``ensure_labels`` 自己会实拉。

        失败**不阻断**打标签：对齐属于加固，拿不到最新列表时按缓存继续，
        真正的失败（如 422）仍由调用方 ``_safe_label`` 记录。
        返回是否真的拉取了一次。
        """
        ttl = float(getattr(adapter, "label_cache_ttl", 0.0) or 0.0)
        if ttl <= 0:
            return False
        try:
            age = adapter.label_cache_age(ref)
            if age is not None and age < ttl:
                return False              # 缓存仍新鲜
            await adapter.cached_repo_labels(ref, refresh=True)
            log.debug(
                "已对齐标签列表：%s（原缓存年龄 %s）", ref.slug, "-" if age is None else f"{age:.0f}s"
            )
            return True
        except Exception as exc:          # pragma: no cover - 平台异常时按缓存继续
            log.warning("对齐远端标签列表失败 %s（按缓存继续）：%s", key or ref.slug, exc)
            return False

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
            raw_prio = info.get("priority")
            prio: Priority = raw_prio if isinstance(raw_prio, Priority) else Priority.NONE
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
