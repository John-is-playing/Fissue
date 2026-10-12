"""抓取器：从四平台把 Issue/PR 拉进数据库（支持增量）。

增量策略
--------
* 首次抓取（或 ``full=True``）：按 ``repo.since_days`` 拉取，写回游标。
* 之后：用仓库游标里记录的 ``last_updated_at`` 作为 ``since``，只拉有更新的条目。
* 内容指纹（``content_hash``）没变 → 视为 unchanged，不重复烧钱评测。
* 分页交给适配器，抓取器只负责过滤（标签 / 类型 / 状态）与落库。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from ..config import RepoConfig
from ..errors import FissueError, PlatformError
from ..logging_setup import get_logger
from ..models import ItemStatus, ItemType, RawItem
from ..platforms.base import since_default
from .context import RuntimeContext

log = get_logger(__name__)


@dataclass
class FetchStats:
    """一次抓取的统计。"""

    repo: str = ""
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    error: str | None = None
    duration_seconds: float = 0.0
    new_keys: list[str] = field(default_factory=list)
    changed_keys: list[str] = field(default_factory=list)
    #: 本轮数据是否不完整（条目被标残缺 / 早停在 limit / 写入失败）。
    #: 不完整时**不得推进游标**：缺掉的那一段再也不会被增量抓到。
    incomplete: bool = False
    incomplete_reasons: list[str] = field(default_factory=list)

    def merge(self, other: "FetchStats") -> "FetchStats":
        self.fetched += other.fetched
        self.created += other.created
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.skipped += other.skipped
        self.new_keys.extend(other.new_keys)
        self.changed_keys.extend(other.changed_keys)
        self.duration_seconds = round(self.duration_seconds + other.duration_seconds, 3)
        if other.error:
            self.error = other.error
        if other.incomplete:
            self.incomplete = True
            self.incomplete_reasons.extend(other.incomplete_reasons)
        return self

    @property
    def summary(self) -> str:
        text = (
            f"{self.repo}：拉取 {self.fetched}，新增 {self.created}，"
            f"更新 {self.updated}，未变 {self.unchanged}，过滤 {self.skipped}"
        )
        if self.incomplete:
            detail = "；".join(self.incomplete_reasons[:2]) or "原因未知"
            text += f"（数据不完整，游标未推进：{detail}）"
        return text


class Fetcher:
    """把平台数据同步到本地库。"""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings

    # -- 单仓库 -----------------------------------------------------------

    async def fetch_repo(
        self,
        repo: RepoConfig,
        *,
        full: bool = False,
        limit: int | None = None,
        item_types: Sequence[ItemType] | None = None,
    ) -> FetchStats:
        import time

        started = time.monotonic()
        stats = FetchStats(repo=repo.slug)
        adapter = await self.ctx.adapter_for_repo(repo)
        ref = self.ctx.repo_ref(repo)
        scan_id = self.ctx.repo.start_scan(repo.key)

        types = list(item_types) if item_types else _collect_types(repo)
        if not types:
            self.ctx.repo.finish_scan(scan_id, error="未配置要抓取的类型（collect）")
            return stats

        since = self._compute_since(repo, full=full)
        repo_id = self.ctx.ensure_repo_row(repo)
        #: 本轮**实际入库**条目的最大 updated_at，作为新的游标水位。
        watermark: datetime | None = None

        try:
            async for item in adapter.fetch_items(ref, item_types=types, since=since, limit=limit):
                if not self._accept(item, repo):
                    stats.skipped += 1
                    continue
                stats.fetched += 1
                try:
                    _item_id, created, changed = self.ctx.repo.upsert_item(repo_id, item)
                except Exception as exc:
                    log.warning("写入条目失败 %s：%s", item.key, exc)
                    stats.skipped += 1
                    self._mark_incomplete(stats, f"write_failed:{item.key}")
                    continue

                # 适配器把「评论/文件清单没拉全」记在条目上；带着残缺的轮次不能
                # 推进游标，否则那段缺口就永久留在库里了。
                if item.degraded:
                    self._mark_incomplete(
                        stats, ";".join(f"{item.key}:{r}" for r in item.degraded)
                    )

                if item.updated_at is not None:
                    watermark = (
                        item.updated_at if watermark is None else max(watermark, item.updated_at)
                    )

                if created:
                    stats.created += 1
                    stats.new_keys.append(item.key)
                elif changed:
                    stats.updated += 1
                    stats.changed_keys.append(item.key)
                else:
                    stats.unchanged += 1
        except PlatformError as exc:
            stats.error = str(exc)
            log.error("抓取 %s 失败：%s", repo.slug, exc)
        except Exception as exc:  # 兜底：不让单个仓库拖垮整轮
            stats.error = f"{type(exc).__name__}: {exc}"
            log.exception("抓取 %s 出现未预期错误", repo.slug)

        # 早停在 --limit 上意味着后面还有没抓到的旧条目，不能把游标推过去。
        if limit is not None and stats.fetched >= limit:
            self._mark_incomplete(stats, f"early_stop:limit={limit}")

        stats.duration_seconds = round(time.monotonic() - started, 3)
        self.ctx.repo.finish_scan(
            scan_id,
            fetched=stats.fetched,
            created=stats.created,
            updated=stats.updated,
            unchanged=stats.unchanged,
            error=stats.error,
        )
        self._save_cursor(repo, types, stats=stats, watermark=watermark)
        log.info(stats.summary + f"（{stats.duration_seconds:.1f}s）")
        return stats

    @staticmethod
    def _mark_incomplete(stats: FetchStats, reason: str) -> None:
        """记录一处「本轮数据不完整」的原因（去重，便于日志与断言）。"""
        stats.incomplete = True
        if reason not in stats.incomplete_reasons:
            stats.incomplete_reasons.append(reason)

    # -- 多仓库 -----------------------------------------------------------

    async def fetch_many(
        self,
        repos: Sequence[RepoConfig],
        *,
        full: bool = False,
        limit: int | None = None,
        concurrency: int = 2,
    ) -> FetchStats:
        """并发抓取多个仓库（默认串行度低，避免触发平台限流）。"""
        if not repos:
            return FetchStats()
        sem = asyncio.Semaphore(max(1, concurrency))
        total = FetchStats(repo=f"{len(repos)} 个仓库")

        async def one(r: RepoConfig) -> FetchStats:
            async with sem:
                return await self.fetch_repo(r, full=full, limit=limit)

        results = await asyncio.gather(*(one(r) for r in repos), return_exceptions=True)
        for r, res in zip(repos, results):
            if isinstance(res, Exception):
                total.error = f"{r.slug}: {res}"
                log.error("抓取 %s 异常：%s", r.slug, res)
                continue
            total.merge(res)
        return total

    # -- 内部 -------------------------------------------------------------

    def _compute_since(self, repo: RepoConfig, *, full: bool) -> datetime | None:
        """决定 ``since`` 参数。"""
        if full or not self.settings.schedule.incremental:
            return since_default(repo.since_days)

        cursor = self.ctx.repo.get_repo_cursor(repo.slug)
        last = cursor.get("last_updated_at")
        if last:
            try:
                dt = datetime.fromisoformat(str(last))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                # 回退一小段时间，避免边界上漏条目
                return dt - timedelta(minutes=5)
            except ValueError:
                pass

        # 没有游标 → 首次抓取，按 since_days
        return since_default(repo.since_days)

    def _save_cursor(
        self,
        repo: RepoConfig,
        types: Sequence[ItemType],
        *,
        stats: FetchStats,
        watermark: datetime | None,
    ) -> None:
        """推进增量游标。

        三道闸门，任一不满足就**原地不动**：

        1. 本轮无错误——出错说明有条目根本没抓到；
        2. 本轮数据完整——条目被标残缺、或早停在 ``--limit`` 上，都说明后面还有；
        3. 本轮确实入库了条目——一条都没入库时若把游标推到 ``now()``，会把
           这中间发生的所有更新一次性跳过。

        写回的是**本轮实际入库条目的最大 ``updated_at``**，而不是 wall-clock
        ``now()``：后者等于宣告「这段时间我都抓过了」，而实际上没抓到的那几条
        再也不会被增量抓取碰到（除非 ``--full``）。
        """
        if stats.error:
            log.warning("本轮抓取有错误（%s），不推进游标：%s", stats.error, repo.slug)
            return
        if stats.incomplete:
            log.warning(
                "本轮数据不完整（%s），不推进游标 %s：下一轮会重抓这一段",
                "；".join(stats.incomplete_reasons[:3]) or "原因未知",
                repo.slug,
            )
            return
        if watermark is None:
            log.info("本轮未入库任何条目，游标保持不变：%s", repo.slug)
            return
        self.ctx.repo.set_repo_cursor(
            repo.slug,
            {
                "last_updated_at": watermark.isoformat(),
                "item_types": [t.value for t in types],
            },
        )

    @staticmethod
    def _accept(item: RawItem, repo: RepoConfig) -> bool:
        """按仓库配置过滤条目。"""
        labels = {l.lower() for l in item.labels}
        if repo.labels_include:
            wanted = {l.lower() for l in repo.labels_include}
            if not (labels & wanted):
                return False
        if repo.labels_exclude:
            unwanted = {l.lower() for l in repo.labels_exclude}
            if labels & unwanted:
                return False
        if repo.assignee and item.author.lower() != repo.assignee.lower():
            # assignee 语义是「指派给谁」，这里用作者做近似过滤（多数平台未暴露 assignee 列表）
            pass
        return True


def _collect_types(repo: RepoConfig) -> list[ItemType]:
    out: list[ItemType] = []
    for raw in repo.collect:
        text = str(raw).strip().lower()
        if text in ("issue", "issues"):
            out.append(ItemType.ISSUE)
        elif text in ("pr", "prs", "pull", "pulls", "mr", "merge_request"):
            out.append(ItemType.PR)
    return out


async def fetch_items_only(ctx: RuntimeContext, repos: Sequence[RepoConfig], **kwargs) -> FetchStats:
    """便捷函数。"""
    return await Fetcher(ctx).fetch_many(repos, **kwargs)
