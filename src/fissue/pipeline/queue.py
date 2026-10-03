"""队列管理：入队、执行、批量 flush（Q9）。

三种队列
--------
``verify``       Issue-BUG：验证器执行队列
``fix_bug``      PR-BUG：合并 + 功能验证队列
``fix_feature``  PR-FEATURE：合并 + 功能验证队列（独立队列，共用沙盒）

触发条件（Q9 选 D：三者可配）
----------------------------
1. **数量阈值**：完成（done）但未 flush 的条目数 ≥ ``flush_size`` → 立即 flush。
2. **空闲超时**：队首完成条目已静默 ≥ ``idle_flush_seconds`` → flush。
   （常驻服务下「队列为空」几乎只在空闲时出现，所以这条才是真正的兜底。）
3. **手动触发**：``flush_now=True``（CLI / API 调用）。

flush 的含义：把这一批「已完成验证但未定论」的条目一次性交给 LLM 出结论并打标签，
避免逐条调用导致的上下文割裂与成本上升。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from ..logging_setup import get_logger
from ..models import Priority, QueueName
from ..store.repository import Repository
from .context import RuntimeContext

log = get_logger(__name__)

# flush 回调签名：(queue_kind, entries) -> 结论列表
FlushHandler = Callable[[str, Sequence[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]


@dataclass
class FlushDecision:
    """是否该 flush 的判定结果。"""

    should_flush: bool
    reason: str = ""
    done_count: int = 0
    idle_seconds: float | None = None
    entry_ids: list[int] = field(default_factory=list)


@dataclass
class FlushOutcome:
    """一次 flush 的执行结果。"""

    queue: str
    batch_id: str
    flushed: int = 0
    conclusions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class QueueManager:
    """队列的统一入口。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.repo: Repository = ctx.repo
        self.settings = ctx.settings

    # -- 入队 -------------------------------------------------------------

    def enqueue(
        self,
        queue: QueueName,
        item_key: str,
        *,
        item_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        entry_id = self.repo.enqueue(queue, item_key, item_id=item_id, payload=payload)
        log.debug("入队 %s -> %s（entry=%d）", queue.value, item_key, entry_id)
        return entry_id

    def pending_count(self, queue: QueueName) -> int:
        return self.repo.queue_pending_count(queue)

    def stats(self) -> dict[str, dict[str, int]]:
        return self.repo.queue_stats()

    # -- flush 判定 -------------------------------------------------------

    def should_flush(self, queue: QueueName, *, force: bool = False) -> FlushDecision:
        """判断某个队列是否该 flush。"""
        spec = self._spec(queue)
        unflushed = self.repo.queue_unflushed(queue)
        entry_ids = [int(e.id) for e in unflushed]

        if force:
            return FlushDecision(
                True, "手动触发", done_count=len(entry_ids), entry_ids=entry_ids
            )
        if not entry_ids:
            return FlushDecision(False, "没有已完成待定论的条目", done_count=0)

        if len(entry_ids) >= spec.flush_size:
            return FlushDecision(
                True,
                f"已达数量阈值（{len(entry_ids)}/{spec.flush_size}）",
                done_count=len(entry_ids),
                entry_ids=entry_ids,
            )

        age = self.repo.oldest_unflushed_age(queue)
        if age is not None and age >= spec.idle_flush_seconds and spec.idle_flush_seconds > 0:
            return FlushDecision(
                True,
                f"已空闲 {age:.0f}s（阈值 {spec.idle_flush_seconds}s）",
                done_count=len(entry_ids),
                idle_seconds=age,
                entry_ids=entry_ids,
            )

        # 还有 pending/running → 等它跑完再一起 flush
        pending = self.repo.queue_pending_count(queue)
        if pending == 0:
            return FlushDecision(
                True,
                f"队列已无待处理条目（仅 {len(entry_ids)} 条待定论）",
                done_count=len(entry_ids),
                idle_seconds=age,
                entry_ids=entry_ids,
            )

        return FlushDecision(
            False,
            f"等待中（待定论 {len(entry_ids)}，未完成 {pending}）",
            done_count=len(entry_ids),
            idle_seconds=age,
        )

    def flushable_queues(self) -> list[QueueName]:
        return [q for q in QueueName if self.should_flush(q).should_flush]

    # -- flush 执行 -------------------------------------------------------

    async def flush(
        self,
        queue: QueueName,
        *,
        handler: FlushHandler,
        force: bool = False,
        limit: int = 50,
    ) -> FlushOutcome:
        """执行一次 flush：取出一批已完成条目 → 交给 handler 出结论 → 落库并标记已 flush。"""
        batch_id = uuid.uuid4().hex[:16]
        decision = self.should_flush(queue, force=force)
        if not decision.should_flush:
            return FlushOutcome(queue=queue.value, batch_id=batch_id, error=None, flushed=0)

        entries = self.repo.queue_unflushed(queue, limit=limit)
        if not entries:
            return FlushOutcome(queue=queue.value, batch_id=batch_id, flushed=0)

        payloads = [self._build_payload(queue, e) for e in entries]
        try:
            conclusions = await handler(queue.value, payloads)
        except Exception as exc:
            log.exception("flush %s 失败", queue.value)
            return FlushOutcome(
                queue=queue.value, batch_id=batch_id, flushed=0, error=f"{type(exc).__name__}: {exc}"
            )

        # 落库结论
        by_key = {str(p.get("key")): p for p in payloads}
        for c in conclusions:
            key = str(c.get("key") or "")
            if key not in by_key:
                continue
            self.repo.save_batch_conclusion(
                key,
                batch_id,
                verdict=str(c.get("verdict") or ""),
                labels=[str(x) for x in (c.get("labels") or [])],
                priority=c.get("priority") if isinstance(c.get("priority"), Priority) else Priority.NONE,
                reason=str(c.get("reason") or ""),
                confidence=float(c.get("confidence") or 0.5),
                model=str(c.get("model") or ""),
            )

        self.repo.mark_flushed([int(e.id) for e in entries])
        log.info(
            "flush %s 完成（batch=%s，%d 条，结论 %d 条）：%s",
            queue.value, batch_id, len(entries), len(conclusions), decision.reason,
        )
        return FlushOutcome(
            queue=queue.value, batch_id=batch_id, flushed=len(entries), conclusions=conclusions
        )

    async def flush_all(self, *, handler: FlushHandler, force: bool = False) -> list[FlushOutcome]:
        """按需 flush 所有队列。"""
        out: list[FlushOutcome] = []
        for queue in QueueName:
            decision = self.should_flush(queue, force=force)
            if not decision.should_flush:
                continue
            out.append(await self.flush(queue, handler=handler, force=force))
        return out

    # -- 内部 -------------------------------------------------------------

    def _spec(self, queue: QueueName):
        # 验证队列与修复队列各自可配
        if queue is QueueName.VERIFY:
            return self.settings.queues.verify_queue
        return self.settings.queues.fix_queue

    def _build_payload(self, queue: QueueName, entry: Any) -> dict[str, Any]:
        """把队列条目 + 条目元数据 + 验证器结果组装成交给 LLM 的材料。"""
        key = entry.item_key
        item = self.repo.get_item(key)
        evaluation = self.repo.latest_evaluation(key)
        runs = self.repo.verifier_runs(key, limit=6)

        base_run = next((r for r in runs if r.stage == "base"), None)
        fix_run = next((r for r in runs if r.stage in ("fix", "merged")), None)
        verifier = self.repo.latest_verifier(key)
        result = entry.result or {}

        output = ""
        if fix_run is not None:
            output = (fix_run.stdout or "") + "\n" + (fix_run.stderr or "")
        elif base_run is not None:
            output = (base_run.stdout or "") + "\n" + (base_run.stderr or "")
        if result.get("output"):
            output = str(result["output"]) + "\n" + output

        return {
            "key": key,
            "queue": queue.value,
            "title": item.title if item else "",
            "category": (item.category.value if item else ""),
            "item_type": item.item_type.value if item else "",
            "repo": item.repo if item else "",
            "number": item.number if item else 0,
            "scores": evaluation.scores.as_dict() if evaluation else {},
            "priority": evaluation.priority.value if evaluation else "none",
            "verifier_kind": verifier[1].kind.value if verifier else "",
            "base_outcome": base_run.outcome.value if base_run else "",
            "base_exit": base_run.exit_code if base_run else None,
            "fix_outcome": fix_run.outcome.value if fix_run else "",
            "fix_exit": fix_run.exit_code if fix_run else None,
            "f2p": bool(fix_run.f2p_ok) if fix_run and fix_run.f2p_ok is not None else None,
            "output": output[:8000],
            "entry_result": result,
        }

    # -- 常驻循环辅助 -----------------------------------------------------

    async def run_flush_loop(
        self,
        *,
        handler: FlushHandler,
        interval_seconds: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """常驻：周期性检查并 flush（空闲超时兜底就靠它）。"""
        interval = interval_seconds or self.settings.schedule.queue_flush_interval_seconds
        log.info("队列 flush 循环启动（每 %ds 检查一次）", interval)
        while stop_event is None or not stop_event.is_set():
            try:
                await self.flush_all(handler=handler)
            except Exception:
                log.exception("flush 循环出错（继续）")
            try:
                if stop_event is not None:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                else:
                    await asyncio.sleep(interval)
            except asyncio.TimeoutError:
                continue
        log.info("队列 flush 循环退出")
