"""常驻服务：增量扫描 + 定时调度 + 队列 flush + 阈值通知（Q4）。

调度循环
--------
::

    ┌─ 扫描循环（默认每小时）
    │     抓取新条目（增量）→ 通知 scan_done
    ├─ 评测循环（紧跟扫描）
    │     评测 NEW 条目 → 高重要性告警
    ├─ flush 循环（默认每 60s）
    │     按「数量阈值 / 空闲超时」flush 队列 → 打标签 → 派发修复
    └─ 修复循环（紧跟 flush，受策略与预算限制）

设计要点
--------
* 各循环是**独立的 asyncio 任务**，互不阻塞；单个仓库出错不影响其他仓库。
* 优雅退出：收到 SIGINT/SIGTERM 后置位 ``stop_event``，等任务收尾。
* 预算熔断：某仓库当天超预算时跳过该仓库并告警，不中断整轮。
* ``--once`` 模式跑一轮就退出，方便交给外部 cron。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import RepoConfig, Settings
from ..errors import BudgetExceeded
from ..logging_setup import get_logger
from ..models import Category, ItemStatus, ItemType, Priority, QueueName, RawItem
from ..pipeline.context import RuntimeContext
from ..pipeline.fetcher import Fetcher
from ..pipeline.flush import FlushProcessor
from ..pipeline.queue import QueueManager
from ..pipeline.stages import EvalStage
from .notify import Notifier

log = get_logger(__name__)


class FissueService:
    """常驻服务本体。"""

    def __init__(self, settings: Settings, *, with_web: bool = False) -> None:
        self.settings = settings
        self.with_web = with_web
        self.stop_event = asyncio.Event()
        self._last_scan: datetime | None = None

    # -- 生命周期 ---------------------------------------------------------

    async def run_once(self) -> dict[str, Any]:
        """跑完整的一轮：扫描 → 评测 → flush → 修复。"""
        from ..pipeline.context import RuntimeContext

        async with RuntimeContext.open(self.settings, exclusive=True) as ctx:
            ctx.init_db()
            notifier = Notifier(self.settings.notify, self.settings, ctx.repo)
            result: dict[str, Any] = {}

            repos = ctx.all_repos()
            if not repos:
                log.warning("配置里没有启用的仓库，无事可做")
                return {"repos": 0}

            # 1) 增量扫描
            full = self._need_full_rescan(ctx)
            fetcher = Fetcher(ctx)
            stats = await fetcher.fetch_many(repos, full=full, concurrency=2)
            result["fetch"] = {
                "fetched": stats.fetched,
                "created": stats.created,
                "updated": stats.updated,
                "unchanged": stats.unchanged,
            }
            for cfg in repos:
                await notifier.notify_scan(repo_slug=cfg.slug, stats=stats)
            self._last_scan = datetime.now(timezone.utc)

            # 2) 评测新条目
            eval_stage = EvalStage(ctx)
            evaluated = 0
            for cfg in repos:
                items = ctx.repo.list_items(repo_slug=cfg.slug, status=ItemStatus.NEW, limit=50)
                if not items:
                    continue
                if not self._budget_ok(ctx, cfg, notifier):
                    continue
                try:
                    res = await eval_stage.run_batch(items, concurrency=3)
                    evaluated += res.evaluated
                    await self._alert_high_importance(ctx, cfg, res.results, notifier)
                except BudgetExceeded as exc:
                    log.warning("预算熔断：%s", exc)
                    await notifier.notify_budget(repo_key=cfg.slug, used=ctx.usage_report(cfg.slug))
                except Exception:
                    log.exception("评测 %s 出错（继续）", cfg.slug)
            result["evaluated"] = evaluated

            # 3) 验证：给刚评测完的条目生成验证器并跑 F2P
            #
            # 此前 daemon **没有**这一步（只做 fetch → eval → flush → fix），于是常驻模式下
            # 条目停在 `evaluated`、再也不会往下走——「部署成服务它自己就能转」这条链是断的，
            # 验证只能靠人工 `fissue verify` / 调 API。这里补上。
            result["verify"] = await self._verify_pending(ctx, notifier)

            # 4) flush 队列 + 派发修复
            flush_info = await self._flush_queues(ctx, notifier)
            result["flush"] = flush_info

            # 5) 自动修复（仅 tier1/tier2，受策略与预算限制）
            result["fix"] = await self._run_fixes(ctx, notifier)

            log.info("单轮完成：%s", result)
            return result

    async def run_forever(self) -> None:
        """常驻运行，直到收到停止信号。"""
        self._install_signal_handlers()
        log.info(
            "Fissue 服务启动：扫描间隔 %ds，flush 间隔 %ds%s",
            self.settings.schedule.scan_interval_seconds,
            self.settings.schedule.queue_flush_interval_seconds,
            "（含 Web）" if self.with_web else "",
        )

        tasks: list[asyncio.Task] = []
        if not self.settings.schedule.enabled:
            # 关掉调度：不自动扫描 / flush / 修复。想手动跑一轮用 `fissue serve --once`，
            # 或直接用 CLI 子命令（fetch / eval / verify / flush / fix）。
            log.warning(
                "schedule.enabled=false：不启动定时循环（扫描 / flush / 修复都不会自动跑）"
            )
        else:
            tasks.append(asyncio.create_task(self._scan_loop(), name="fissue-scan"))
            if self.settings.queues.manual_flush_enabled:
                tasks.append(asyncio.create_task(self._flush_loop(), name="fissue-flush"))
            if self.settings.auto_fix.enabled:
                tasks.append(asyncio.create_task(self._fix_loop(), name="fissue-fix"))

        web_task = None
        if self.with_web:
            web_task = asyncio.create_task(self._web_loop(), name="fissue-web")
            tasks.append(web_task)

        try:
            await self.stop_event.wait()
        finally:
            log.info("正在停止服务…")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("服务已停止")

    def request_stop(self) -> None:
        self.stop_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, AttributeError, ValueError):
                # Windows 不支持 add_signal_handler → 退回 KeyboardInterrupt
                with contextlib.suppress(ValueError, OSError):  # pragma: no cover
                    signal.signal(sig, lambda *_: self.request_stop())

    # -- 循环 -------------------------------------------------------------

    async def _scan_loop(self) -> None:
        interval = max(60, self.settings.schedule.scan_interval_seconds)
        while not self.stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("扫描轮次异常（继续下一轮）")
            await self._sleep(interval)

    async def _flush_loop(self) -> None:
        interval = max(10, self.settings.schedule.queue_flush_interval_seconds)
        while not self.stop_event.is_set():
            try:
                async with RuntimeContext.open(self.settings, exclusive=True) as ctx:
                    notifier = Notifier(self.settings.notify, self.settings, ctx.repo)
                    await self._flush_queues(ctx, notifier)
            except Exception:
                log.exception("flush 轮次异常（继续）")
            await self._sleep(interval)

    async def _fix_loop(self) -> None:
        """修复循环：比 flush 稍慢，避免与评测争抢沙盒与预算。"""
        interval = max(60, self.settings.schedule.queue_flush_interval_seconds * 2)
        while not self.stop_event.is_set():
            await self._sleep(interval)
            if self.stop_event.is_set():
                return
            try:
                async with RuntimeContext.open(self.settings, exclusive=True) as ctx:
                    notifier = Notifier(self.settings.notify, self.settings, ctx.repo)
                    await self._run_fixes(ctx, notifier)
            except Exception:
                log.exception("修复轮次异常（继续）")

    async def _web_loop(self) -> None:
        """把 Web/API 跑在同一个事件循环里。"""
        try:
            import uvicorn

            from ..web.app import create_app

            config = uvicorn.Config(
                create_app(self.settings),
                host=self.settings.web.host,
                port=self.settings.web.port,
                log_level=self.settings.app.log_level,
            )
            server = uvicorn.Server(config)
            await server.serve()
        except Exception:
            log.exception("Web 服务异常退出")

    async def _sleep(self, seconds: float) -> None:
        """可被停止事件唤醒的 sleep。"""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)

    # -- 子流程 -----------------------------------------------------------

    async def _flush_queues(self, ctx: RuntimeContext, notifier: Notifier) -> dict[str, Any]:
        """按触发条件 flush 全部队列，并派发 tier1/tier2 修复。"""
        qm = QueueManager(ctx)
        processor = FlushProcessor(ctx)
        handler = processor.make_handler()
        info: dict[str, Any] = {}

        for queue in QueueName:
            decision = qm.should_flush(queue)
            if not decision.should_flush:
                info[queue.value] = {"flushed": 0, "reason": decision.reason}
                continue
            outcome = await qm.flush(queue, handler=handler)
            info[queue.value] = {
                "flushed": outcome.flushed,
                "conclusions": len(outcome.conclusions),
                "error": outcome.error,
                "reason": decision.reason,
            }
            if outcome.error:
                log.warning("flush %s 出错：%s", queue.value, outcome.error)
        return info

    async def _run_fixes(self, ctx: RuntimeContext, notifier: Notifier) -> dict[str, Any]:
        """对已定级为 tier1/tier2 的 Issue 执行自动修复。"""
        if not self.settings.auto_fix.enabled:
            return {"skipped": "auto_fix 未启用"}

        from ..fixer.autofix import AutoFixer

        fixer = AutoFixer(ctx)
        report: dict[str, Any] = {"attempted": 0, "succeeded": 0, "failed": 0, "prs": []}

        for cfg in ctx.all_repos():
            if not self._budget_ok(ctx, cfg, notifier):
                continue
            try:
                # 常驻服务同样要先认领上次中断残留的 fixing 条目，否则它们
                # 永远不在候选里（状态机不会自己回到 fix_queued）。
                reclaimed = ctx.repo.reclaim_fixing_items(repo_slug=cfg.slug)
                if reclaimed:
                    log.info("已认领 %d 条残留的修复中条目：%s", len(reclaimed), ", ".join(reclaimed[:3]))
                candidates = ctx.repo.list_items(
                    repo_slug=cfg.slug,
                    item_type=ItemType.ISSUE,
                    status=ItemStatus.FIX_QUEUED,
                    limit=3,
                )
            except Exception:
                log.exception("取候选失败 %s", cfg.slug)
                continue

            for item in candidates:
                if item.priority not in (Priority.TIER1, Priority.TIER2):
                    continue
                try:
                    attempt = await fixer.fix_item(item)
                except BudgetExceeded as exc:
                    log.warning("修复预算熔断：%s", exc)
                    await notifier.notify_budget(repo_key=cfg.slug, used=ctx.usage_report(cfg.slug))
                    break
                except Exception:
                    log.exception("修复失败 %s（继续）", item.key)
                    continue

                report["attempted"] += 1
                if attempt.pr_url:
                    report["succeeded"] += 1
                    report["prs"].append(attempt.pr_url)
                    await notifier.notify_pr_created(item, attempt)
                elif attempt.outcome.value in ("needs_manual", "failed"):
                    report["failed"] += 1
                    await notifier.notify_needs_manual(
                        item, attempt.error or "自动修复未成功", report_path=attempt.report_path
                    )
        return report

    async def _alert_high_importance(
        self, ctx: RuntimeContext, cfg: RepoConfig, results: Sequence[Any], notifier: Notifier
    ) -> None:
        """对高重要性条目推送告警（Q4C 阈值告警）。"""
        threshold = self.settings.evaluation.thresholds.alert_importance
        for r in results:
            if getattr(r, "evaluation", None) is None:
                continue
            if r.evaluation.importance < threshold:
                continue
            item = ctx.repo.get_item(r.key)
            if item is None:
                continue
            try:
                adapter = await ctx.adapter_for_repo(cfg)
                from ..platforms.registry import parse_repo_ref

                url = adapter.item_url(parse_repo_ref(item.repo, item.platform), item.number, item.item_type)
            except Exception:
                url = None
            await notifier.notify_high_importance(item, r.evaluation, url=url)

    # -- 辅助 -------------------------------------------------------------

    async def _verify_pending(self, ctx: RuntimeContext, notifier: Notifier) -> dict[str, Any]:
        """给 ``status=evaluated`` 的条目生成验证器并跑 F2P，返回本步计数。

        口径与 CLI 的 ``fissue verify`` 对齐（那是先跑通的一条路）：只取 ``evaluated``；
        Issue-FEATURE 不验证（没有缺陷可复现，直接置 ``labeled`` 等打标签）；失败重试
        ``verifier.item_retries`` 次，用尽后计一次失败而**不抛**——一条失败不该拖垮整轮。

        另外两道 daemon 特有的门：

        * **基础镜像不可用就跳过整步**。daemon 里没人盯着，缺镜像时每条都要「克隆 → LLM
          生成验证器 → 沙盒报错」白跑一遍（pipekit 实测 15 条全栽在这同一条上），既烧
          token 又白等；CLI 那边是直接报错、让人先去 build。
        * **预算熔断**：验证器生成要调 LLM，与评测共用当日预算。

        串行执行：daemon 与评测 / flush 共享同一套资源（沙盒、预算），先求稳；要并发请用
        CLI 的 ``--concurrency``。
        """
        from ..pipeline.stages import PRVerifyStage, VerifyStage

        out: dict[str, Any] = {"verified": 0, "skipped": 0, "failed": 0}
        if not self.settings.schedule.verify_enabled:
            # 关掉这一步 = 验证交给人工（`fissue verify` / API），常驻服务只做抓取→评测→
            # flush→修复。见 config.ScheduleConfig.verify_enabled。
            out["skipped_reason"] = "schedule.verify_enabled=false"
            return out
        ready, img_reason = ctx.sandbox.image_ready(self.settings.sandbox.image)
        if not ready:
            log.warning("跳过验证：%s（先构建镜像，或以 sandbox.runtime: local 运行）", img_reason)
            out["skipped_reason"] = img_reason
            return out

        stage_issue, stage_pr = VerifyStage(ctx), PRVerifyStage(ctx)
        max_retries = max(0, self.settings.verifier.item_retries)
        for cfg in ctx.all_repos():
            items = ctx.repo.list_items(
                repo_slug=cfg.slug,
                status=ItemStatus.EVALUATED,
                limit=self.settings.schedule.verify_batch_limit,
            )
            if not items:
                continue
            if not self._budget_ok(ctx, cfg, notifier):
                out["skipped"] += len(items)
                continue
            for item in items:
                ev = ctx.repo.latest_evaluation(item.key)
                if ev is None:
                    out["skipped"] += 1
                    continue
                if ev.category is not Category.BUG and item.item_type is ItemType.ISSUE:
                    # Issue-FEATURE 没有缺陷可复现，直接等打标签（与 CLI 同口径）
                    ctx.repo.set_item_status(item.key, ItemStatus.LABELED)
                    out["skipped"] += 1
                    continue
                stage = stage_issue if item.item_type is ItemType.ISSUE else stage_pr
                r = await self._verify_one(stage, item, ev, max_retries=max_retries)
                if r.error:
                    out["failed"] += 1
                    log.warning("验证失败 %s：%s", item.key, r.error)
                else:
                    out["verified"] += 1
                    log.info(
                        "已验证 %s：%s", item.key, r.notes[-1] if r.notes else r.status
                    )
        if out["verified"] or out["failed"] or out["skipped"]:
            log.info("验证一步：%s", out)
        return out

    async def _verify_one(
        self,
        stage: Any,
        item: RawItem,
        evaluation: Any,
        *,
        max_retries: int,
    ) -> Any:
        """跑一条验证；失败退避重试，用尽后返回带 ``error`` 的结果而不是抛异常。"""
        from ..pipeline.stages import StageResult

        delay = 2.0
        last = ""
        for attempt in range(max_retries + 1):
            try:
                return await stage.run(item, evaluation)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                log.warning("验证 %s 第 %d 次失败：%s", item.key, attempt + 1, last)
                if attempt < max_retries:
                    await self._sleep(delay)
                    delay = min(delay * 2, 30.0)
        return StageResult(key=item.key, error=last)

    def _budget_ok(self, ctx: RuntimeContext, cfg: RepoConfig, notifier: Notifier) -> bool:
        """检查该仓库当日预算是否已超。"""
        budget = self.settings.llm.budget
        used = ctx.usage_report(cfg.slug)
        over_tokens = budget.daily_tokens_per_repo and used["total_tokens"] > budget.daily_tokens_per_repo
        over_cost = budget.daily_usd_per_repo and used["cost_usd"] > budget.daily_usd_per_repo
        if over_tokens or over_cost:
            if budget.hard_stop:
                log.warning("仓库 %s 已超预算，跳过本轮", cfg.slug)
                return False
            log.warning("仓库 %s 已超预算（hard_stop=false，继续）", cfg.slug)
        return True

    def _need_full_rescan(self, ctx: RuntimeContext) -> bool:
        """是否需要定期全量兜底（``full_rescan_days``）。"""
        days = self.settings.schedule.full_rescan_days
        if days <= 0:
            return False
        for cfg in ctx.all_repos():
            row = ctx.repo.get_repo(cfg.slug)
            if row is None or row.last_scanned_at is None:
                return True
            last = row.last_scanned_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last > timedelta(days=days):
                log.info("仓库 %s 距上次扫描超过 %d 天，执行全量兜底", cfg.slug, days)
                return True
        return False


async def run_service(settings: Settings, *, once: bool = False, with_web: bool = False) -> None:
    """便捷入口。"""
    service = FissueService(settings, with_web=with_web)
    if once:
        await service.run_once()
    else:
        await service.run_forever()
