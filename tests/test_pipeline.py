"""流水线、队列与批量 flush 测试（打桩 LLM，不触网）。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from fissue.models import (
    Category,
    DimensionScore,
    Evaluation,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    QueueName,
    RawItem,
    RepoRef,
    Scores,
    VerifierKind,
    VerifierOutcome,
    VerifierResult,
    VerifierRun,
    VerifierSpec,
)
from fissue.pipeline.context import RuntimeContext
from fissue.pipeline.fetcher import Fetcher, FetchStats, _collect_types
from fissue.pipeline.flush import FlushProcessor
from fissue.pipeline.queue import QueueManager
from fissue.pipeline.stages import VerifyStage

GH = "https://api.github.com"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx(settings, repo) -> RuntimeContext:
    """复用测试用 db/repo 的 Context（不新建连接池）。"""
    from fissue.ai.client import LLMClient
    from fissue.ai.evaluator import Evaluator
    from fissue.sandbox.forwarder import SandboxManager
    from fissue.verifier.generator import VerifierGenerator
    from fissue.verifier.regression import RegressionGate
    from fissue.verifier.runner import VerifierRunner

    c = RuntimeContext.__new__(RuntimeContext)
    object.__setattr__(c, "settings", settings)
    object.__setattr__(c, "db", repo.db)
    object.__setattr__(c, "repo", repo)
    object.__setattr__(c, "_adapters", {})
    object.__setattr__(c, "_lock", asyncio.Lock())
    llm = LLMClient(settings.llm)
    object.__setattr__(c, "llm", llm)
    object.__setattr__(c, "sandbox", SandboxManager(settings.sandbox, force_inline=True))
    object.__setattr__(c, "evaluator", Evaluator(llm, repo, settings))
    object.__setattr__(c, "generator", VerifierGenerator(llm, settings))
    object.__setattr__(c, "verifier", VerifierRunner(c.sandbox, repo, settings, client=llm))
    object.__setattr__(c, "regression_gate", RegressionGate(c.sandbox, repo, settings))
    return c


def _seed_issue(repo, number: int, *, category: Category = Category.BUG, priority: Priority = Priority.NONE) -> RawItem:
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number, item_type=ItemType.ISSUE,
        title=f"崩溃 {number}", body="boom", labels=["bug"],
    )
    repo.upsert_item(rid, item)
    repo.save_evaluation(
        item.key,
        Evaluation(
            category=category, priority=priority,
            scores=Scores(
                authenticity=DimensionScore(score=90),
                importance=DimensionScore(score=85),
                difficulty=DimensionScore(score=20),
            ),
            model="stub",
        ),
    )
    return item


# ---------------------------------------------------------------------------
# 抓取器
# ---------------------------------------------------------------------------


def test_collect_types_parsing() -> None:
    from fissue.config import RepoConfig

    assert _collect_types(RepoConfig(platform=Platform.GITHUB, owner="a", name="b")) == [ItemType.ISSUE, ItemType.PR]
    assert _collect_types(RepoConfig(platform=Platform.GITHUB, owner="a", name="b", collect=["pr"])) == [ItemType.PR]
    assert _collect_types(RepoConfig(platform=Platform.GITHUB, owner="a", name="b", collect=["junk"])) == []


def test_fetch_stats_merge() -> None:
    a = FetchStats(repo="a", fetched=3, created=1, new_keys=["k1"])
    b = FetchStats(repo="b", fetched=2, updated=2, changed_keys=["k2"], error="boom")
    a.merge(b)
    assert a.fetched == 5 and a.created == 1 and a.updated == 2
    assert a.new_keys == ["k1"] and a.changed_keys == ["k2"]
    assert a.error == "boom"


def test_fetcher_rejects_filtered_labels(settings) -> None:
    from fissue.config import RepoConfig

    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b", labels_exclude=["wontfix"])
    item = RawItem(platform=Platform.GITHUB, repo="a/b", number=1, item_type=ItemType.ISSUE,
                   title="t", labels=["wontfix"])
    assert Fetcher._accept(item, cfg) is False

    cfg2 = RepoConfig(platform=Platform.GITHUB, owner="a", name="b", labels_include=["bug"])
    assert Fetcher._accept(item, cfg2) is False
    item.labels = ["bug"]
    assert Fetcher._accept(item, cfg2) is True


def test_fetcher_compute_since_full(settings, ctx) -> None:
    from fissue.config import RepoConfig

    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b", since_days=30)
    fetcher = Fetcher(ctx)
    assert fetcher._compute_since(cfg, full=True) is not None
    cfg2 = RepoConfig(platform=Platform.GITHUB, owner="a", name="b", since_days=0)
    assert fetcher._compute_since(cfg2, full=True) is None


def test_fetcher_cursor_fallback(settings, ctx) -> None:
    from fissue.config import RepoConfig

    cfg = RepoConfig(platform=Platform.GITHUB, owner="a", name="b", since_days=7)
    fetcher = Fetcher(ctx)
    assert fetcher._compute_since(cfg, full=False) is not None   # 无游标 → 按 since_days


# ---------------------------------------------------------------------------
# P0：增量游标口径
#
# 此前游标写的是 wall-clock now()：等于宣告「这段时间我都抓过了」，而实际上
# 没抓到的条目再也不会被增量碰到。现在改为「本轮实际入库条目的最大 updated_at」，
# 并在数据不完整 / 一条都没入库时**原地不动**。
# ---------------------------------------------------------------------------


class _StubFetchAdapter:
    """只按脚本产出条目，用于验证游标口径。"""

    def __init__(self, items: list[RawItem], *, error: Exception | None = None) -> None:
        self.items = items
        self.error = error
        # 与真实适配器同名：fetcher 用 getattr 读它判「本轮数据是否完整」
        self.pagination_truncated = 0

    async def fetch_items(self, ref, *, item_types=None, since=None, limit=None):  # noqa: ANN001, ANN201
        if self.error is not None:
            raise self.error
        for idx, item in enumerate(self.items):
            if limit is not None and idx >= limit:
                return
            yield item


def _wire_adapter(ctx, monkeypatch, adapter) -> Fetcher:  # noqa: ANN001
    async def _fake(repo_cfg):  # noqa: ANN001
        return adapter

    monkeypatch.setattr(ctx, "adapter_for_repo", _fake)
    return Fetcher(ctx)


def _fetched_item(number: int, updated: str) -> RawItem:
    from datetime import datetime

    return RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number, item_type=ItemType.ISSUE,
        title=f"t{number}", body="b", updated_at=datetime.fromisoformat(updated),
    )


def _repo_cfg():  # noqa: ANN202
    from fissue.config import RepoConfig

    return RepoConfig(platform=Platform.GITHUB, owner="psf", name="requests", since_days=30)


def _seed_cursor(ctx, cfg, value: str) -> None:  # noqa: ANN001
    """预置游标：必须先建仓库行——``set_repo_cursor`` 在行不存在时会静默 no-op。"""
    ctx.ensure_repo_row(cfg)
    ctx.repo.set_repo_cursor(cfg.slug, {"last_updated_at": value})


async def test_cursor_uses_max_updated_at_not_wall_clock(settings, ctx, monkeypatch) -> None:
    """游标写的是本轮入库条目的最大 updated_at，而不是 now()。"""
    cfg = _repo_cfg()
    adapter = _StubFetchAdapter([
        _fetched_item(1, "2026-01-02T00:00:00+00:00"),
        _fetched_item(2, "2026-01-05T00:00:00+00:00"),
    ])
    fetcher = _wire_adapter(ctx, monkeypatch, adapter)

    stats = await fetcher.fetch_repo(cfg)

    assert stats.created == 2
    assert stats.incomplete is False
    assert ctx.repo.get_repo_cursor(cfg.slug)["last_updated_at"].startswith("2026-01-05")


async def test_cursor_frozen_when_data_incomplete(settings, ctx, monkeypatch) -> None:
    """条目被标残缺时游标必须原地不动，否则那段缺口永久留在库里。"""
    cfg = _repo_cfg()
    _seed_cursor(ctx, cfg, "2026-01-01T00:00:00+00:00")
    item = _fetched_item(1, "2026-01-09T00:00:00+00:00")
    item.mark_degraded("comments_unavailable#1")
    fetcher = _wire_adapter(ctx, monkeypatch, _StubFetchAdapter([item]))

    stats = await fetcher.fetch_repo(cfg)

    assert stats.incomplete is True
    assert stats.incomplete_reasons, "应当留下不完整的原因"
    assert ctx.repo.get_repo_cursor(cfg.slug)["last_updated_at"].startswith("2026-01-01")


async def test_cursor_frozen_when_nothing_stored(settings, ctx, monkeypatch) -> None:
    """一条都没入库时不得把游标推到 now()（那会跳过中间所有更新）。"""
    cfg = _repo_cfg()
    _seed_cursor(ctx, cfg, "2026-01-01T00:00:00+00:00")
    fetcher = _wire_adapter(ctx, monkeypatch, _StubFetchAdapter([]))

    stats = await fetcher.fetch_repo(cfg)

    assert stats.fetched == 0
    assert ctx.repo.get_repo_cursor(cfg.slug)["last_updated_at"].startswith("2026-01-01")


async def test_cursor_frozen_on_early_stop(settings, ctx, monkeypatch) -> None:
    """早停在 --limit 上说明后面还有没抓到的旧条目，不能推进游标。"""
    cfg = _repo_cfg()
    _seed_cursor(ctx, cfg, "2026-01-01T00:00:00+00:00")
    fetcher = _wire_adapter(ctx, monkeypatch, _StubFetchAdapter([
        _fetched_item(1, "2026-01-09T00:00:00+00:00"),
        _fetched_item(2, "2026-01-08T00:00:00+00:00"),
    ]))

    stats = await fetcher.fetch_repo(cfg, limit=1)

    assert stats.fetched == 1
    assert stats.incomplete is True
    assert "early_stop" in "；".join(stats.incomplete_reasons)
    assert ctx.repo.get_repo_cursor(cfg.slug)["last_updated_at"].startswith("2026-01-01")


async def test_cursor_frozen_when_pagination_was_truncated(settings, ctx, monkeypatch) -> None:
    """分页到上限被截断时**不能推进游标**——没抓到的那段再也不会被增量碰到。

    触发场景：`_paginate` 到 `max_pages` 就停（page 风格原先静默返回；link 风格原先连这个
    上限都不生效）。截断本身不可怕，可怕的是它被当成「到底了」——那批（`update` 倒序里更旧的）
    就永久落在增量窗口外，日志里也看不出来。
    """
    cfg = _repo_cfg()
    _seed_cursor(ctx, cfg, "2026-01-01T00:00:00+00:00")
    adapter = _StubFetchAdapter([_fetched_item(1, "2026-01-09T00:00:00+00:00")])
    adapter.pagination_truncated = 1                     # 模拟适配器报上来的截断
    fetcher = _wire_adapter(ctx, monkeypatch, adapter)

    stats = await fetcher.fetch_repo(cfg)

    assert stats.incomplete is True
    assert "pagination_truncated" in "；".join(stats.incomplete_reasons)
    assert ctx.repo.get_repo_cursor(cfg.slug)["last_updated_at"].startswith("2026-01-01")


async def test_early_stop_uses_pre_filter_count(settings, ctx, monkeypatch) -> None:
    """早停要用「适配器交出来的条数」判，不能用过滤后的 ``fetched``。

    回归：适配器按 **yield 条数**早停（github/v5/gitlab 三处都是 ``count >= limit``），
    而原先的判定是 ``stats.fetched >= limit``，而 ``stats.fetched`` 只在 ``_accept``
    通过后自增。配了 ``labels_include`` / ``labels_exclude``（``config.example.yaml``
    默认就带 ``labels_exclude``）时，被滤掉的条目不计入 fetched → 早停不被识别 →
    游标照推 → ``limit`` 之后那些从未抓到的旧条目永久落在增量窗口外，静默不再入库。
    """
    cfg = _repo_cfg()
    cfg.labels_exclude = ["duplicate"]
    _seed_cursor(ctx, cfg, "2026-01-01T00:00:00+00:00")
    items = [
        _fetched_item(1, "2026-01-09T00:00:00+00:00"),
        _fetched_item(2, "2026-01-08T00:00:00+00:00"),
    ]
    items[1].labels = ["duplicate"]
    fetcher = _wire_adapter(ctx, monkeypatch, _StubFetchAdapter(items))

    stats = await fetcher.fetch_repo(cfg, limit=2)

    assert stats.fetched == 1, "被标签过滤掉的那条不该计入 fetched"
    assert stats.incomplete is True, "适配器已在 limit 上停住，必须是「数据不完整」"
    assert "early_stop" in "；".join(stats.incomplete_reasons)
    assert ctx.repo.get_repo_cursor(cfg.slug)["last_updated_at"].startswith("2026-01-01")


async def test_cursor_frozen_on_rate_limit(settings, ctx, monkeypatch) -> None:
    """限流不再被吞：本轮记为失败，游标不推进（下一轮会重抓这一段）。"""
    from fissue.errors import RateLimitError

    cfg = _repo_cfg()
    _seed_cursor(ctx, cfg, "2026-01-01T00:00:00+00:00")
    fetcher = _wire_adapter(
        ctx, monkeypatch, _StubFetchAdapter([], error=RateLimitError("触发限流"))
    )

    stats = await fetcher.fetch_repo(cfg)

    assert stats.error, "限流必须体现为本轮错误"
    assert ctx.repo.get_repo_cursor(cfg.slug)["last_updated_at"].startswith("2026-01-01")


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------


def test_should_flush_threshold(ctx, repo, settings) -> None:
    """达到数量阈值 → flush。"""
    settings.queues.verify_queue.flush_size = 3
    settings.queues.verify_queue.idle_flush_seconds = 99999
    qm = QueueManager(ctx)
    assert qm.should_flush(QueueName.VERIFY).should_flush is False

    for n in (1, 2, 3):
        item = _seed_issue(repo, n)
        eid = qm.enqueue(QueueName.VERIFY, item.key)
        repo.finish_queue_entry(eid, status="done", result={"output": "FAIL"})
    decision = qm.should_flush(QueueName.VERIFY)
    assert decision.should_flush is True
    assert "数量阈值" in decision.reason
    assert decision.done_count == 3


def test_should_flush_force_while_still_pending(ctx, repo) -> None:
    """还有未处理条目时不该自动 flush，但手动 force 可以。"""
    qm = QueueManager(ctx)
    done_item = _seed_issue(repo, 1)
    eid = qm.enqueue(QueueName.VERIFY, done_item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    pending_item = _seed_issue(repo, 2)
    qm.enqueue(QueueName.VERIFY, pending_item.key)          # 仍在 pending

    assert qm.should_flush(QueueName.VERIFY).should_flush is False
    forced = qm.should_flush(QueueName.VERIFY, force=True)
    assert forced.should_flush is True and "手动触发" in forced.reason


def test_should_flush_when_queue_drains(ctx, repo) -> None:
    """Q9 的兜底：队列已无待处理条目（哪怕只有 1 条）也应 flush。"""
    qm = QueueManager(ctx)
    item = _seed_issue(repo, 1)
    eid = qm.enqueue(QueueName.VERIFY, item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    decision = qm.should_flush(QueueName.VERIFY)
    assert decision.should_flush is True
    assert "无待处理" in decision.reason


def test_should_flush_idle_timeout(ctx, repo, settings) -> None:
    """有 pending 卡住时，靠空闲超时兜底 flush 已完成的批次。"""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from fissue.store.tables import QueueEntryRow

    settings.queues.verify_queue.flush_size = 100        # 阈值远不可达
    settings.queues.verify_queue.idle_flush_seconds = 300
    qm = QueueManager(ctx)

    done_item = _seed_issue(repo, 1)
    eid = qm.enqueue(QueueName.VERIFY, done_item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    # 制造一个 pending，避免走「队列已无待处理」那条分支
    pending_item = _seed_issue(repo, 2)
    qm.enqueue(QueueName.VERIFY, pending_item.key)

    # 把完成时间回拨到 10 分钟前，模拟「静默超时」
    with repo.db.session() as s:
        s.execute(
            update(QueueEntryRow)
            .where(QueueEntryRow.id == eid)
            .values(finished_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        )

    decision = qm.should_flush(QueueName.VERIFY)
    assert decision.should_flush is True
    assert "空闲" in decision.reason
    assert decision.idle_seconds and decision.idle_seconds > 300


async def test_flush_persists_conclusions_and_marks(ctx, repo, stub_llm) -> None:
    qm = QueueManager(ctx)
    items = [_seed_issue(repo, n) for n in (1, 2)]
    for item in items:
        eid = qm.enqueue(QueueName.VERIFY, item.key)
        repo.finish_queue_entry(eid, status="done", result={"conclusion": "复现成功", "output": "FAIL"})

    async def handler(queue_kind, entries):
        return [
            {"key": e["key"], "verdict": "fix", "labels": ["reproduced"],
             "priority": Priority.TIER1, "reason": "低难度高重要性", "confidence": 0.9,
             "model": "stub"}
            for e in entries
        ]

    outcome = await qm.flush(QueueName.VERIFY, handler=handler, force=True)
    assert outcome.ok and outcome.flushed == 2
    assert len(outcome.conclusions) == 2
    assert repo.queue_unflushed(QueueName.VERIFY) == []

    conclusion = repo.latest_batch_conclusion(items[0].key)
    assert conclusion.verdict == "fix"
    assert conclusion.priority is Priority.TIER1


async def test_flush_handler_error_does_not_lose_entries(ctx, repo) -> None:
    qm = QueueManager(ctx)
    item = _seed_issue(repo, 1)
    eid = qm.enqueue(QueueName.VERIFY, item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    async def boom(queue_kind, entries):
        raise RuntimeError("LLM 挂了")

    outcome = await qm.flush(QueueName.VERIFY, handler=boom, force=True)
    assert outcome.ok is False
    assert "LLM 挂了" in (outcome.error or "")
    # 条目仍留在未 flush 状态，等下一轮重试
    assert len(repo.queue_unflushed(QueueName.VERIFY)) == 1


async def test_flush_missing_conclusion_is_not_marked_flushed(ctx, repo) -> None:
    """模型漏返回一条结论 → 那条**不能**被标记已 flush（否则结论静默丢失、永不重试）。

    回归：`QueueManager.flush` 原先无条件 `mark_flushed(全部 entries)`，而
    `evaluator.conclude_batch` 遇到返回结构异常时只记一条 warning 就 `return []`
    （本地推理模型被 `finish_reason=length` 截断时正是这个形状）。那些条目照样被推进
    `flushed_at`，而 `queue_unflushed` 只取 `flushed_at IS NULL`：它们再也不会进 flush，
    状态停在 queued、拿不到结论、打不上标签、也进不了修复队列，整条链路却报成功。
    """
    qm = QueueManager(ctx)
    items = [_seed_issue(repo, n) for n in (1, 2)]
    for item in items:
        eid = qm.enqueue(QueueName.VERIFY, item.key)
        repo.finish_queue_entry(eid, status="done", result={})

    answered: list[str] = []

    async def handler(queue_kind, entries):
        # 只给第一条结论：模拟模型漏项 / 整批被截断
        answered.append(entries[0]["key"])
        return [
            {"key": entries[0]["key"], "verdict": "fix", "labels": [],
             "priority": Priority.TIER1, "reason": "r", "confidence": 0.9, "model": "stub"}
        ]

    outcome = await qm.flush(QueueName.VERIFY, handler=handler, force=True)

    assert outcome.flushed == 1, "只有真拿到结论的那条算已消费"
    assert outcome.error and "1 条没有拿到结论" in (outcome.error or ""), "缺结论必须响亮报出来"
    left = {e.item_key for e in repo.queue_unflushed(QueueName.VERIFY)}
    assert left == {i.key for i in items} - set(answered), "缺结论的那条要留在队里等下一轮"
    assert repo.latest_batch_conclusion(answered[0]) is not None, "拿到结论的要正常落库"


async def test_flush_gives_up_on_a_never_concluded_entry(ctx, repo) -> None:
    """连续 MAX_FLUSH_MISSES 轮都缺结论 → 落成显式 failed，不再无限重试。

    缺结论留在队里能自愈（偶发漏项），但「对模型来说恒不可定论」的条目若无上限重试，
    每轮 flush 都是白烧一次 LLM；到顶后响亮地失败（报告/看板可见，可人工 --key 重跑）。
    """
    from fissue.pipeline.queue import MAX_FLUSH_MISSES

    qm = QueueManager(ctx)
    item = _seed_issue(repo, 1)
    eid = qm.enqueue(QueueName.VERIFY, item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    async def empty(queue_kind, entries):
        return []

    for _ in range(MAX_FLUSH_MISSES - 1):
        outcome = await qm.flush(QueueName.VERIFY, handler=empty, force=True)
        assert outcome.flushed == 0 and outcome.error
        assert repo.queue_unflushed(QueueName.VERIFY) != [], "还没到上限，应留待下轮"

    outcome = await qm.flush(QueueName.VERIFY, handler=empty, force=True)
    assert "标记 failed" in (outcome.error or "")
    assert repo.queue_unflushed(QueueName.VERIFY) == [], "到顶后不再重试"


async def test_flush_claims_the_batch_so_a_second_writer_gets_nothing(ctx, repo) -> None:
    """一批条目被 flush 抢走后，另一个写者只能拿到空批。

    回归：「挑（``flushed_at IS NULL``）→ 调 LLM → 标记已 flush」原先没有任何互斥，
    于是 `fissue serve --with-web` 里 daemon 的 flush 循环与同进程 uvicorn 的 `/queues/flush`
    会挑到**同一批**：同一批条目被送两次 LLM（重复计费）、平台标签被打两遍。
    这里在 handler 内部（也就是「已交出、结论还没落库」的窗口）再抢一次来复现它。
    """
    qm = QueueManager(ctx)
    item = _seed_issue(repo, 1)
    eid = qm.enqueue(QueueName.VERIFY, item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    stolen: list[int] = []

    async def handler(queue_kind, entries):
        stolen.append(len(qm.repo.claim_unflushed(QueueName.VERIFY)))
        return [
            {"key": e["key"], "verdict": "skip", "labels": [], "priority": Priority.NONE,
             "reason": "r", "confidence": 0.5, "model": "stub"}
            for e in entries
        ]

    outcome = await qm.flush(QueueName.VERIFY, handler=handler, force=True)

    assert outcome.flushed == 1
    assert stolen == [0], "同批条目不该被第二个写者抢走"
    assert repo.queue_unflushed(QueueName.VERIFY) == [], "结论已落，批次已消费"


async def test_flush_noop_when_nothing_done(ctx) -> None:
    qm = QueueManager(ctx)

    async def handler(queue_kind, entries):
        return []

    outcome = await qm.flush(QueueName.VERIFY, handler=handler)
    assert outcome.flushed == 0


# ---------------------------------------------------------------------------
# flush 处理器
# ---------------------------------------------------------------------------


async def test_flush_processor_labels_and_dispatches(ctx, repo, monkeypatch) -> None:
    """结论 fix + tier1 → 状态置为 fix_queued 并进入待修列表。"""
    processor = FlushProcessor(ctx)

    async def fake_label(payload, labels):
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", fake_label)

    item = _seed_issue(repo, 5)
    entries = [{"key": item.key, "repo": item.repo, "number": item.number, "item_type": "issue", "title": item.title}]
    conclusions = [{"key": item.key, "verdict": "fix", "labels": ["bug"], "priority": Priority.TIER1,
                    "reason": "r", "confidence": 0.9}]
    report = await processor.apply(QueueName.VERIFY.value, entries, conclusions)

    assert report.labeled == [item.key]
    assert report.fix_candidates == [(item.key, Priority.TIER1)]
    assert not report.label_errors
    row = repo.get_item_row(item.key)
    assert row.status == ItemStatus.FIX_QUEUED.value
    assert row.priority == Priority.TIER1.value


async def test_flush_processor_skip_and_needs_review(ctx, repo, monkeypatch) -> None:
    processor = FlushProcessor(ctx)

    async def fake_label(payload, labels):
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", fake_label)
    a = _seed_issue(repo, 6)
    b = _seed_issue(repo, 7)
    entries = [
        {"key": a.key, "repo": a.repo, "number": a.number, "item_type": "issue"},
        {"key": b.key, "repo": b.repo, "number": b.number, "item_type": "issue"},
    ]
    conclusions = [
        {"key": a.key, "verdict": "skip", "labels": [], "priority": Priority.NONE, "reason": "", "confidence": 0.5},
        {"key": b.key, "verdict": "needs_review", "labels": [], "priority": Priority.NONE, "reason": "", "confidence": 0.5},
    ]
    report = await processor.apply(QueueName.VERIFY.value, entries, conclusions)
    assert report.fix_candidates == []
    assert repo.get_item_row(a.key).status == ItemStatus.SKIPPED.value
    assert repo.get_item_row(b.key).status == ItemStatus.NEEDS_MANUAL.value


async def test_flush_processor_pr_never_auto_merges(ctx, repo, monkeypatch) -> None:
    """PR 的 merge 结论只打标签，状态为 labeled（绝不自动合并）。"""
    processor = FlushProcessor(ctx)

    async def fake_label(payload, labels):
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", fake_label)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    pr = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=99, item_type=ItemType.PR,
                 title="PR", body="d")
    repo.upsert_item(rid, pr)

    entries = [{"key": pr.key, "repo": pr.repo, "number": 99, "item_type": "pr"}]
    conclusions = [{"key": pr.key, "verdict": "merge", "labels": ["ai-verified"],
                    "priority": Priority.NONE, "reason": "功能验证通过", "confidence": 0.85}]
    report = await processor.apply(QueueName.FIX_BUG.value, entries, conclusions)
    assert report.verdicts[pr.key] == "merge"
    assert repo.get_item_row(pr.key).status == ItemStatus.LABELED.value
    assert not report.fix_candidates


async def test_flush_conclusion_priority_follows_rules_for_pr(ctx, repo, monkeypatch) -> None:
    """落库的优先级必须与规则一致：only_issues 下 PR 恒为 none。

    回归（ratekit 实测）：PR#11 被 LLM 批量结论写成 tier1 并原样落库，导致
    items.priority=tier1 与 compute_priority 的「PR 一律 none」自相矛盾，
    污染报告 / 看板 / API。这里走**生产路径**（make_handler → apply → 落库）
    复现并锁定修复。
    """
    processor = FlushProcessor(ctx)

    async def fake_label(payload, labels):
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", fake_label)

    async def fake_conclude(*, queue_kind, entries):
        return [
            {"key": e["key"], "verdict": "merge", "labels": ["ai-verified"],
             "priority": Priority.TIER1,           # LLM 误给的优先级
             "reason": "验证通过", "confidence": 0.9, "model": "stub"}
            for e in entries
        ]

    monkeypatch.setattr(ctx.evaluator, "conclude_batch", fake_conclude)

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    pr = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=99, item_type=ItemType.PR,
                 title="fix: 修好了一处缺陷", body="d")
    repo.upsert_item(rid, pr)
    repo.save_evaluation(pr.key, Evaluation(
        category=Category.BUG, priority=Priority.NONE, model="stub",
        scores=Scores(
            authenticity=DimensionScore(score=95),
            importance=DimensionScore(score=85),
            difficulty=DimensionScore(score=10),
        ),
    ))
    eid = processor.queues.enqueue(QueueName.FIX_BUG, pr.key)
    repo.finish_queue_entry(eid, status="done", result={})

    outcome = await processor.queues.flush(
        QueueName.FIX_BUG, handler=processor.make_handler(), force=True
    )
    assert outcome.ok and outcome.flushed == 1
    # 无论模型怎么说，PR 的优先级都必须是 none
    assert repo.latest_batch_conclusion(pr.key).priority is Priority.NONE
    assert repo.get_item_row(pr.key).priority == Priority.NONE.value


async def test_flush_processor_label_failure_recorded(ctx, repo, monkeypatch) -> None:
    """打标签失败时：要记录错误，但流水线必须继续推进状态。

    这里**显式**让打标签抛错，而不是依赖"本地网络恰好不通"——
    否则在能连上 GitHub 的机器上，这个测试会真的发请求（更慢，且断言变得
    取决于环境）。确定性失败才是这个用例要验的东西。
    """
    processor = FlushProcessor(ctx)

    async def _boom(repo_cfg):
        raise RuntimeError("模拟打标签失败（无写权限）")

    monkeypatch.setattr(ctx, "adapter_for_repo", _boom)

    item = _seed_issue(repo, 8)
    entries = [{"key": item.key, "repo": item.repo, "number": item.number, "item_type": "issue"}]
    conclusions = [{"key": item.key, "verdict": "fix", "labels": ["bug"],
                    "priority": Priority.TIER2, "reason": "r", "confidence": 0.7}]
    report = await processor.apply(QueueName.VERIFY.value, entries, conclusions)
    # 打标签失败 → 记录错误，但状态仍应推进到待修复
    assert report.label_errors
    assert "模拟打标签失败" in report.label_errors[0]
    assert repo.get_item_row(item.key).status == ItemStatus.FIX_QUEUED.value
    # 派发优先级由规则决定（该 seed 难度 20 / 重要性 85 → tier1），模型说的 tier2 不作数
    assert report.fix_candidates == [(item.key, Priority.TIER1)]


async def test_flush_dispatch_priority_follows_rules_not_llm(ctx, repo, monkeypatch) -> None:
    """派发优先级以 compute_priority 的规则结论为准，LLM 的 priority 仅作参考。

    否则模型随手给个 tier1/tier2 就能绕过 fix_policy 的阈值（把高难度条目
    也派去自动修复），且与评测报告里的优先级自相矛盾。
    """
    processor = FlushProcessor(ctx)

    async def fake_label(payload, labels):
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", fake_label)

    # a：难度低 + 重要性高 → 规则给 tier1（模型故意说 none）
    a = _seed_issue(repo, 11)
    # b：难度高 → 规则给 none（模型故意说 tier1）
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    b = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=12,
                item_type=ItemType.ISSUE, title="架构级重构", body="big")
    repo.upsert_item(rid, b)
    repo.save_evaluation(b.key, Evaluation(
        category=Category.BUG, priority=Priority.NONE, model="stub",
        scores=Scores(
            authenticity=DimensionScore(score=95),
            importance=DimensionScore(score=90),
            difficulty=DimensionScore(score=75),      # 高于 tier 阈值
        ),
    ))

    entries = [{"key": i.key, "repo": i.repo, "number": i.number, "item_type": "issue"} for i in (a, b)]
    conclusions = [
        {"key": a.key, "verdict": "fix", "labels": [], "priority": Priority.NONE, "reason": "", "confidence": 0.9},
        {"key": b.key, "verdict": "fix", "labels": [], "priority": Priority.TIER1, "reason": "", "confidence": 0.9},
    ]
    report = await processor.apply(QueueName.VERIFY.value, entries, conclusions)

    # 只有规则判定为 tier1 的 a 进入自动修复；b 难度过高，模型再怎么说 tier1 也不派发
    assert report.fix_candidates == [(a.key, Priority.TIER1)]
    assert repo.get_item_row(b.key).status == ItemStatus.NEEDS_MANUAL.value


def test_decide_labels_mapping(ctx) -> None:
    processor = FlushProcessor(ctx)
    labels = processor._decide_labels(QueueName.VERIFY.value, "fix", {"labels": ["bug"]})
    assert "reproduced" in labels and "bug" in labels and "fissue" in labels

    labels_pr = processor._decide_labels(QueueName.FIX_BUG.value, "merge", {"labels": []})
    assert "ai-verified" in labels_pr


# ---------------------------------------------------------------------------
# 优先级规划
# ---------------------------------------------------------------------------


async def test_plan_priorities_batches_and_updates(ctx, repo, monkeypatch) -> None:
    processor = FlushProcessor(ctx)
    items = [_seed_issue(repo, n) for n in (1, 2)]

    async def fake_plan(rows):
        return {r["key"]: {"priority": Priority.TIER1, "reason": "低难度高重要性", "confidence": 0.9} for r in rows}

    monkeypatch.setattr(ctx.evaluator, "plan_fix_priority", fake_plan)
    out = await processor.plan_priorities(items)
    assert set(out) == {i.key for i in items}
    assert all(p is Priority.TIER1 for p in out.values())
    assert repo.get_item_row(items[0].key).priority == Priority.TIER1.value


async def test_plan_priorities_ignores_non_bug_and_pr(ctx, repo) -> None:
    processor = FlushProcessor(ctx)
    _seed_issue(repo, 1, category=Category.FEATURE)
    assert await processor.plan_priorities(list(repo.list_items())) == {}


def test_fix_candidates_orders_tier1_first(ctx, repo) -> None:
    processor = FlushProcessor(ctx)
    t2 = _seed_issue(repo, 1, priority=Priority.TIER2)
    t1 = _seed_issue(repo, 2, priority=Priority.TIER1)
    repo.set_item_status(t2.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER2)
    repo.set_item_status(t1.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    candidates = processor.fix_candidates()
    assert [c.number for c in candidates] == ["2", "1"]


def test_fix_candidates_only_returns_still_queued(ctx, repo) -> None:
    """只有仍停在 fix_queued 的条目才是候选。

    回归（envkit 实测）：``fix_candidates`` 此前只按 priority 取条目，不筛状态。
    priority 是评测阶段落的「值不值得修」，修完不会清掉，于是已经转成
    needs_manual / pr_created 的条目仍会被选中——``fix --limit 3`` 连跑四轮
    都只在重复处理同一批 #9/#8/#7，后面的 #1-#6 永远轮不到。
    """
    processor = FlushProcessor(ctx)

    done = _seed_issue(repo, 1, priority=Priority.TIER1)
    repo.set_item_status(done.key, ItemStatus.NEEDS_MANUAL, priority=Priority.TIER1)

    opened = _seed_issue(repo, 2, priority=Priority.TIER1)
    repo.set_item_status(opened.key, ItemStatus.PR_CREATED, priority=Priority.TIER1)

    still = _seed_issue(repo, 3, priority=Priority.TIER1)
    repo.set_item_status(still.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    candidates = processor.fix_candidates()

    assert [c.number for c in candidates] == ["3"]     # 只剩余队条目
    assert repo.get_item_row(still.key).status == ItemStatus.FIX_QUEUED.value


def test_fix_candidates_advances_to_next_batch_across_rounds(ctx, repo) -> None:
    """连跑多轮必须能推进到下一批，而不是每轮从同一批重修。

    只断言「跨轮不重复、且两轮合起来恰好覆盖全部待修条目」——
    同一秒内写入的条目之间排序是并列的，具体谁先谁后不属于契约。
    """
    processor = FlushProcessor(ctx)
    for n in (1, 2, 3, 4):
        item = _seed_issue(repo, n, priority=Priority.TIER1)
        repo.set_item_status(item.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    first = processor.fix_candidates(limit=2)
    assert len(first) == 2

    # 第一轮的两条修完后转为 needs_manual
    for c in first:
        repo.set_item_status(c.key, ItemStatus.NEEDS_MANUAL, priority=Priority.TIER1)

    second = processor.fix_candidates(limit=2)
    assert len(second) == 2
    assert not {c.number for c in first} & {c.number for c in second}   # 不重复处理
    assert {c.number for c in first} | {c.number for c in second} == {"1", "2", "3", "4"}  # 全部轮到过


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


async def test_context_adapter_cache_and_platform_lookup(ctx, settings) -> None:
    cfg = settings.repo("psf/requests")
    a1 = await ctx.adapter_for_repo(cfg)
    a2 = await ctx.adapter_for_repo(cfg)
    assert a1 is a2                                   # 命中缓存
    assert ctx.repo_ref(cfg).slug == "psf/requests"
    p = ctx.adapter_for_platform(Platform.GITEE)
    assert p is ctx.adapter_for_platform(Platform.GITEE)


def test_context_budget_guard_reads_usage(ctx, settings) -> None:
    guard = ctx.budget_guard("psf/requests")
    assert guard.budget.daily_tokens_per_repo == settings.llm.budget.daily_tokens_per_repo
    guard.check()                                     # 无用量 → 不抛


def test_usage_sink_attributes_spend_to_the_repo(ctx, repo, settings) -> None:
    """记账必须落到**仓库**键上，否则 per-repo 每日预算恒读 ~0、永远不会熔断。

    回归：`_make_usage_sink` 原先取 `usage.purpose.split(":")[0]` 当 repo_key——那取到的是
    **用途**本身，于是每一行 `llm_usage` 的 repo_key 都是空串；而 `usage_today(repo_key=slug)`
    正是按这个键汇总的，daemon 的 `_budget_ok` 于是永远读到 ~0（`except BudgetExceeded` 那两处
    成了死代码）。
    """
    from fissue.ai.client import Usage
    from fissue.errors import BudgetExceeded
    from fissue.pipeline.context import _make_usage_sink

    sink = _make_usage_sink(repo)
    sink(
        Usage(
            purpose="evaluate:psf/requests",
            model="stub",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.01,
        )
    )

    used = repo.usage_today(repo_key="psf/requests")
    assert used["total_tokens"] == 15, f"用量应当记在该仓库名下，实际：{used}"
    # 别的仓库不该看到这笔（注意：`repo_key=""` 在 usage_today 里表示**不加过滤**，
    # 所以这里不能用它来验「空桶」）
    assert repo.usage_today(repo_key="other/repo")["total_tokens"] == 0

    # 归属修好之后，per-repo 守卫才真的看得见这笔用量
    settings.llm.budget.daily_tokens_per_repo = 10
    with pytest.raises(BudgetExceeded):
        ctx.budget_guard("psf/requests").check()


async def test_require_repos_helper(ctx, settings) -> None:
    from fissue.pipeline.context import require_repos

    assert [r.slug for r in require_repos(ctx, None)] == ["psf/requests"]
    assert [r.slug for r in require_repos(ctx, ["psf/requests"])] == ["psf/requests"]


# ---------------------------------------------------------------------------
# PR 验证：base 必须跑在「未修复」的代码上
#
# 回归：曾经先 _merge_pr 再跑 base，于是 base 跑在“已修复”的代码上必然通过，
# 被 judge_f2p 判成「验证器不可靠」——任何正确的 PR 都无法被推荐合并。
# ---------------------------------------------------------------------------


class _FakeWorkspace:
    """只提供 PRVerifyStage 用到的几个方法。"""

    def detect_language(self) -> str:
        return "python"

    def file_tree(self, limit: int = 150) -> str:
        return "textkit/slugify.py"

    def readme(self) -> str:
        return "# textkit"

    def api_signatures(self, **kw) -> str:
        """公开 API 契约（真实工作区由 ast 抽取；替身给最小样本）。"""
        return "def slugify(text: str) -> str"

    def call_graph(self, **kw) -> str:
        """跨模块调用链（真实工作区由 ast 抽取；替身给最小样本）。"""
        return "stats.count_batches → pipeline.drop_ranges"

    def detect_test_command(self, hint: str | None = None) -> str | None:
        """回归门探测命令；返回 None 表示探不到 → 该门记 skipped，不打扰本用例。"""
        return None


def _pr_item(number: int = 43) -> RawItem:
    from fissue.models import FileChange

    return RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number,
        item_type=ItemType.PR, title="fix: 修复超时处理", body="修复 #42",
        head_branch="fix-timeout", base_branch="main", linked_issues=[42],
        # 带上文件清单：默认是「改了代码」的 PR。
        # 没有它，PR 验证会去平台现拉一次文件列表（判「纯文档改动」用），
        # 而单测禁止真实网络，于是卡在重试退避上直到超时。
        files=[FileChange(path="requests/adapters.py", status="modified", additions=5, deletions=2)],
    )


def _spec():
    from fissue.models import VerifierKind, VerifierSpec

    return VerifierSpec(
        kind=VerifierKind.EXECUTABLE,
        command="python -m pytest -q",
        files={"tests/test_repro.py": "def test_x():\n    assert False\n"},
    )


def _verifier_result(base_outcome, fix_outcome=None):
    from fissue.models import VerifierKind, VerifierResult, VerifierRun

    base = VerifierRun(item_key="k", stage="base", outcome=base_outcome,
                       exit_code=0 if base_outcome.value == "pass" else 1)
    fix = None
    if fix_outcome is not None:
        fix = VerifierRun(item_key="k", stage="fix", outcome=fix_outcome,
                          exit_code=0 if fix_outcome.value == "pass" else 1)
    return VerifierResult(kind=VerifierKind.EXECUTABLE, base_run=base, fix_run=fix,
                          conclusion="stub")


def _make_pr_stage(ctx, monkeypatch, *, spec, base_result, fix_result=None, order=None):
    """装配一个 PRVerifyStage；把克隆、验证、合并全部打桩，并记录调用顺序。"""
    from fissue.pipeline.stages import PRVerifyStage

    stage = PRVerifyStage(ctx)

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWorkspace()

    async def fake_validate(*, item, workspace, repo_context, generator, test_hint=None,
                            linked_context=None):
        if order is not None:
            order.append("base")
        # verifier_id 必须是**库里真有的行**：SQLite 开了外键强制之后，自造的 id 会让
        # `save_verifier_run` 撞 FK（以前不强制，所以自造 id 也能“过”）。
        verifier_id = ctx.repo.save_verifier(item.key, spec)
        return verifier_id, spec, base_result

    async def fake_merge(ws, it, repo_cfg):
        if order is not None:
            order.append("merge")
        return True, "已合并（stub）"

    async def fake_fix(verify_ctx, *, verifier_id=None, base_run=None):
        if order is not None:
            order.append("fix")
        return fix_result or base_result

    monkeypatch.setattr(ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(ctx.verifier, "generate_and_validate", fake_validate)
    monkeypatch.setattr(ctx.verifier, "verify_fix", fake_fix)
    monkeypatch.setattr(stage, "_merge_pr", fake_merge)
    return stage


async def test_pr_verify_passes_linked_issue_body_to_verifier(ctx, repo, monkeypatch) -> None:
    """PR 验证必须把**关联 Issue 的正文**交给验证器生成。

    回归：PR 作者常在正文里只复述自己修的那一个场景（#7 只说 `Hello   World`），
    而完整复现用例写在原 Issue 里（#2 还列了 `Hello - World`、`a  -  b`）。
    不喂这段材料，验证器只覆盖 PR 提到的场景，会把「只修一半」判成通过。
    """
    from fissue.models import VerifierOutcome
    from fissue.pipeline.stages import PRVerifyStage

    # 关联的原 Issue #2，正文含 PR 自己没提的反例
    issue = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=2,
                    item_type=ItemType.ISSUE, title="slugify 连续分隔符未折叠",
                    body='期望：slugify("Hello - World") == "hello-world"')
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, issue)

    pr = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=7, item_type=ItemType.PR,
                 title="fix: slugify 连续空白", body="Hello   World 已修好", linked_issues=[2])
    repo.upsert_item(rid, pr)

    captured: dict[str, Any] = {}

    async def fake_validate(*, item, workspace, repo_context, generator, test_hint=None,
                            linked_context=None):
        captured["linked_context"] = linked_context
        return 1, _spec(), _verifier_result(VerifierOutcome.PASS)

    async def fake_clone(repo_cfg, **kwargs):
        return _FakeWorkspace()

    monkeypatch.setattr(ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(ctx.verifier, "generate_and_validate", fake_validate)

    stage = PRVerifyStage(ctx)
    await stage.run(pr, Evaluation(category=Category.BUG, model="stub"))

    linked = captured.get("linked_context") or ""
    assert "Hello - World" in linked          # PR 正文没有、只有 Issue 里才有的反例
    assert "Issue #2" in linked


async def test_pr_verify_runs_base_before_merge(ctx, repo, monkeypatch) -> None:
    from fissue.models import VerifierOutcome

    item = _pr_item()
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    order: list[str] = []
    spec = _spec()
    base = _verifier_result(VerifierOutcome.FAIL)
    fix = _verifier_result(VerifierOutcome.FAIL, VerifierOutcome.PASS)
    fix.f2p_satisfied = True

    stage = _make_pr_stage(ctx, monkeypatch, spec=spec, base_result=base,
                           fix_result=fix, order=order)
    result = await stage.run(item, Evaluation(category=Category.BUG, model="stub"))

    # 合并必须发生在 base 之后：先证明问题存在，再看 PR 是否修好
    assert order == ["base", "merge", "fix"]
    assert result.queued is QueueName.FIX_BUG
    assert repo.get_item_row(item.key).status == ItemStatus.QUEUED.value


async def test_pr_verify_unreproducible_base_never_merges(ctx, repo, monkeypatch) -> None:
    from fissue.models import VerifierOutcome

    item = _pr_item(44)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    order: list[str] = []
    # base 就通过 → 验证器证明不了这个 PR 修的是什么
    base = _verifier_result(VerifierOutcome.PASS)
    base.conclusion = "验证器在未修复代码上就已通过，无法证明问题存在（验证器不可靠）"

    stage = _make_pr_stage(ctx, monkeypatch, spec=_spec(), base_result=base, order=order)
    result = await stage.run(item, Evaluation(category=Category.BUG, model="stub"))

    assert "merge" not in order            # 证明不了问题，就不该再合并取假结论
    assert "fix" not in order
    assert result.status is ItemStatus.NEEDS_MANUAL
    assert repo.get_item_row(item.key).status == ItemStatus.NEEDS_MANUAL.value


@pytest.mark.parametrize(
    "spam_kwargs, keyword",
    [
        ({"is_spam": True}, "刷量"),
        ({"is_duplicate": True, "duplicate_of": [1]}, "重复"),
    ],
)
async def test_pr_verify_blocks_suspicious_before_verifying(
    ctx, repo, monkeypatch, spam_kwargs, keyword
) -> None:
    """疑似刷量/重复的 PR 与 Issue 同口径：不验证、不合并。"""
    from fissue.models import SpamSignal
    from fissue.pipeline.stages import PRVerifyStage

    item = _pr_item(45 if keyword == "刷量" else 46)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    order: list[str] = []

    async def boom(**kw):
        raise AssertionError("疑似刷量/重复的 PR 不该进验证/合并流程")

    monkeypatch.setattr(ctx, "clone_workspace", boom)
    monkeypatch.setattr(ctx.verifier, "generate_and_validate", boom)

    ev = Evaluation(category=Category.BUG, model="stub", spam=SpamSignal(**spam_kwargs))
    result = await PRVerifyStage(ctx).run(item, ev)

    assert order == []
    assert result.status is ItemStatus.SKIPPED
    assert keyword in (result.skipped_reason or "")
    assert repo.get_item_row(item.key).status == ItemStatus.SKIPPED.value


# ---------------------------------------------------------------------------
# 重复检测：候选预筛 + 接入评测
# ---------------------------------------------------------------------------


def test_similar_to_picks_duplicate_titles() -> None:
    from fissue.pipeline.stages import _similar_to

    def issue(number: int, title: str) -> RawItem:
        return RawItem(platform=Platform.GITHUB, repo="psf/requests", number=number,
                       item_type=ItemType.ISSUE, title=title, body="")

    a = issue(2, "slugify 连续分隔符未折叠，产生 hello---world 这样的 slug")
    b = issue(5, "slugify 生成的 URL 里出现多余的横线")
    c = issue(4, "支持 CJK 文本按显示宽度折行（wrap 目前只按空格分词）")

    got = _similar_to(b, [a, b, c])
    assert [g["number"] for g in got] == [2]      # 只挑出同属 slugify 的那条


def test_similar_to_ignores_prs_and_newer_items() -> None:
    """候选必须是同类型且更早的条目。

    回归：#1 曾被判成 #6（**修 #1 的那个 PR**）的重复——Issue 不可能是
    “修它的 PR” 的重复，这是纯假阳性；#2 也不该把更晚的 #5 当作自己的重复。
    """
    from fissue.pipeline.stages import _similar_to

    def it(number: int, title: str, item_type) -> RawItem:
        return RawItem(platform=Platform.GITHUB, repo="psf/requests", number=number,
                       item_type=item_type, title=title, body="")

    issue1 = it(1, 'word_count("") 返回 1，空字符串应返回 0', ItemType.ISSUE)
    pr6 = it(6, "fix: word_count 对空字符串应返回 0", ItemType.PR)
    issue2 = it(2, "slugify 连续分隔符未折叠，产生 hello---world 这样的 slug", ItemType.ISSUE)
    issue5 = it(5, "slugify 生成的 URL 里出现多余的横线", ItemType.ISSUE)

    pool = [issue1, pr6, issue2, issue5]

    assert _similar_to(issue1, pool) == []        # #6 是 PR，不算重复对象
    assert _similar_to(issue2, pool) == []        # #5 更晚，单向指向更早的
    assert [g["number"] for g in _similar_to(issue5, pool)] == [2]


def test_similar_to_recalls_chinese_duplicate_titles() -> None:
    """中文重复标题必须能被预筛召回，否则连候选都进不去。

    回归（ratekit 实测）：#8「含税总价反推出来的不含税价好像不对」与 #2
    「remove_tax 公式错误，含税价反推净额严重偏低」是同一问题，但中文切成
    **单字**后相似度只有 0.21，达不到阈值 —— LLM 从未收到 #2 作为候选，于是
    漏检。改成中文 bigram + 包含度后应能召回；同时不该把只是共用
    ``parse_amount`` 这个词的 #3 也拉进来。
    """
    from fissue.pipeline.stages import _similar_to

    def issue(number: int, title: str) -> RawItem:
        return RawItem(platform=Platform.GITHUB, repo="John-is-playing/ratekit",
                       number=number, item_type=ItemType.ISSUE, title=title, body="")

    dup2 = issue(2, "remove_tax 公式错误，含税价反推净额严重偏低")
    other3 = issue(3, "parse_amount 丢失负号，负数金额被解析成正数")
    dup8 = issue(8, "含税总价反推出来的不含税价好像不对")

    got = [g["number"] for g in _similar_to(dup8, [dup2, other3, dup8])]
    assert got == [2]                             # 召回真重复 #2，且不误召 #3


def test_similar_to_recalls_duplicate_by_body_evidence() -> None:
    """标题措辞完全不同、但正文在说同一处代码的重复，也要能进候选。

    回归（pipekit 实测）：#12「count_batches 有时候会少算一个分片」与 #3
    「in_ranges 漏掉区间右端点，进而算错剩余分片数」实为同一问题，但两条标题
    的标识符完全不同，词面包含度只有 0.10，仅靠标题连候选都进不去，模型因此
    从未看到 #3，漏判为非重复。它们正文贴的是同一段过滤逻辑，共享
    ``count_batches`` / ``ranges`` / ``stats`` 这些辨识性标识符。
    """
    from fissue.pipeline.stages import _similar_to

    def issue(number: int, title: str, body: str) -> RawItem:
        return RawItem(platform=Platform.GITHUB, repo="John-is-playing/pipekit",
                       number=number, item_type=ItemType.ISSUE, title=title, body=body)

    dup3 = issue(
        3,
        "in_ranges 漏掉区间右端点，进而算错剩余分片数",
        "`filterops.in_ranges(point, ranges)` 对右端点返回 False。\n"
        "```python\nfrom pipekit import in_ranges, count_batches, drop_ranges\n"
        "print(count_batches([(0, 4), (5, 9)], [(5, 5)]))\n"
        "print(drop_ranges([(5, 9)], [(5, 9)]))\n```\n",
    )
    other1 = issue(
        1,
        "intervals.merge 不合并端点相接的区间",
        "`merge` 漏了端点相接的情况。\n"
        "```python\nfrom pipekit.intervals import merge\nprint(merge([(1, 3), (3, 5)]))\n```\n",
    )
    dup12 = issue(
        12,
        "count_batches 有时候会少算一个分片",
        "过滤用的 ranges 边界正好跟分片起点重合的时候最容易出现。\n"
        "```python\nfrom pipekit import count_batches, drop_ranges\n"
        "print(count_batches(slices, ranges))\n```\n",
    )

    got = [g["number"] for g in _similar_to(dup12, [dup3, other1, dup12])]
    assert 3 in got                                # 正文同源 → 召回 #3


def test_similar_to_ignores_boilerplate_only_bodies() -> None:
    """只共享贴代码的样板词（import/print/return）不算重复证据。"""
    from fissue.pipeline.stages import _similar_to

    def issue(number: int, title: str, body: str) -> RawItem:
        return RawItem(platform=Platform.GITHUB, repo="psf/requests", number=number,
                       item_type=ItemType.ISSUE, title=title, body=body)

    boiler = (
        "复现步骤：\n```python\n"
        "import requests\nfrom requests import get\nprint(get('https://example.com'))\n"
        "result = 1\nreturn result\n```\n"
    )
    a = issue(2, "连接超时后没有重试", boiler)
    b = issue(5, "重定向时丢掉了请求头", boiler)

    assert [g["number"] for g in _similar_to(b, [a, b])] == []


def test_similar_to_body_signal_needs_more_than_one_shared_word() -> None:
    """正文里只共享一个标识符不足以当重复证据（同模块下的不同问题）。

    标题刻意不共享任何词——否则会被**标题通道**的 ASCII 抬分命中，
    测不到正文通道本身。
    """
    from fissue.pipeline.stages import _similar_to

    def issue(number: int, title: str, body: str) -> RawItem:
        return RawItem(platform=Platform.GITHUB, repo="John-is-playing/pipekit",
                       number=number, item_type=ItemType.ISSUE, title=title, body=body)

    a = issue(2, "结果整体偏大", "coverage 算出来的值不对。\n```python\nprint(coverage(x))\n```\n")
    b = issue(6, "空列表会让统计崩溃", "传空列表进来就崩。\n```python\nprint(coverage([]))\n```\n")

    # 标题无共享词；正文只共享 coverage 一个标识符 → 不构成候选
    assert [g["number"] for g in _similar_to(b, [a, b])] == []


async def test_eval_batch_passes_similar_candidates(ctx, repo) -> None:
    from fissue.pipeline.stages import EvalStage

    seen: dict[str, Any] = {}

    class _FakeEval:
        async def evaluate(self, item, *, similar=None):
            seen[item.key] = similar
            return Evaluation(
                category=Category.BUG,
                scores=Scores(
                    authenticity=DimensionScore(score=90),
                    importance=DimensionScore(score=80),
                    difficulty=DimensionScore(score=20),
                ),
                model="stub",
            )

    ctx.evaluator = _FakeEval()
    a = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=2,
                item_type=ItemType.ISSUE, title="slugify 连续分隔符未折叠", body="")
    b = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=5,
                item_type=ItemType.ISSUE, title="slugify 生成的 URL 里出现多余的横线", body="")

    await EvalStage(ctx).run_batch([a, b])

    # 同批内互相可见（此前 similar 从未接线），重复关系单向指向更早的 #2
    assert seen[b.key] and any(s["number"] == 2 for s in seen[b.key])
    assert not seen[a.key]


async def test_flush_reclaims_interrupted_residue_first(ctx, repo, settings, monkeypatch) -> None:
    """flush 先回收中断残留，判定才恢复得了。

    场景：一条已 `done` 待定论 + 一条上一轮中断留下的 `pending`。残留让 `pending_count > 0`，
    于是「队列已无待处理条目」这条兜底失效——哪怕只剩一条 done 也不肯自动 flush；回收后
    正常消费掉。
    """
    settings.queues.verify_queue.flush_size = 100            # 数量阈值远不可达
    settings.queues.verify_queue.idle_flush_seconds = 99999  # 空闲兜底也关掉
    qm = QueueManager(ctx)

    done_item = _seed_issue(repo, 71)
    eid = qm.enqueue(QueueName.VERIFY, done_item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    stuck = _seed_issue(repo, 72)
    repo.set_item_status(stuck.key, ItemStatus.QUEUED)
    qm.enqueue(QueueName.VERIFY, stuck.key)                  # 中断：没有 finish

    # 回收前：残留把兜底条件废掉
    assert qm.should_flush(QueueName.VERIFY).should_flush is False

    monkeypatch.setattr(repo, "STUCK_QUEUE_ENTRY_SECONDS", -1)      # 模拟超龄

    async def handler(queue_kind, entries):
        return [
            {"key": e["key"], "verdict": "skip", "labels": [], "priority": Priority.NONE,
             "reason": "r", "confidence": 0.5, "model": "stub"}
            for e in entries
        ]

    outcome = await qm.flush(QueueName.VERIFY, handler=handler)

    assert outcome.flushed == 1, "回收后兜底恢复，这唯一一条 done 应当被消费"
    assert repo.queue_pending_count(QueueName.VERIFY) == 0
    assert repo.get_item_row(stuck.key).status == ItemStatus.EVALUATED.value, "卡住的条目被放回重验"


async def test_flush_releases_stale_claims_before_the_decision(ctx, repo, settings, monkeypatch) -> None:
    """超龄的 `running` 抢占要在**判定之前**回收，否则队列永久卡死。

    这些行既不是 `done`、也不算 `pending`：`should_flush` 看不见它们 → 早退「没有已完成待定论
    的条目」→ 而回收代码（`claim_unflushed` 内那次）只在判定为真时才跑——死结。真跑里落库中途
    报错就能造出这个形状（见下一条用例），此后**每一轮 flush 都会直接早退**。
    """
    settings.queues.verify_queue.flush_size = 100            # 数量阈值远不可达
    settings.queues.verify_queue.idle_flush_seconds = 99999  # 空闲兜底也关掉
    qm = QueueManager(ctx)

    item = _seed_issue(repo, 81)
    eid = qm.enqueue(QueueName.VERIFY, item.key)
    repo.finish_queue_entry(eid, status="done", result={})
    assert repo.claim_unflushed(QueueName.VERIFY, limit=10), "先正常抢一次，制造 running 行"

    monkeypatch.setattr(repo, "CLAIM_STALE_SECONDS", -1)     # 模拟超龄

    async def handler(queue_kind, entries):
        return [
            {"key": e["key"], "verdict": "skip", "labels": [], "priority": Priority.NONE,
             "reason": "r", "confidence": 0.5, "model": "stub"}
            for e in entries
        ]

    outcome = await qm.flush(QueueName.VERIFY, handler=handler)

    assert outcome.flushed == 1, "超龄抢占回收后，这条应当被正常消费"


async def test_flush_releases_the_claim_when_the_db_tail_fails(ctx, repo, settings, monkeypatch) -> None:
    """落库/收尾段抛异常时**必须把抢占还回去**，否则条目停在 running、该队列再也 flush 不了。

    回归：claim 之后的整段（`save_batch_conclusion` → `finish_flush_claim` → 收尾）原先没有任何
    异常保护（`try` 只包住 handler）。异常一抛，行停在 `running`，而 `queue_unflushed` 只认 done、
    `queue_pending_count` 只认 pending——谁也看不见它：队列表面上「没有待定论条目」。
    """
    settings.queues.verify_queue.flush_size = 100
    settings.queues.verify_queue.idle_flush_seconds = 99999
    qm = QueueManager(ctx)

    item = _seed_issue(repo, 82)
    eid = qm.enqueue(QueueName.VERIFY, item.key)
    repo.finish_queue_entry(eid, status="done", result={})

    def boom(*a, **kw):
        raise RuntimeError("DB 抖了")

    monkeypatch.setattr(repo, "save_batch_conclusion", boom)

    async def handler(queue_kind, entries):
        return [
            {"key": e["key"], "verdict": "fix", "labels": [], "priority": Priority.NONE,
             "reason": "r", "confidence": 0.5, "model": "stub"}
            for e in entries
        ]

    outcome = await qm.flush(QueueName.VERIFY, handler=handler)

    assert outcome.error and "DB 抖了" in outcome.error, "失败要如实报出来，不能假装成功"
    assert outcome.flushed == 0
    assert repo.claim_unflushed(QueueName.VERIFY, limit=10), (
        "抢占必须归还——否则条目永久停在 running，而 should_flush 看不见 running"
    )


async def test_eval_skips_items_with_incomplete_comments(ctx, repo) -> None:
    """评论残缺时不评测。

    在没有讨论上下文的数据上下结论，而那个结论会被**回写到平台**、下一轮又会重抓同一条
    （游标本轮被冻结）——错误就此固化。跳过它：条目留在 new，等抓到完整评论再评。
    """
    from fissue.pipeline.stages import EvalStage

    called: list[str] = []

    class _FakeEval:
        async def evaluate(self, item, *, similar=None):  # noqa: ANN001, ANN202
            called.append(item.key)
            return Evaluation(
                category=Category.BUG,
                scores=Scores(importance=DimensionScore(score=80)),
                model="stub",
            )

    ctx.evaluator = _FakeEval()
    incomplete = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=9,
        item_type=ItemType.ISSUE, title="t", body="b", comments=[],
    )
    incomplete.mark_degraded("comments_unavailable#9")

    result = await EvalStage(ctx).run_batch([incomplete])

    assert called == [], "残缺条目不该被送去评测"
    assert result.evaluated == 0 and result.skipped == 1
    assert "残缺" in (result.results[0].skipped_reason or ""), "跳过原因要写清是评论残缺"


# ---------------------------------------------------------------------------
# 验证器生成：缺 command 时重生成
# ---------------------------------------------------------------------------


async def test_generate_and_validate_retries_when_executable_lacks_command(ctx, repo, monkeypatch) -> None:
    """可执行验证器缺 command 时应带反馈重生成，而不是拿它白跑一次沙盒。

    回归（ratekit #6 实测）：模型给出 kind=executable 却没有 command，此前只记一条
    warning 就继续调 run_once —— 跑不出任何结论，白白浪费一次沙盒执行，最终落个
    f2p=false 的「可执行」验证器。应重生成以补齐命令。
    """
    from fissue.ai.client import Usage
    from fissue.models import VerifierKind, VerifierOutcome, VerifierRun, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 21)

    no_cmd = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="bad",
                          files={"t.py": "x"}, command=None)
    with_cmd = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="good",
                            files={"t.py": "x"}, command="python -m pytest t.py -q")

    calls = {"refine": 0, "run": 0}

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=no_cmd, usage=Usage(),
                                     warnings=["可执行验证器缺少 command"])

        async def refine(self, spec, **kw):
            calls["refine"] += 1
            return GeneratedVerifier(spec=with_cmd, usage=Usage(), warnings=[])

    async def fake_run_once(**kw):
        calls["run"] += 1
        return VerifierRun(item_key=item.key, stage="base",
                           outcome=VerifierOutcome.FAIL, exit_code=1)

    monkeypatch.setattr(ctx.verifier, "run_once", fake_run_once)
    _vid, spec, result = await ctx.verifier.generate_and_validate(
        item=item, workspace=object(), repo_context="", generator=_Gen()
    )

    assert calls["refine"] == 1                       # 缺 command → 触发一次重生成
    assert spec.command == "python -m pytest t.py -q"  # 用补齐后的命令继续
    assert calls["run"] == 1
    assert result.reproducible is True


async def test_generate_and_validate_unreliable_when_command_never_given(ctx, repo, monkeypatch) -> None:
    """修正多轮仍不给 command → 直接判不可靠，不浪费沙盒执行。"""
    from fissue.ai.client import Usage
    from fissue.models import VerifierKind, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 22)
    no_cmd = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="bad",
                          files={"t.py": "x"}, command=None)

    calls = {"refine": 0, "run": 0}

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=no_cmd, usage=Usage(), warnings=[])

        async def refine(self, spec, **kw):
            calls["refine"] += 1
            return GeneratedVerifier(spec=no_cmd, usage=Usage(), warnings=[])

    async def fake_run_once(**kw):
        calls["run"] += 1
        return None

    monkeypatch.setattr(ctx.verifier, "run_once", fake_run_once)
    max_rounds = max(1, ctx.settings.verifier.max_rounds)
    _vid, spec, result = await ctx.verifier.generate_and_validate(
        item=item, workspace=object(), repo_context="", generator=_Gen()
    )

    assert calls["run"] == 0                          # 从未带着空命令跑沙盒
    assert calls["refine"] == max_rounds - 1          # 每轮都尝试重生成
    assert result.f2p_satisfied is False
    assert "缺少 command" in result.conclusion


# ---------------------------------------------------------------------------
# 验证器自身写坏：与「问题已复现」区分开
# ---------------------------------------------------------------------------


async def test_generate_and_validate_retries_on_verifier_self_error(ctx, repo, monkeypatch) -> None:
    """验证器引用了库里不存在的 API → 应带反馈重生成，而不是当成「问题已复现」。

    回归（envkit #12 实测）：生成的测试调用了 ``TTLCache.put``（库里只有 ``set``），
    pytest 因 AttributeError 非零退出，被 judge_f2p 当作 base 失败 = 复现，
    最终这条真问题被打上 invalid。
    """
    from fissue.ai.client import Usage
    from fissue.models import VerifierKind, VerifierOutcome, VerifierRun, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 23)
    bad = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="bad",
                       files={"t.py": "x"}, command="python -m pytest t.py -q")
    good = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="good",
                        files={"t.py": "y"}, command="python -m pytest t.py -q")

    calls = {"refine": 0, "run": 0}

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=bad, usage=Usage(), warnings=[])

        async def refine(self, spec, **kw):
            calls["refine"] += 1
            # 反馈里应明确指出是验证器自身的问题
            assert "验证器" in kw.get("failure_output", "")
            return GeneratedVerifier(spec=good, usage=Usage(), warnings=[])

    async def fake_run_once(**kw):
        calls["run"] += 1
        if calls["run"] == 1:
            # 第一次：验证器自己调用了不存在的方法。
            # 定位行落在验证器自己的文件（t.py）里 —— 真实 pytest 的输出形态。
            return VerifierRun(
                item_key=item.key, stage="base", outcome=VerifierOutcome.FAIL, exit_code=1,
                stdout=(
                    ">       cache.put(\"k\", \"v\")\n"
                    "E       AttributeError: 'TTLCache' object has no attribute 'put'\n\n"
                    "t.py:21: AttributeError\n"
                ),
            )
        # 第二次：真正的断言失败 → 问题确实可复现
        return VerifierRun(
            item_key=item.key, stage="base", outcome=VerifierOutcome.FAIL, exit_code=1,
            stdout=(
                ">       assert parse_offset('-05:30') == -330\n"
                "E       AssertionError: assert 330 == -330\n\n"
                "t.py:9: AssertionError\n"
            ),
        )

    monkeypatch.setattr(ctx.verifier, "run_once", fake_run_once)
    _vid, spec, result = await ctx.verifier.generate_and_validate(
        item=item, workspace=object(), repo_context="", generator=_Gen()
    )

    assert calls["run"] == 2          # 自错那一轮不算复现，重试了一次
    assert calls["refine"] == 1
    assert spec.name == "good"
    assert result.reproducible is True


async def test_generate_and_validate_gives_up_after_self_error_rounds(ctx, repo, monkeypatch) -> None:
    """每轮都因验证器自身出错而失败 → 到最后判不可靠，不谎报「已复现」。"""
    from fissue.ai.client import Usage
    from fissue.models import VerifierKind, VerifierOutcome, VerifierRun, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 24)
    bad = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="bad",
                       files={"t.py": "x"}, command="python -m pytest t.py -q")

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=bad, usage=Usage(), warnings=[])

        async def refine(self, spec, **kw):
            return GeneratedVerifier(spec=bad, usage=Usage(), warnings=[])

    async def fake_run_once(**kw):
        # 每轮都是验证器自身写错：定位行落在验证器自己的文件（t.py）里
        return VerifierRun(
            item_key=item.key, stage="base", outcome=VerifierOutcome.FAIL, exit_code=1,
            stdout=(
                ">       import nonexistent\n"
                "E       ModuleNotFoundError: No module named 'nonexistent'\n\n"
                "t.py:1: ModuleNotFoundError\n"
            ),
        )

    monkeypatch.setattr(ctx.verifier, "run_once", fake_run_once)
    _vid, spec, result = await ctx.verifier.generate_and_validate(
        item=item, workspace=object(), repo_context="", generator=_Gen()
    )

    assert result.reproducible is False         # 绝不能把自错当成复现
    assert "验证器" in result.conclusion


# ---------------------------------------------------------------------------
# 策略闸门：疑似刷量/重复的条目不得生成验证器
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spam_kwargs, keyword",
    [
        ({"is_spam": True}, "刷量"),
        ({"is_duplicate": True, "duplicate_of": [1]}, "重复"),
    ],
)
async def test_verify_stage_blocks_suspicious_before_generating(
    ctx, repo, monkeypatch, spam_kwargs, keyword
) -> None:
    """评测已判定疑似刷量/重复 → 验证阶段直接跳过，不生成验证器、不跑沙盒。

    回归（envkit #14 实测）：评测已给出 is_spam=true、建议关闭，
    验证阶段仍为它生成了验证器并跑出 fix 结论，白烧一轮沙盒。
    """
    from fissue.models import SpamSignal
    from fissue.pipeline.stages import VerifyStage

    item = _seed_issue(repo, 30 if keyword == "刷量" else 32)
    ev = Evaluation(category=Category.BUG, priority=Priority.TIER1, model="stub",
                    spam=SpamSignal(**spam_kwargs))
    repo.save_evaluation(item.key, ev)

    async def boom(**kw):                      # 走到生成验证器就是失败
        raise AssertionError("疑似刷量/重复的条目不该走到生成验证器")

    monkeypatch.setattr(ctx.verifier, "generate_and_validate", boom)
    monkeypatch.setattr(ctx, "clone_workspace", boom)

    result = await VerifyStage(ctx).run(item, ev)

    assert result.status is ItemStatus.SKIPPED
    assert keyword in (result.skipped_reason or "")
    assert repo.get_item(item.key).status is ItemStatus.SKIPPED


async def test_verify_stage_checklist_event_does_not_claim_not_reproduced(
    ctx, repo, monkeypatch
) -> None:
    """清单型验证器：base 验证**根本没跑过**（无可执行命令），事件必须如实说「待判定」，
    不能写「未能复现」——那等于谎称测过没复现。

    回归（2026-10-21 常驻轮次实测，Fissue 的 MySQL 功能请求 issue）：任务详情页
    显示「base 验证（checklist）：未能复现」，误导以为验证器跑过且判定失败。
    """
    from fissue.models import (
        Category,
        Evaluation,
        ItemStatus,
        Priority,
        VerifierKind,
        VerifierResult,
        VerifierSpec,
    )

    item = _seed_issue(repo, 33)
    ev = Evaluation(category=Category.BUG, priority=Priority.TIER1, model="stub")
    repo.save_evaluation(item.key, ev)

    spec = VerifierSpec(
        kind=VerifierKind.CHECKLIST,
        name="增加 MySQL 存储后端支持（可核查清单）",
        language="python",
        files={},
        command="",
        checklist=["【配置入口】config.example.yaml 明确列出 MySQL 支持形式"],
    )
    placeholder = VerifierResult(
        kind=spec.kind,
        f2p_satisfied=False,
        conclusion="清单型验证器：需执行后由 LLM 逐条判定",
    )

    async def fake_gav(**kw):
        return 1, spec, placeholder

    class _Sink:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def tool(self, name: str, detail: str, *, ok: bool | None = None) -> None:
            self.calls.append({"name": name, "detail": detail, "ok": ok})

    monkeypatch.setattr(VerifyStage, "_repo_context", lambda self, ws, cfg: "")
    monkeypatch.setattr(ctx.verifier, "generate_and_validate", fake_gav)
    ctx.job_sink = _Sink()

    result = await VerifyStage(ctx).run(item, ev, workspace=object())

    assert result.status is ItemStatus.NEEDS_MANUAL
    tools = [t for t in ctx.job_sink.calls if t["name"] == "验证器"]
    assert len(tools) == 1
    assert "未能复现" not in tools[0]["detail"]
    assert "判定" in tools[0]["detail"]
    assert tools[0]["ok"] is None  # 无成败结论，不该挂红 ✗


async def test_generate_and_validate_judges_checklist_with_llm(ctx, repo, monkeypatch) -> None:
    """清单型验证器：生成后把仓库上下文交 LLM 逐条判定（接通此前无调用方的
    verify_checklist 路径）。base 上判「未全部满足」→ 符合 expect_fail_on_base
    的预期方向，验证器算「可复现」（base_run=FAIL），但 f2p 仍为 false
    （清单不构成 F2P 证明）；逐条明细落验证记录，条目标 verifier_ready。"""
    from fissue.ai.client import Usage
    from fissue.models import ItemStatus, VerifierKind, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 34)
    spec = VerifierSpec(
        kind=VerifierKind.CHECKLIST,
        name="增加 MySQL 存储后端支持（可核查清单）",
        checklist=["【配置入口】config.example.yaml 列出 MySQL 支持形式"],
        expect_fail_on_base=True,
    )

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=spec, usage=Usage(), warnings=[])

        async def refine(self, spec, **kw):
            return GeneratedVerifier(spec=spec, usage=Usage(), warnings=[])

    async def fake_chat_json(messages, **kw):
        return (
            {
                "passed": False,
                "items": [{"index": 1, "satisfied": False, "reason": "证据不足"}],
                "conclusion": "MySQL 支持未实现",
            },
            None,
        )

    gen = _Gen()
    monkeypatch.setattr(ctx.generator, "generate", gen.generate)
    monkeypatch.setattr(ctx.generator, "refine", gen.refine)
    monkeypatch.setattr(ctx.verifier.client, "chat_json", fake_chat_json)

    vid, got_spec, result = await ctx.verifier.generate_and_validate(
        item=item,
        workspace=object(),
        repo_context="仓库文件（节选）：\n- pyproject.toml",
        generator=ctx.generator,
    )

    assert got_spec.kind is VerifierKind.CHECKLIST
    assert result.base_run is not None
    assert result.reproducible is True       # base 未全部满足 → 符合预期方向
    assert result.f2p_satisfied is False     # 清单不构成 F2P 证明
    assert "未全部满足" in result.conclusion and "MySQL 支持未实现" in result.conclusion
    runs = repo.verifier_runs(item.key, stage="base")
    assert len(runs) == 1 and runs[0].verifier_id == vid
    assert repo.get_item(item.key).status is ItemStatus.VERIFIER_READY


async def test_generate_and_validate_checklist_all_satisfied_on_base(ctx, repo, monkeypatch) -> None:
    """清单在 base 上被 LLM 判「全部满足」→ 问题可能已解决 → 不算复现。"""
    from fissue.ai.client import Usage
    from fissue.models import VerifierKind, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 36)
    spec = VerifierSpec(
        kind=VerifierKind.CHECKLIST,
        name="增加 MySQL 存储后端支持（可核查清单）",
        checklist=["【配置入口】config.example.yaml 列出 MySQL 支持形式"],
        expect_fail_on_base=True,
    )

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=spec, usage=Usage(), warnings=[])

        async def refine(self, spec, **kw):
            return GeneratedVerifier(spec=spec, usage=Usage(), warnings=[])

    async def fake_chat_json(messages, **kw):
        return (
            {
                "passed": True,
                "items": [{"index": 1, "satisfied": True, "reason": "已声明"}],
                "conclusion": "MySQL 支持已实现",
            },
            None,
        )

    gen = _Gen()
    monkeypatch.setattr(ctx.generator, "generate", gen.generate)
    monkeypatch.setattr(ctx.generator, "refine", gen.refine)
    monkeypatch.setattr(ctx.verifier.client, "chat_json", fake_chat_json)

    _vid, _spec, result = await ctx.verifier.generate_and_validate(
        item=item,
        workspace=object(),
        repo_context="仓库文件（节选）：\n- pyproject.toml",
        generator=ctx.generator,
    )

    assert result.reproducible is False      # base 全满足 → 按「未复现」对待
    assert "全部满足" in result.conclusion and "MySQL 支持已实现" in result.conclusion


async def test_generate_and_validate_checklist_without_llm_client(ctx, repo, monkeypatch) -> None:
    """无 LLM 客户端：不报错，回退「转人工复核」，且不留下假的 base 运行记录。"""
    from fissue.ai.client import Usage
    from fissue.models import VerifierKind, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 37)
    spec = VerifierSpec(
        kind=VerifierKind.CHECKLIST,
        name="增加 MySQL 存储后端支持（可核查清单）",
        checklist=["【配置入口】config.example.yaml 列出 MySQL 支持形式"],
    )

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=spec, usage=Usage(), warnings=[])

        async def refine(self, spec, **kw):
            return GeneratedVerifier(spec=spec, usage=Usage(), warnings=[])

    gen = _Gen()
    monkeypatch.setattr(ctx.generator, "generate", gen.generate)
    monkeypatch.setattr(ctx.generator, "refine", gen.refine)
    monkeypatch.setattr(ctx.verifier, "client", None)

    _vid, _spec, result = await ctx.verifier.generate_and_validate(
        item=item,
        workspace=object(),
        repo_context="仓库文件（节选）：\n- pyproject.toml",
        generator=ctx.generator,
    )

    assert result.base_run is None
    assert result.reproducible is False
    assert "人工" in result.conclusion
    assert repo.verifier_runs(item.key, stage="base") == []


async def test_verify_stage_checklist_llm_judgement_recorded(ctx, repo, monkeypatch) -> None:
    """VerifyStage 全链路：清单的 LLM 逐条判定结果进入任务事件与条目备注，
    不再出现「未能复现」；条目转 needs_manual，备注含判定结论。"""
    from fissue.ai.client import Usage
    from fissue.models import (
        Category,
        Evaluation,
        ItemStatus,
        Priority,
        VerifierKind,
        VerifierSpec,
    )
    from fissue.verifier.generator import GeneratedVerifier

    item = _seed_issue(repo, 35)
    ev = Evaluation(category=Category.BUG, priority=Priority.TIER1, model="stub")
    repo.save_evaluation(item.key, ev)

    spec = VerifierSpec(
        kind=VerifierKind.CHECKLIST,
        name="增加 MySQL 存储后端支持（可核查清单）",
        checklist=["【配置入口】config.example.yaml 列出 MySQL 支持形式"],
    )

    class _Gen:
        async def generate(self, item, **kw):
            return GeneratedVerifier(spec=spec, usage=Usage(), warnings=[])

    async def fake_chat_json(messages, **kw):
        return ({"passed": False, "items": [], "conclusion": "MySQL 支持未实现"}, None)

    class _Sink:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def tool(self, name: str, detail: str, *, ok: bool | None = None) -> None:
            self.calls.append({"name": name, "detail": detail, "ok": ok})

    gen = _Gen()
    monkeypatch.setattr(ctx.generator, "generate", gen.generate)
    monkeypatch.setattr(ctx.verifier.client, "chat_json", fake_chat_json)
    monkeypatch.setattr(VerifyStage, "_repo_context", lambda self, ws, cfg: "")
    ctx.job_sink = _Sink()

    result = await VerifyStage(ctx).run(item, ev, workspace=object())

    assert result.status is ItemStatus.NEEDS_MANUAL
    assert any("清单判定" in n for n in result.notes)
    tools = [t for t in ctx.job_sink.calls if t["name"] == "验证器"]
    assert len(tools) == 1
    assert tools[0]["detail"].startswith("清单 LLM 逐条判定")
    assert "未能复现" not in tools[0]["detail"]
    assert tools[0]["ok"] is None


# ---------------------------------------------------------------------------
# PR-FEATURE 必须验证（不能按 category 一刀切挡掉）
# ---------------------------------------------------------------------------


def _seed_pr(repo, number: int, *, category: Category = Category.FEATURE) -> RawItem:
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number,
        item_type=ItemType.PR, title="feat: 补充文档", body="见 files.py",
        head_branch="docs", base_branch="main",
    )
    repo.upsert_item(rid, item)
    repo.save_evaluation(
        item.key,
        Evaluation(category=category, priority=Priority.NONE,
                   scores=Scores(authenticity=DimensionScore(score=90)), model="stub"),
    )
    return item


async def test_process_repo_routes_feature_pr_to_pr_verify(ctx, repo, monkeypatch) -> None:
    """PR-FEATURE 必须走 PR 验证，不能被当作「FEATURE 不验证」跳过。

    回归（envkit #18 实测）：调用点用 ``category is not BUG`` 一刀切，
    把功能类 PR 与 Issue-FEATURE 一起挡掉，于是「只改文档却宣称实现了功能」
    这类 PR 停在 labeled、没有任何结论，fix_feature 队列一条都没有
    （DESIGN §2 明确要求 PR|FEATURE 入 fix_feature 队列）。
    """
    from fissue.pipeline.stages import BatchResult, Pipeline, StageResult

    _seed_pr(repo, 71, category=Category.FEATURE)
    calls = {"pr": 0}

    async def fake_eval(items, *, deep=False, concurrency=3):
        br = BatchResult(repo="psf/requests")
        for it in items:
            br.add(StageResult(key=it.key, category=Category.FEATURE,
                               evaluation=Evaluation(category=Category.FEATURE, model="stub")))
        return br

    async def fake_pr_run(item, evaluation=None, **kw):
        calls["pr"] += 1
        return StageResult(key=item.key, status=ItemStatus.QUEUED, queued=QueueName.FIX_FEATURE,
                           skipped_reason=None)

    pipe = Pipeline(ctx)
    monkeypatch.setattr(pipe, "evaluate_items", fake_eval)
    monkeypatch.setattr(pipe.pr_stage, "run", fake_pr_run)

    await pipe.process_repo("psf/requests", limit=10)

    assert calls["pr"] == 1, "PR-FEATURE 应进入 PR 验证流程"


async def test_process_repo_still_labels_feature_issue(ctx, repo, monkeypatch) -> None:
    """Issue-FEATURE 仍然只打标签（没有缺陷可复现，不进验证）。"""
    from fissue.pipeline.stages import BatchResult, Pipeline, StageResult

    issue = _seed_issue(repo, 72, category=Category.FEATURE)
    calls = {"pr": 0, "issue": 0}

    async def fake_eval(items, *, deep=False, concurrency=3):
        br = BatchResult(repo="psf/requests")
        for it in items:
            br.add(StageResult(key=it.key, category=Category.FEATURE,
                               evaluation=Evaluation(category=Category.FEATURE, model="stub")))
        return br

    async def fake_pr_run(item, evaluation=None, **kw):
        calls["pr"] += 1
        return StageResult(key=item.key)

    async def fake_issue_run(item, evaluation=None, **kw):
        calls["issue"] += 1
        return StageResult(key=item.key)

    pipe = Pipeline(ctx)
    monkeypatch.setattr(pipe, "evaluate_items", fake_eval)
    monkeypatch.setattr(pipe.pr_stage, "run", fake_pr_run)
    monkeypatch.setattr(pipe.verify_stage, "run", fake_issue_run)

    await pipe.process_repo("psf/requests", limit=10)

    assert calls == {"pr": 0, "issue": 0}          # 两种验证流程都没走
    assert repo.get_item_row(issue.key).status == ItemStatus.LABELED.value


# ---------------------------------------------------------------------------
# 只改文档的 PR：明确不推荐合并（不给含糊结论）
# ---------------------------------------------------------------------------


def test_is_doc_path_classification() -> None:
    from fissue.pipeline.stages import _is_doc_path

    assert _is_doc_path("README.md") is True
    assert _is_doc_path("docs/guide.md") is True
    assert _is_doc_path("CHANGELOG.md") is True
    # 代码/配置即便在 docs/ 下也不算文档
    assert _is_doc_path("docs/conf.py") is False
    assert _is_doc_path("envkit/files.py") is False
    assert _is_doc_path("pyproject.toml") is False


async def test_pr_verify_docs_only_pr_explicitly_rejects_merge(ctx, repo, monkeypatch) -> None:
    """只改文档、没有代码实现的 PR → 明确「不建议合并」，不跑沙盒、不给含糊结论。

    回归（envkit #18 实测）：该 PR 只改 README 却宣称提供 envkit/files.py，
    验证器必然 ModuleNotFoundError（与「验证器自己写错」无法区分），
    此前的结论含糊地停在「验证器自身有误」，没有明确的不合并建议。
    """
    from fissue.models import FileChange
    from fissue.pipeline.stages import PRVerifyStage

    item = _pr_item(73)
    item.files = [FileChange(path="README.md", status="modified", additions=21, deletions=0)]
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    async def boom(**kw):
        raise AssertionError("纯文档 PR 不该进沙盒验证")

    monkeypatch.setattr(ctx, "clone_workspace", boom)
    monkeypatch.setattr(ctx.verifier, "generate_and_validate", boom)

    ev = Evaluation(category=Category.FEATURE, model="stub")
    result = await PRVerifyStage(ctx).run(item, ev)

    assert result.status is ItemStatus.NEEDS_MANUAL
    assert "不建议合并" in (result.skipped_reason or "")
    assert "README.md" in (result.skipped_reason or "")


async def test_pr_verify_marks_merged_and_closed_prs_skipped(ctx, repo) -> None:
    """已合并 / 已关闭的 PR 必须落 SKIPPED，不能再永久停在 EVALUATED。

    回归：这两个分支原先只写 `skipped_reason` 就返回。daemon 每轮按 `status=EVALUATED` +
    `verify_batch_limit`（默认 30）取件，于是这类条目一旦累积到 30，**真正待验证的条目就永远
    进不了这一批**。但只在「还没往后走过」时才落 SKIPPED——已经 verified/labeled 的不该被打回。
    """
    from fissue.pipeline.stages import PRVerifyStage

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))

    def _pr(number: int, **kw: Any):
        it = RawItem(
            platform=Platform.GITHUB, repo="psf/requests", number=number,
            item_type=ItemType.PR, title=f"pr {number}", body="b", **kw,
        )
        repo.upsert_item(rid, it)
        repo.set_item_status(it.key, ItemStatus.EVALUATED)
        return it

    merged = _pr(101, merged=True)
    closed = _pr(102, state="closed")
    already = _pr(103, merged=True)
    repo.set_item_status(already.key, ItemStatus.LABELED)      # 已推进过的不能被打回

    ev = Evaluation(category=Category.BUG, model="stub")
    r1 = await PRVerifyStage(ctx).run(merged, ev)
    r2 = await PRVerifyStage(ctx).run(closed, ev)

    assert r1.status is ItemStatus.SKIPPED and "已合并" in (r1.skipped_reason or "")
    assert r2.status is ItemStatus.SKIPPED and "已关闭" in (r2.skipped_reason or "")
    assert repo.get_item_row(merged.key).status == ItemStatus.SKIPPED.value, "必须落状态"
    assert repo.get_item_row(closed.key).status == ItemStatus.SKIPPED.value

    await PRVerifyStage(ctx).run(already, ev)
    assert repo.get_item_row(already.key).status == ItemStatus.LABELED.value, (
        "别把已经打标/验证过的条目打回 SKIPPED"
    )


async def test_verify_stage_reraises_transient_failure_and_keeps_the_item_retryable(
    ctx, repo, monkeypatch
) -> None:
    """验证阶段的瞬时故障必须**上抛**，条目留在可重试状态，而不是当场 FAILED。

    回归：`run()` 原先自己 catch-all 并置 `FAILED`，而常驻服务那侧本来就写了带退避的重试
    （`daemon._verify_one`）——它**只认「run 抛异常」**。这里一吞，那次重试永远不会发生：
    克隆/网络的一次抖动就把条目永久钉死（daemon 只取 EVALUATED），只能人工
    `fissue requeue --failed` 救回来。
    """
    from fissue.pipeline.stages import VerifyStage

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=901,
        item_type=ItemType.ISSUE, title="t", body="b",
    )
    repo.upsert_item(rid, item)
    repo.set_item_status(item.key, ItemStatus.EVALUATED)
    ev = Evaluation(category=Category.BUG, priority=Priority.TIER1, model="stub")

    async def boom(*a, **kw):
        raise RuntimeError("克隆炸了")

    monkeypatch.setattr(ctx, "clone_workspace", boom)

    with pytest.raises(RuntimeError, match="克隆炸了"):
        await VerifyStage(ctx).run(item, ev)

    assert repo.get_item_row(item.key).status == ItemStatus.EVALUATED.value, (
        "瞬时故障不该把条目钉成 FAILED——它还得能被重试"
    )


async def test_pr_verify_code_pr_is_not_short_circuited(ctx, repo, monkeypatch) -> None:
    """含代码改动的 PR 不能被文档闸门误拦——必须照常走验证。"""
    from fissue.models import FileChange
    from fissue.pipeline.stages import PRVerifyStage

    item = _pr_item(74)
    item.files = [FileChange(path="envkit/units.py", status="modified", additions=5, deletions=2)]
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, item)

    reached = {"n": 0}

    async def spy_clone(repo_cfg, **kw):
        reached["n"] += 1
        raise RuntimeError("到这里说明闸门放行了（本用例只验证放行）")

    monkeypatch.setattr(ctx, "clone_workspace", spy_clone)

    ev = Evaluation(category=Category.BUG, model="stub")
    # 验证阶段把瞬时故障**上抛**（见 VerifyStage）：这里正好用它证明闸门确实放行了
    with pytest.raises(RuntimeError, match="闸门放行"):
        await PRVerifyStage(ctx).run(item, ev)

    assert reached["n"] == 1, "含代码改动的 PR 应继续走验证"


# ---------------------------------------------------------------------------
# #12 的端到端验证：真沙盒里「自错识别 → 重生成 → 不谎报复现」
# ---------------------------------------------------------------------------


async def test_e2e_self_error_detected_then_regenerates_in_real_sandbox(ctx, repo, tmp_path) -> None:
    """真沙盒端到端：首轮验证器写了不存在的 API → 识别为自错 → 重生成 → 真复现。

    静态单测只能证明判定函数本身对；这里证明整条链路成立：
    真 pytest 输出 → 定位行解析 → 识别为验证器自错 → 带反馈重生成 → 拿到真复现，
    且**不把自错那轮谎报成「问题已复现」**（envkit #12 实测的失效模式）。
    """
    import sys

    from fissue.ai.client import Usage
    from fissue.models import VerifierKind, VerifierSpec
    from fissue.verifier.generator import GeneratedVerifier
    from fissue.workspace import RepoWorkspace

    item = _seed_issue(repo, 91)

    # 真工作区：放一个有真实缺陷的库（丢负号）
    root = tmp_path / "ws"
    root.mkdir()
    (root / "mylib.py").write_text(
        "def parse_offset(text):\n"
        "    body = text.lstrip('+-')\n"
        "    h, m = body.split(':')\n"
        "    return int(h) * 60 + int(m)\n",
        encoding="utf-8",
    )

    cmd = f'"{sys.executable}" -m pytest tests/test_repro.py -q'
    # 首轮：调用了不存在的方法 put() → AttributeError 落在验证器自己的文件里
    broken = VerifierSpec(
        kind=VerifierKind.EXECUTABLE, name="broken",
        files={"tests/test_repro.py": (
            "from mylib import parse_offset\n\n"
            "def test_calls_missing_api():\n"
            "    parse_offset.put('k', 'v')\n"
        )},
        command=cmd,
    )
    # 重生成后：正经断言到缺陷行为 → 真复现
    good = VerifierSpec(
        kind=VerifierKind.EXECUTABLE, name="good",
        files={"tests/test_repro.py": (
            "from mylib import parse_offset\n\n"
            "def test_sign():\n"
            "    assert parse_offset('-05:30') == -330\n"
        )},
        command=cmd,
    )

    calls: dict[str, Any] = {"generate": 0, "refine": 0, "feedback": ""}

    class _Gen:
        async def generate(self, item, **kw):
            calls["generate"] += 1
            return GeneratedVerifier(spec=broken, usage=Usage(), warnings=[])

        async def refine(self, spec, *, failure_output="", **kw):
            calls["refine"] += 1
            calls["feedback"] = failure_output
            return GeneratedVerifier(spec=good, usage=Usage(), warnings=[])

    ws = RepoWorkspace(root=root, slug="psf/requests")
    _vid, spec, result = await ctx.verifier.generate_and_validate(
        item=item, workspace=ws, repo_context="", generator=_Gen()
    )

    assert calls["generate"] == 1
    assert calls["refine"] == 1, "首轮自错必须触发重生成"
    assert "验证器" in calls["feedback"], "反馈要点名是验证器自身的问题"
    assert spec.name == "good"
    # 真复现：次轮是真断言失败，而不是把自错当成复现
    assert result.reproducible is True
    assert "验证器自身" not in result.conclusion


# ---------------------------------------------------------------------------
# flush 打标签的并发化
# ---------------------------------------------------------------------------


async def test_label_all_runs_concurrently_and_keeps_order(ctx, repo, monkeypatch) -> None:
    """打标签并发跑：墙钟远小于串行，且返回值与输入同序。

    串行时 N 条标签请求的耗时直接相加（实测 10 条 39s），并发能近似压到一份。
    """
    processor = FlushProcessor(ctx)
    seen: list[int] = []

    async def slow_label(payload, labels):
        seen.append(payload["number"])
        await asyncio.sleep(0.05)
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", slow_label)
    items = [({"key": f"k{n}", "number": n}, ["bug"]) for n in range(1, 9)]

    start = asyncio.get_running_loop().time()
    results = await processor._label_all(items)
    elapsed = asyncio.get_running_loop().time() - start

    assert results == [(True, "")] * 8
    # 8 条 × 0.05s：并发（≥2 路）必然远快于串行的 0.4s
    assert elapsed < 0.3, f"未并发（耗时 {elapsed:.3f}s）"
    assert sorted(seen) == list(range(1, 9))          # 每条都被处理


async def test_label_all_preserves_input_order(ctx, repo, monkeypatch) -> None:
    """并发完成顺序不确定，但结果必须按输入顺序回填（报告顺序才稳定）。"""
    processor = FlushProcessor(ctx)

    async def uneven(payload, labels):
        # 让先发起的更慢，诱导乱序完成
        await asyncio.sleep(0.02 * (10 - payload["number"]))
        return payload["number"] != 3, f"err{payload['number']}"

    monkeypatch.setattr(processor, "_safe_label", uneven)
    items = [({"key": f"k{n}", "number": n}, ["bug"]) for n in range(1, 6)]

    results = await processor._label_all(items)
    assert [ok for ok, _ in results] == [True, True, False, True, True]
    assert results[2] == (False, "err3")


async def test_apply_labels_every_entry_with_shared_labels(ctx, repo, monkeypatch) -> None:
    """apply 把每条结论的标签都交给打标签流程（并发路径不丢条目）。"""
    processor = FlushProcessor(ctx)
    got: dict[str, list[str]] = {}

    async def record(payload, labels):
        got[payload["key"]] = list(labels)
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", record)
    a = _seed_issue(repo, 21)
    b = _seed_issue(repo, 22)
    entries = [
        {"key": a.key, "repo": a.repo, "number": a.number, "item_type": "issue"},
        {"key": b.key, "repo": b.repo, "number": b.number, "item_type": "issue"},
    ]
    conclusions = [
        {"key": a.key, "verdict": "fix", "labels": ["bug"], "priority": Priority.TIER1,
         "reason": "r", "confidence": 0.9},
        {"key": b.key, "verdict": "needs_review", "labels": [], "priority": Priority.NONE,
         "reason": "r", "confidence": 0.5},
    ]
    report = await processor.apply(QueueName.VERIFY.value, entries, conclusions)

    assert set(got) == {a.key, b.key}
    # 状态标签由我方规则补上，并统一带来源标记
    assert "reproduced" in got[a.key] and "fissue" in got[a.key]
    assert "needs-review" in got[b.key]
    assert sorted(report.labeled) == sorted([a.key, b.key])
    assert not report.label_errors


# ---------------------------------------------------------------------------
# 打标签前的标签列表对齐（TTL）
#
# 缓存带 TTL 之后，打标签前会把过期/缺失的仓库标签列表对齐一次：否则平台侧
# 新建的标签会被缓存一直判为「不存在」，导致反复创建并吃 422（该异常由适配器
# 直接抛出，整条打标签失败）。对齐只应发生在缓存过期或缺失时。
# ---------------------------------------------------------------------------


def _install_fake_adapter(ctx, *, label_ttl: float):
    """造一个不触网的适配器：拉标签列表换成计数桩。"""
    from fissue.platforms.registry import build_adapter

    ctx.settings.platforms.label_cache_ttl_seconds = label_ttl
    adapter = build_adapter(ctx.settings, platform=Platform.GITHUB, token="t")
    calls = {"n": 0}

    async def fake_list(_repo):
        calls["n"] += 1
        return ["bug"]

    # 给实例方法打桩：mypy 不喜欢「给方法赋值」，而这里正是它的用武之地
    adapter.fetch_repo_labels = fake_list  # type: ignore[method-assign]
    return adapter, calls


REPO_REF = RepoRef(platform=Platform.GITHUB, owner="psf", name="requests")


async def test_refresh_labels_fetches_when_cache_cold(ctx) -> None:
    """冷缓存：对齐真的拉一次远端标签列表，并把写入时刻推到现在。"""
    processor = FlushProcessor(ctx)
    adapter, calls = _install_fake_adapter(ctx, label_ttl=600.0)

    assert adapter.label_cache_age(REPO_REF) is None
    assert await processor._refresh_labels(adapter, REPO_REF, key="psf/requests#1") is True

    assert calls["n"] == 1
    assert adapter.label_cache_age(REPO_REF) is not None


async def test_refresh_labels_skips_when_cache_fresh(ctx) -> None:
    """缓存新鲜：对齐**不发任何请求**（否则 TTL 反而增加了流量）。"""
    processor = FlushProcessor(ctx)
    adapter, calls = _install_fake_adapter(ctx, label_ttl=600.0)
    await adapter.cached_repo_labels(REPO_REF)     # 预热：模拟上一轮 flush 刚对齐过
    assert calls["n"] == 1

    assert await processor._refresh_labels(adapter, REPO_REF) is False
    assert calls["n"] == 1                         # 未新增拉取


async def test_refresh_labels_refetches_after_ttl(ctx, monkeypatch) -> None:
    """缓存过期：对齐重新拉取（常驻服务跨轮次才会看到平台侧的新标签）。"""
    import fissue.platforms.base as base_mod

    clock = {"now": 0.0}
    monkeypatch.setattr(base_mod.time, "monotonic", lambda: clock["now"])
    processor = FlushProcessor(ctx)
    adapter, calls = _install_fake_adapter(ctx, label_ttl=100.0)

    await processor._refresh_labels(adapter, REPO_REF)
    assert calls["n"] == 1

    clock["now"] += 101.0
    await processor._refresh_labels(adapter, REPO_REF)
    assert calls["n"] == 2


async def test_refresh_labels_skips_when_cache_disabled(ctx) -> None:
    """TTL=0（关缓存）：不对齐——每次 ensure 都会实拉，再对齐纯属浪费。"""
    processor = FlushProcessor(ctx)
    adapter, calls = _install_fake_adapter(ctx, label_ttl=0.0)

    assert await processor._refresh_labels(adapter, REPO_REF) is False
    assert calls["n"] == 0


async def test_refresh_labels_swallows_adapter_errors(ctx) -> None:
    """对齐失败不抛：它只是加固手段，不能让一次网络抖动废掉整条打标签。"""
    processor = FlushProcessor(ctx)
    adapter, _ = _install_fake_adapter(ctx, label_ttl=600.0)

    async def boom(_repo):
        raise RuntimeError("模拟标签列表拉取失败")

    adapter.fetch_repo_labels = boom
    assert await processor._refresh_labels(adapter, REPO_REF, key="psf/requests#9") is False


# -- 打标签并发 -------------------------------------------------------------


@respx.mock
async def test_safe_label_aligns_then_labels(ctx, repo) -> None:
    """端到端（HTTP 层）：_safe_label 先对齐标签列表，再 ensure + 打标签。"""
    from fissue.platforms.registry import build_adapter

    ctx.settings.platforms.label_cache_ttl_seconds = 600.0
    adapter = build_adapter(ctx.settings, platform=Platform.GITHUB, token="t")
    ctx._adapters["github:psf/requests"] = adapter

    listing = respx.get(f"{GH}/repos/psf/requests/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "bug"}])
    )
    create = respx.post(f"{GH}/repos/psf/requests/labels").mock(
        return_value=httpx.Response(201, json={})
    )
    add = respx.post(f"{GH}/repos/psf/requests/issues/7/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    processor = FlushProcessor(ctx)
    payload = {"key": "psf/requests#7", "repo": "psf/requests", "number": 7, "item_type": "issue"}

    assert await processor._safe_label(payload, ["bug", "fissue"]) == (True, "")
    assert listing.call_count == 1                 # 对齐这一次
    assert create.call_count == 1                  # 缺失的 fissue 建一次
    assert add.call_count == 1                     # 打标签一次
    assert adapter.label_cache_age(REPO_REF) is not None

    # 第二条：缓存新鲜 → 不再对齐，但打标签照常
    await processor._safe_label(payload, ["bug"])
    assert listing.call_count == 1
    assert add.call_count == 2


@respx.mock
async def test_safe_label_keeps_local_snapshot_in_sync(ctx, repo) -> None:
    """打标成功后必须把标签并回本地，否则下一次抓取会把自己刚做出的结论作废。

    回归（树莓派实测，2026-10-18）：`labels` 是实质字段（MATERIAL_FIELDS），而打标签
    只写平台、**不回写本地**。于是下一次抓取必然读到「平台 ≠ 库里」的标签集，判成内容
    实质变化 → 结论作废、置回 `new` 重评。条目 #1 因此 28 分钟内被作废两次（平台
    `updated_at` 恰好是 flush 打标那一秒），两天里 LLM 调用 94 → 101、$0.4171 → $0.4528。

    这一条走**生产路径**（respx 打到 HTTP 层，不 stub `_safe_label`），否则测不到写回。
    """
    from fissue.platforms.registry import build_adapter

    ctx.settings.platforms.label_cache_ttl_seconds = 600.0
    adapter = build_adapter(ctx.settings, platform=Platform.GITHUB, token="t")
    ctx._adapters["github:psf/requests"] = adapter

    respx.get(f"{GH}/repos/psf/requests/labels").mock(
        return_value=httpx.Response(
            200, json=[{"name": "bug"}, {"name": "fissue"}, {"name": "reproduced"}]
        )
    )
    added = respx.post(f"{GH}/repos/psf/requests/issues/7/labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    item = _seed_issue(repo, 7)          # 库里 labels=["bug"]，结论已落库（status=evaluated）
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    processor = FlushProcessor(ctx)
    payload = {"key": item.key, "repo": item.repo, "number": item.number, "item_type": "issue"}

    assert await processor._safe_label(payload, ["bug", "fissue", "reproduced"]) == (True, "")
    assert added.call_count == 1
    # 本地快照已经并上我们刚写下去的那两个标签
    assert sorted(repo.get_item_row(item.key).labels) == ["bug", "fissue", "reproduced"]

    # 下一次抓取：平台返回的正是「库里原有 + 我们写上去的」
    _, created, changed = repo.upsert_item(
        rid,
        RawItem(
            platform=Platform.GITHUB, repo="psf/requests", number=7, item_type=ItemType.ISSUE,
            title="崩溃 7", body="boom", labels=["bug", "fissue", "reproduced"],
        ),
    )
    assert created is False
    assert changed is False, (
        "指纹也要跟着回写，否则抓取仍会重报一次「更新」（并重写字段与评论表）"
    )

    row = repo.get_item_row(item.key)
    assert row.status != ItemStatus.NEW.value, (
        "自己打标不该把结论作废——否则每打一次标就白烧一轮 classify + evaluate + verify"
    )
    assert repo.get_item(item.key).stale_reasons == [], "也不该留下「结论已过期」的痕迹"


async def test_label_all_concurrency_follows_platform_setting(ctx, repo, monkeypatch) -> None:
    """打标签的并发上限跟随 platforms.label_concurrency（不再绑死 llm.concurrency）。"""
    processor = FlushProcessor(ctx)
    ctx.settings.platforms.label_concurrency = 1
    inflight = {"now": 0, "max": 0}

    async def counted(payload, labels):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.01)
        inflight["now"] -= 1
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", counted)
    items = [({"key": f"k{n}", "number": n}, ["bug"]) for n in range(4)]
    await processor._label_all(items)
    assert inflight["max"] == 1                # 上限 1 → 严格串行

    ctx.settings.platforms.label_concurrency = 4
    inflight["max"] = 0
    await processor._label_all(items)
    assert inflight["max"] > 1                 # 放开后确实并行


def test_num_key_handles_mixed_number_forms() -> None:
    """编号混合形态（Gitee 的编码 + 各平台的数字）比较时不能崩，且数字仍按数值排。

    直接比较是 ``TypeError``（Gitee 的 Issue 编号是 `IKKIOZ` 这种编码，与数字共存于
    同一仓库）；而一律当字符串比，又会把 ``"10"`` 排到 ``"9"`` 前面。
    """
    from fissue.pipeline.stages import _num_key

    assert _num_key(9) < _num_key(10)
    assert _num_key("9") < _num_key("10"), "数字形态的字符串也要按数值排"
    assert _num_key(10) < _num_key("IKKIOZ"), "编码型统一排在数字之后"
    assert _num_key("IKKIOZ") < _num_key("ILZZZZ")
    # 混在一起排序不抛异常（原先的 `>=` 直接比较会 TypeError）
    assert sorted([_num_key("IKKIOZ"), _num_key(3), _num_key("10")]) == [
        _num_key(3), _num_key("10"), _num_key("IKKIOZ")
    ]


# ---------------------------------------------------------------------------
# 已关闭条目：只更新，不新建
#
# 抓取问的是 `state=all`（默认值，见 platforms/base.py 的 `fetch_items`），所以历史上已关闭
# 的条目也会被返回。原先它们一律入库，而流水线要到**验证阶段**才跳过——那之前已经白花一次
# 评测。实测：一条已关闭 PR 花了 2185/2379 tokens、$0.0056，换来的只是一行「PR 已关闭」。
#
# 但**不能**改成只抓 open：条目在平台侧被关闭后，只有 state=all 仍返回它我们才知道；
# 漏掉的话本地状态永远停在 open，流水线会接着验证、打标签，甚至为一条早已关闭的 issue
# 自动修复提 PR，而且全程没有任何报错。
# ---------------------------------------------------------------------------


def _closed_item(number: int) -> RawItem:
    item = _fetched_item(number, "2026-01-09T00:00:00+00:00")
    item.state = "closed"
    return item


async def test_fetcher_skips_a_closed_item_it_never_tracked(settings, ctx, monkeypatch) -> None:
    """从没见过的已关闭条目**不建行**，但同一批里还开着的照常入库。"""
    cfg = _repo_cfg()
    fetcher = _wire_adapter(
        ctx,
        monkeypatch,
        _StubFetchAdapter([_closed_item(1), _fetched_item(2, "2026-01-09T00:00:00+00:00")]),
    )

    stats = await fetcher.fetch_repo(cfg)

    assert stats.created == 1, f"只该入库那条还开着的：{stats}"
    assert stats.skipped == 1, "已关闭且从未跟踪的那条应当计入过滤"
    assert ctx.repo.item_exists("github:psf/requests#1") is False
    assert ctx.repo.item_exists("github:psf/requests#2") is True


async def test_fetcher_still_updates_a_closed_item_it_already_tracks(
    settings, ctx, monkeypatch
) -> None:
    """**已经在跟踪的**条目即便关了也要更新进来——否则本地状态会永远停在 open。"""
    cfg = _repo_cfg()
    first = _wire_adapter(
        ctx, monkeypatch, _StubFetchAdapter([_fetched_item(1, "2026-01-01T00:00:00+00:00")])
    )
    await first.fetch_repo(cfg)
    item = ctx.repo.get_item("github:psf/requests#1")
    assert item is not None and item.state == "open"

    # 下一轮它在平台上关了：必须更新进来，而不是被当成「已关闭的噪声」丢掉
    second = _wire_adapter(ctx, monkeypatch, _StubFetchAdapter([_closed_item(1)]))
    stats = await second.fetch_repo(cfg)

    assert stats.updated == 1, f"已关闭这个事实没被更新：{stats}"
    updated = ctx.repo.get_item("github:psf/requests#1")
    assert updated is not None and updated.state == "closed"


def test_is_closed_covers_every_platform_wording() -> None:
    """`is_closed` 要覆盖四个平台的写法，且**只在这些写法上**成立。

    写成白名单（只认 open）会在遇到 Gitee 的 `progressing` 或 GitLab 原样的 `opened` 时，
    把还开着的条目当成已关闭丢掉——那等于我们永远不再处理它，比多抓一条已关闭的条目严重得多。
    """

    def pr(state: str, *, merged: bool = False) -> RawItem:
        return RawItem(
            platform=Platform.GITHUB, repo="a/b", number=1, item_type=ItemType.PR,
            title="t", state=state, merged=merged,
        )

    for closed in ("closed", "merged", "locked", "rejected", "CLOSED", " closed "):
        assert pr(closed).is_closed is True, closed
    for openish in ("open", "opened", "progressing", "reopened", ""):
        assert pr(openish).is_closed is False, openish
    # `merged` 标志比 state 更直接（GitHub 已合并 PR 的 state 也是 closed）
    assert pr("open", merged=True).is_closed is True


# ---------------------------------------------------------------------------
# 强制修复（force）：闸门旁路是**人的决定**优先于**模型的结论**
# ---------------------------------------------------------------------------


def _repro_result(key: str, *, reproducible: bool) -> VerifierResult:
    outcome = VerifierOutcome.FAIL if reproducible else VerifierOutcome.PASS
    return VerifierResult(
        kind=VerifierKind.EXECUTABLE,
        base_run=VerifierRun(item_key=key, stage="base", outcome=outcome, f2p_ok=reproducible),
        f2p_satisfied=reproducible,
        conclusion="可复现" if reproducible else "不可复现",
    )


def _stub_force_stage(ctx, monkeypatch, *, reproducible: bool = True):
    """搭一个不触网 / 不进沙盒的 VerifyStage：打桩工作区与验证器生成。

    返回 (stage, calls)：calls["generate"] 数了生成验证器的次数——
    「该不该顺手生成验证器」本身就是强制流程该被断言的行为。
    """
    calls: dict[str, int] = {"generate": 0}

    async def fake_clone(_cfg):
        return object()

    async def fake_generate(**kwargs):
        calls["generate"] += 1
        item = kwargs["item"]
        return (
            7,
            VerifierSpec(kind=VerifierKind.EXECUTABLE, command="pytest"),
            _repro_result(item.key, reproducible=reproducible),
        )

    monkeypatch.setattr(ctx, "clone_workspace", fake_clone)
    monkeypatch.setattr(ctx.verifier, "generate_and_validate", fake_generate)
    monkeypatch.setattr(ctx.settings.verifier, "regression_gate", "off")
    monkeypatch.setattr(stage := VerifyStage(ctx), "_repo_context", lambda _ws, _cfg: "")
    return stage, calls


async def test_verify_stage_force_feature_goes_to_fix_queued(ctx, repo, monkeypatch) -> None:
    """强制修复 FEATURE 请求：分类闸门不再拦，验证器补完后直落待修 tier1。"""
    item = _seed_issue(repo, 51, category=Category.FEATURE)
    repo.set_force_fix(item.key, True)
    stage, calls = _stub_force_stage(ctx, monkeypatch)

    r = await stage.run(repo.get_item(item.key), repo.latest_evaluation(item.key))

    assert calls["generate"] == 1, "必须顺手生成验证器——修复器靠它判定修复"
    fresh = repo.get_item(item.key)
    assert fresh is not None
    assert fresh.status is ItemStatus.FIX_QUEUED
    assert fresh.priority is Priority.TIER1
    assert r.queued is None, "强制条目直落待修，不登记定论队列"


async def test_verify_stage_force_unreproducible_still_fix_queued(ctx, repo, monkeypatch) -> None:
    """强制修复：复现不了也照修（NEEDS_MANUAL 只留给非强制条目）。"""
    item = _seed_issue(repo, 52)
    repo.set_force_fix(item.key, True)
    stage, _calls = _stub_force_stage(ctx, monkeypatch, reproducible=False)

    await stage.run(repo.get_item(item.key), repo.latest_evaluation(item.key))

    fresh = repo.get_item(item.key)
    assert fresh is not None
    assert fresh.status is ItemStatus.FIX_QUEUED, f"强制条目不该停在需人工：{fresh.status}"


async def test_verify_stage_force_in_fix_flow_is_noop(ctx, repo, monkeypatch) -> None:
    """强制修复 + 已在修复流程中（fix_queued / fixing / pr_created）：不重验、不动状态。

    尤其 pr_created：把它拉回来重验会多开一个 PR。
    """
    item = _seed_issue(repo, 53)
    repo.set_force_fix(item.key, True)
    repo.set_item_status(item.key, ItemStatus.PR_CREATED)

    async def boom(**_kwargs):
        raise AssertionError("已在修复流程中的条目不该再进验证")

    monkeypatch.setattr(ctx.verifier, "generate_and_validate", boom)
    r = await VerifyStage(ctx).run(repo.get_item(item.key), repo.latest_evaluation(item.key))

    fresh = repo.get_item(item.key)
    assert fresh is not None
    assert fresh.status is ItemStatus.PR_CREATED
    assert r.skipped_reason and "保持状态" in r.skipped_reason


async def test_flush_apply_force_bypasses_skip_verdict(ctx, repo, monkeypatch) -> None:
    """强制修复：LLM 裁决 skip 也直落待修 tier1（人的决定优先于裁决）。

    覆盖「强制前已在验证队列」的残留条目：它不该被裁决判成 SKIPPED。
    """
    item = _seed_issue(repo, 54)
    repo.set_item_status(item.key, ItemStatus.QUEUED)
    repo.set_force_fix(item.key, True)
    processor = FlushProcessor(ctx)

    async def fake_label(_payload, _labels):
        return True, ""

    monkeypatch.setattr(processor, "_safe_label", fake_label)
    entries = [{"key": item.key, "item_type": "issue", "title": item.title, "labels": []}]
    conclusions = [{"key": item.key, "verdict": "skip", "priority": Priority.NONE}]

    report = await processor.apply(QueueName.VERIFY.value, entries, conclusions)

    fresh = repo.get_item(item.key)
    assert fresh is not None
    assert fresh.status is ItemStatus.FIX_QUEUED
    assert fresh.priority is Priority.TIER1
    assert (item.key, Priority.TIER1) in report.fix_candidates
