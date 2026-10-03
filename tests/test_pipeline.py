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


def _pr_item(number: int = 43) -> RawItem:
    return RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number,
        item_type=ItemType.PR, title="fix: 修复超时处理", body="修复 #42",
        head_branch="fix-timeout", base_branch="main", linked_issues=[42],
    )


def _spec():
    from fissue.models import VerifierKind, VerifierSpec

    return VerifierSpec(
        kind=VerifierKind.EXECUTABLE,
        command="python -m pytest -q",
        files={"tests/test_repro.py": "def test_x():\n    assert False\n"},
    )


def _verifier_result(base_outcome, fix_outcome=None):
    from fissue.models import VerifierKind, VerifierRun, VerifierResult

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
        return 1, spec, base_result

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

    captured: dict[str, object] = {}

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


async def test_eval_batch_passes_similar_candidates(ctx, repo) -> None:
    from fissue.pipeline.stages import EvalStage

    seen: dict[str, object] = {}

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
