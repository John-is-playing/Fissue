"""流水线、队列与批量 flush 测试（打桩 LLM，不触网）。"""

from __future__ import annotations

import asyncio

import pytest

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
)
from fissue.pipeline.context import RuntimeContext
from fissue.pipeline.fetcher import Fetcher, FetchStats, _collect_types
from fissue.pipeline.flush import FlushProcessor
from fissue.pipeline.queue import QueueManager


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
    assert "LLM 挂了" in outcome.error
    # 条目仍留在未 flush 状态，等下一轮重试
    assert len(repo.queue_unflushed(QueueName.VERIFY)) == 1


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


async def test_flush_processor_label_failure_recorded(ctx, repo) -> None:
    processor = FlushProcessor(ctx)
    item = _seed_issue(repo, 8)
    entries = [{"key": item.key, "repo": item.repo, "number": item.number, "item_type": "issue"}]
    conclusions = [{"key": item.key, "verdict": "fix", "labels": ["bug"],
                    "priority": Priority.TIER2, "reason": "r", "confidence": 0.7}]
    report = await processor.apply(QueueName.VERIFY.value, entries, conclusions)
    # 无 token → 打标签必然失败，但流程不能中断，状态仍应推进
    assert report.label_errors
    assert repo.get_item_row(item.key).status == ItemStatus.FIX_QUEUED.value


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
    candidates = processor.fix_candidates()
    assert [c.number for c in candidates] == [2, 1]


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


async def test_require_repos_helper(ctx, settings) -> None:
    from fissue.pipeline.context import require_repos

    assert [r.slug for r in require_repos(ctx, None)] == ["psf/requests"]
    assert [r.slug for r in require_repos(ctx, ["psf/requests"])] == ["psf/requests"]
