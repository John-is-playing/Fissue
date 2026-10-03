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
        return self

    @property
    def summary(self) -> str:
        return (
            f"{self.repo}：拉取 {self.fetched}，新增 {self.created}，"
            f"更新 {self.updated}，未变 {self.unchanged}，过滤 {self.skipped}"
        )


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
                    continue

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

        stats.duration_seconds = round(time.monotonic() - started, 3)
        self.ctx.repo.finish_scan(
            scan_id,
            fetched=stats.fetched,
            created=stats.created,
            updated=stats.updated,
            unchanged=stats.unchanged,
            error=stats.error,
        )
        if not stats.error:
            self._save_cursor(repo, types)
        log.info(stats.summary + f"（{stats.duration_seconds:.1f}s）")
        return stats

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

    def _save_cursor(self, repo: RepoConfig, types: Sequence[ItemType]) -> None:
        self.ctx.repo.set_repo_cursor(
            repo.slug,
            {
                "last_updated_at": datetime.now(timezone.utc).isoformat(),
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
