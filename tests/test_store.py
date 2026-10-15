"""存储层与导出测试（SQLite 内存/临时库，无需 PostgreSQL）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from fissue.models import (
    AuthorKind,
    BatchConclusion,
    Category,
    Comment,
    DimensionScore,
    Evaluation,
    FileChange,
    FixAttempt,
    FixOutcome,
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
    VerifierRun,
    VerifierSpec,
)
from fissue.store import export as exp
from fissue.store.repository import Repository


def _seed(repo: Repository, *, number: int = 1, item_type: ItemType = ItemType.ISSUE, labels=None) -> RawItem:
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    item = RawItem(
        platform=Platform.GITHUB,
        repo="psf/requests",
        number=number,
        item_type=item_type,
        title=f"条目 {number}",
        body="正文",
        labels=labels or ["bug"],
        author="u1",
        comments=[Comment(author="c1", body="评论")],
    )
    repo.upsert_item(rid, item)
    return item


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------


def test_upsert_is_idempotent_and_reports_changes(repo: Repository) -> None:
    item = _seed(repo)
    _id, created, changed = repo.upsert_item(repo.get_repo("psf/requests").id, item)
    assert (created, changed) == (False, False)

    item.title = "改了标题"
    _id2, created2, changed2 = repo.upsert_item(repo.get_repo("psf/requests").id, item)
    assert created2 is False and changed2 is True


def test_upsert_updates_status_fields_roundtrip(repo: Repository) -> None:
    _seed(repo, number=7)
    got = repo.get_item("github:psf/requests#7")
    assert got is not None
    assert got.category is Category.UNKNOWN
    assert got.status is ItemStatus.NEW
    assert got.priority is Priority.NONE
    assert got.comments and got.comments[0].author == "c1"


def test_list_items_filters(repo: Repository) -> None:
    _seed(repo, number=1)
    _seed(repo, number=2, item_type=ItemType.PR)
    assert len(repo.list_items(repo_slug="psf/requests")) == 2
    assert len(repo.list_items(item_type=ItemType.PR)) == 1
    assert len(repo.list_items(status=ItemStatus.NEW)) == 2
    assert repo.count_items(item_type=ItemType.ISSUE) == 1


def test_set_item_status_and_priority(repo: Repository) -> None:
    _seed(repo, number=3)
    repo.set_item_status("github:psf/requests#3", ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    row = repo.get_item_row("github:psf/requests#3")
    assert row.status == ItemStatus.FIX_QUEUED.value
    assert row.priority == Priority.TIER1.value


def test_items_needing_eval(repo: Repository) -> None:
    _seed(repo, number=1)
    _seed(repo, number=2)
    repo.set_item_status("github:psf/requests#2", ItemStatus.VERIFIED)
    keys = [i.key for i in repo.items_needing_eval()]
    assert keys == ["github:psf/requests#1"]


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------


def test_evaluation_roundtrip(repo: Repository, good_evaluation: Evaluation) -> None:
    _seed(repo, number=5)
    eid = repo.save_evaluation("github:psf/requests#5", good_evaluation)
    assert eid > 0
    got = repo.latest_evaluation("github:psf/requests#5")
    assert got is not None
    assert got.importance == 88
    assert got.difficulty == 25
    assert got.category is Category.BUG
    assert got.priority is Priority.TIER1
    assert got.scores.authenticity.reason == "有完整复现步骤"


def test_latest_evaluation_returns_newest(repo: Repository) -> None:
    _seed(repo, number=6)
    key = "github:psf/requests#6"
    repo.save_evaluation(key, Evaluation(scores=Scores(importance=DimensionScore(score=10)), model="a"))
    repo.save_evaluation(key, Evaluation(scores=Scores(importance=DimensionScore(score=99)), model="b"))
    assert repo.latest_evaluation(key).importance == 99


def test_evaluations_for_bulk(repo: Repository) -> None:
    _seed(repo, number=1)
    _seed(repo, number=2)
    repo.save_evaluation("github:psf/requests#1", Evaluation(scores=Scores(importance=DimensionScore(score=70))))
    repo.save_evaluation("github:psf/requests#2", Evaluation(scores=Scores(importance=DimensionScore(score=80))))
    out = repo.evaluations_for(["github:psf/requests#1", "github:psf/requests#2"])
    assert out["github:psf/requests#1"].importance == 70
    assert out["github:psf/requests#2"].importance == 80


def test_save_evaluation_unknown_item_raises(repo: Repository) -> None:
    with pytest.raises(ValueError):
        repo.save_evaluation("github:psf/requests#999", Evaluation())


def test_reeval_downgrade_clears_fix_queued(repo: Repository) -> None:
    """重评后优先级降为 none，停留在 fix_queued 的条目应收敛为 needs_manual。

    回归：#5 首次 flush 时被置 fix_queued，之后重评判定为疑似重复（priority=none），
    但 save_evaluation 只处理 NEW→EVALUATED，于是状态停在 fix_queued，
    与 priority=none 自相矛盾（报告/看板显示可疑）。
    """
    _seed(repo, number=31)
    key = "github:psf/requests#31"
    repo.set_item_status(key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)

    repo.save_evaluation(key, Evaluation(priority=Priority.NONE, model="stub"))
    row = repo.get_item_row(key)
    assert row.status == ItemStatus.NEEDS_MANUAL.value
    assert row.priority == Priority.NONE.value


def test_reeval_keeps_qualified_fix_queued(repo: Repository) -> None:
    """仍够格自动修复时，fix_queued 保持不动。"""
    _seed(repo, number=32)
    key = "github/psf/requests#32".replace("github/", "github:")   # github:psf/requests#32
    repo.set_item_status(key, ItemStatus.FIX_QUEUED, priority=Priority.TIER2)
    repo.save_evaluation(key, Evaluation(priority=Priority.TIER2, model="stub"))
    assert repo.get_item_row(key).status == ItemStatus.FIX_QUEUED.value


def test_reeval_does_not_rewind_in_progress_fix(repo: Repository) -> None:
    """已开工的修复不回退：fixing / pr_created 的优先级变化不动其状态。"""
    _seed(repo, number=33)
    key = "github:psf/requests#33"
    for status in (ItemStatus.FIXING, ItemStatus.PR_CREATED):
        repo.set_item_status(key, status, priority=Priority.TIER1)
        repo.save_evaluation(key, Evaluation(priority=Priority.NONE, model="stub"))
        assert repo.get_item_row(key).status == status.value


# ---------------------------------------------------------------------------
# 验证器
# ---------------------------------------------------------------------------


def test_verifier_and_runs_roundtrip(repo: Repository) -> None:
    _seed(repo, number=8)
    key = "github:psf/requests#8"
    spec = VerifierSpec(
        kind=VerifierKind.EXECUTABLE,
        name="repro",
        files={"tests/test_repro.py": "def test_x(): assert False\n"},
        command="python -m pytest tests/test_repro.py -q",
        notes="复现空指针",
    )
    vid = repo.save_verifier(key, spec, rounds=2)
    got = repo.latest_verifier(key)
    assert got is not None
    verifier_id, saved = got
    assert verifier_id == vid
    assert saved.command.startswith("python -m pytest")
    assert list(saved.files) == ["tests/test_repro.py"]

    repo.set_verifier_f2p(vid, True)
    repo.save_verifier_run(
        VerifierRun(
            verifier_id=vid,
            item_key=key,
            stage="base",
            outcome=VerifierOutcome.FAIL,
            exit_code=1,
            stdout="failed",
            f2p_ok=False,
        )
    )
    runs = repo.verifier_runs(key)
    assert len(runs) == 1
    assert runs[0].outcome is VerifierOutcome.FAIL
    assert runs[0].exit_code == 1


def test_verifier_files_should_be_relative() -> None:
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, files={"tests/t.py": "x"}, command="pytest")
    assert all(not p.startswith("/") for p in spec.files)


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------


def test_enqueue_dedup_and_claim(repo: Repository) -> None:
    _seed(repo, number=9)
    key = "github:psf/requests#9"
    a = repo.enqueue(QueueName.VERIFY, key, payload={"x": 1})
    b = repo.enqueue(QueueName.VERIFY, key, payload={"x": 2})
    assert a == b                                     # 同队列同条目 pending 去重
    assert repo.queue_pending_count(QueueName.VERIFY) == 1

    row = repo.claim_next(QueueName.VERIFY)
    assert row is not None and row.status == "running" and row.attempts == 1
    assert repo.claim_next(QueueName.VERIFY) is None    # 已被取走

    repo.finish_queue_entry(row.id, status="done", result={"ok": True})
    assert repo.queue_pending_count(QueueName.VERIFY) == 0
    assert len(repo.queue_unflushed(QueueName.VERIFY)) == 1


def test_enqueue_reuse_after_done_does_not_crash(repo: Repository) -> None:
    """同一队列同一条目在终态后重新入队，必须复用原行而不是新插一行。

    回归：唯一约束 (queue,item_key,status) 把终态也算进去，若重跑时新插一行
    pending，等它转 done 就会撞上上一轮遗留的 done，抛
    ``UNIQUE constraint failed: queue_entries.queue, queue_entries.item_key, queue_entries.status``
    ——表现为「重跑验证必崩」。
    """
    _seed(repo, number=21)
    key = "github:psf/requests#21"

    first = repo.enqueue(QueueName.FIX_BUG, key, payload={"run": 1})
    row = repo.claim_next(QueueName.FIX_BUG)
    repo.finish_queue_entry(row.id, status="done", result={"merge_recommended": False})

    # 重跑：同一条目再次入队
    second = repo.enqueue(QueueName.FIX_BUG, key, payload={"run": 2})
    assert second == first                        # 复用同一行
    row2 = repo.claim_next(QueueName.FIX_BUG)
    assert row2 is not None
    repo.finish_queue_entry(row2.id, status="done", result={"merge_recommended": True})

    # 每一轮都能正常收尾；队列里只有这一行
    rows = repo.queue_unflushed(QueueName.FIX_BUG)
    assert len(rows) == 1
    assert rows[0].result["merge_recommended"] is True


def test_enqueue_collapses_legacy_done_plus_pending(repo: Repository) -> None:
    """存量脏数据（同一队列同一条目的 done + pending 并存）在再次入队时收敛为一行。

    这正是旧 ``enqueue`` 只复用 pending/running 时留下的现场：上一轮的 ``done``
    还留着，重跑又插了一行 ``pending``。注意两行 status 不同，所以唯一约束**允许**
    它们共存——也正因如此，那一行 pending 一旦转 done 就会撞上旧的 done。
    """
    from fissue.store.tables import QueueEntryRow

    _seed(repo, number=22)
    key = "github:psf/requests#22"

    with repo.db.session() as s:
        s.add(QueueEntryRow(queue=QueueName.VERIFY.value, item_key=key,
                            status="done", payload={}, result={"round": 1}))
    with repo.db.session() as s:
        s.add(QueueEntryRow(queue=QueueName.VERIFY.value, item_key=key,
                            status="pending", payload={}, result={}))

    entry_id = repo.enqueue(QueueName.VERIFY, key, payload={"round": 2})
    with repo.db.session() as s:
        rows = list(s.scalars(
            select(QueueEntryRow).where(QueueEntryRow.item_key == key).order_by(QueueEntryRow.id)
        ))
    assert len(rows) == 1                         # done + pending 被收敛为一行
    assert rows[0].id == entry_id                 # 复用最早那行
    assert rows[0].status == "pending"            # 且重置为待处理
    assert rows[0].payload == {"round": 2}
    assert rows[0].result == {}                   # 旧结论清空，避免与新结论混淆


def test_claim_respects_max_attempts(repo: Repository) -> None:
    """达到最大尝试次数的条目不再被领取。"""
    _seed(repo, number=10)
    key = "github:psf/requests#10"
    repo.enqueue(QueueName.FIX_BUG, key)

    for expected_attempt in (1, 2):
        row = repo.claim_next(QueueName.FIX_BUG, max_attempts=2)
        assert row is not None
        assert row.attempts == expected_attempt
        # 模拟「失败后重新排入待处理」：条目可再次被领取，直到耗尽尝试次数
        repo.finish_queue_entry(row.id, status="pending", error="boom")

    assert repo.claim_next(QueueName.FIX_BUG, max_attempts=2) is None


def test_queue_independence(repo: Repository) -> None:
    _seed(repo, number=11)
    _seed(repo, number=12)
    repo.enqueue(QueueName.FIX_BUG, "github:psf/requests#11")
    repo.enqueue(QueueName.FIX_FEATURE, "github:psf/requests#12")
    assert repo.queue_pending_count(QueueName.FIX_BUG) == 1
    assert repo.queue_pending_count(QueueName.FIX_FEATURE) == 1
    stats = repo.queue_stats()
    assert stats["fix_bug"]["pending"] == 1
    assert stats["fix_feature"]["pending"] == 1


def test_unflushed_age_and_mark_flushed(repo: Repository) -> None:
    _seed(repo, number=13)
    key = "github:psf/requests#13"
    repo.enqueue(QueueName.VERIFY, key)
    row = repo.claim_next(QueueName.VERIFY)
    repo.finish_queue_entry(row.id, status="done", result={})
    age = repo.oldest_unflushed_age(QueueName.VERIFY)
    assert age is not None and age < 60

    repo.mark_flushed([row.id])
    assert repo.queue_unflushed(QueueName.VERIFY) == []
    assert repo.oldest_unflushed_age(QueueName.VERIFY) is None


def test_batch_conclusion_roundtrip(repo: Repository) -> None:
    _seed(repo, number=14)
    key = "github:psf/requests#14"
    repo.save_batch_conclusion(
        key,
        "batch-1",
        verdict="fix",
        labels=["reproduced"],
        priority=Priority.TIER1,
        reason="低难度高重要性",
        confidence=0.9,
        model="test-model",
    )
    c = repo.latest_batch_conclusion(key)
    assert c is not None
    assert c.verdict == "fix"
    assert c.priority is Priority.TIER1
    assert repo.get_item_row(key).status == ItemStatus.LABELED.value


# ---------------------------------------------------------------------------
# 修复尝试 / 用量
# ---------------------------------------------------------------------------


def test_fix_attempt_roundtrip_sets_status(repo: Repository) -> None:
    _seed(repo, number=15)
    key = "github:psf/requests#15"
    repo.save_fix_attempt(
        FixAttempt(
            item_key=key,
            outcome=FixOutcome.SUCCESS,
            branch="fissue/fix-15",
            pr_number=100,
            pr_url="https://github.com/psf/requests/pull/100",
            changed_files=["app.py"],
            rounds=3,
        )
    )
    attempts = repo.fix_attempts(key)
    assert len(attempts) == 1
    assert attempts[0].outcome is FixOutcome.SUCCESS
    assert attempts[0].pr_number == 100
    assert repo.get_item_row(key).status == ItemStatus.PR_CREATED.value


def test_reclaim_fixing_items_returns_stuck_entries_to_queue(repo: Repository) -> None:
    """被中断（Ctrl-C / 崩溃）留在 fixing 的条目必须能被放回待修队列。

    回归：demo 实测 pipekit #12 —— 进程被 Ctrl-C 打断后条目永久停在 fixing，
    而 fix_candidates 只取 fix_queued，于是它既修不到、也不再出现在候选里。
    """
    _seed(repo, number=20)
    _seed(repo, number=21)
    _seed(repo, number=22)
    for n in (20, 21):
        repo.set_item_status(f"github:psf/requests#{n}", ItemStatus.FIXING)

    reclaimed = repo.reclaim_fixing_items(repo_slug="psf/requests")

    assert sorted(reclaimed) == ["github:psf/requests#20", "github:psf/requests#21"]
    assert repo.get_item_row("github:psf/requests#20").status == ItemStatus.FIX_QUEUED.value
    assert repo.get_item_row("github:psf/requests#21").status == ItemStatus.FIX_QUEUED.value
    # 正常情况下（没卡住的条目）不该被动
    assert repo.get_item_row("github:psf/requests#22").status == ItemStatus.NEW.value
    # 幂等：再调一次没有可认领的
    assert repo.reclaim_fixing_items(repo_slug="psf/requests") == []


def test_reclaim_fixing_items_respects_repo_scope(repo: Repository) -> None:
    """限定仓库时不碰别家仓库的 fixing 条目。"""
    _seed(repo, number=30)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="urllib3"))
    other = RawItem(
        platform=Platform.GITHUB, repo="psf/urllib3", number=1, item_type=ItemType.ISSUE,
        title="别家", body="x", labels=["bug"], author="u",
    )
    repo.upsert_item(rid, other)
    repo.set_item_status("github:psf/requests#30", ItemStatus.FIXING)
    repo.set_item_status("github:psf/urllib3#1", ItemStatus.FIXING)

    reclaimed = repo.reclaim_fixing_items(repo_slug="psf/requests")

    assert reclaimed == ["github:psf/requests#30"]
    assert repo.get_item_row("github:psf/urllib3#1").status == ItemStatus.FIXING.value


def test_usage_records_and_today(repo: Repository) -> None:
    repo.record_usage(repo_key="psf/requests", purpose="evaluate", model="m",
                      prompt_tokens=1000, completion_tokens=500, cost_usd=0.01)
    repo.record_usage(repo_key="psf/requests", purpose="verifier", model="m",
                      prompt_tokens=200, completion_tokens=100, cost_usd=0.002)
    used = repo.usage_today()
    assert used["total_tokens"] == 1800
    assert used["calls"] == 2
    assert used["cost_usd"] == pytest.approx(0.012)

    scoped = repo.usage_today(repo_key="psf/requests")
    assert scoped["total_tokens"] == 1800
    assert repo.usage_today(repo_key="other/repo")["calls"] == 0


def test_scan_run_lifecycle(repo: Repository) -> None:
    sid = repo.start_scan("github:psf/requests")
    repo.finish_scan(sid, fetched=10, created=3, updated=2, unchanged=5)


def test_repo_cursor_merge(repo: Repository) -> None:
    repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.set_repo_cursor("psf/requests", {"last_updated_at": "2026-01-01T00:00:00+00:00"})
    repo.set_repo_cursor("psf/requests", {"item_types": ["issue"]})
    cursor = repo.get_repo_cursor("psf/requests")
    assert cursor["last_updated_at"].startswith("2026-01-01")
    assert cursor["item_types"] == ["issue"]


def test_notification_dedup(repo: Repository) -> None:
    assert repo.try_claim_notification("k1", "evt", "item", "webhook", {}) is True
    assert repo.try_claim_notification("k1", "evt", "item", "webhook", {}) is False
    assert repo.try_claim_notification("k2", "evt", "item", "webhook", {}) is True


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def test_export_json_shape(repo: Repository, good_evaluation: Evaluation) -> None:
    item = _seed(repo, number=20)
    repo.save_evaluation(item.key, good_evaluation)
    payload = json.loads(exp.export_json(repo, repo_slug="psf/requests"))
    assert payload["count"] == 1
    row = payload["items"][0]
    assert row["key"] == item.key
    assert row["evaluation"]["scores"]["importance"] == 88
    assert row["evaluation"]["priority"] == "tier1"
    assert row["url"].endswith("/issues/20")
    assert payload["summary"]["by_category"]["bug"] == 1


def test_export_markdown_contains_sections(repo: Repository, good_evaluation: Evaluation) -> None:
    item = _seed(repo, number=21)
    repo.save_evaluation(item.key, good_evaluation)
    md = exp.export_markdown(repo, repo_slug="psf/requests")
    assert "# Fissue 评测报告" in md
    assert "## 概览" in md and "## 明细" in md
    assert "重要性" in md
    assert exp.item_url(item).endswith("/issues/21")


def test_item_url_variants() -> None:
    issue = RawItem(platform=Platform.GITEE, repo="a/b", number=1, item_type=ItemType.ISSUE, title="t")
    pr = RawItem(platform=Platform.GITLAB, repo="a/b", number=2, item_type=ItemType.PR, title="t")
    assert exp.item_url(issue) == "https://gitee.com/a/b/issues/1"
    assert exp.item_url(pr) == "https://gitlab.com/a/b/-/merge_requests/2"
    atom = RawItem(platform=Platform.ATOMGIT, repo="a/b", number=3, item_type=ItemType.PR, title="t")
    assert exp.item_url(atom) == "https://atomgit.com/a/b/pulls/3"


def test_write_export_creates_dirs(tmp_path) -> None:
    path = exp.write_export("hello", tmp_path / "deep" / "nested" / "out.md")
    assert path.read_text(encoding="utf-8") == "hello"


def test_summarize_averages(repo: Repository) -> None:
    for n in (1, 2):
        item = _seed(repo, number=n)
        repo.save_evaluation(
            item.key, Evaluation(scores=Scores(importance=DimensionScore(score=60 + n * 10)))
        )
    stats = exp.summarize(repo)
    assert stats["total"] == 2
    assert stats["avg_scores"]["importance"] == pytest.approx(75.0)   # (70 + 80) / 2


def test_summarize_applies_the_same_filters_as_detail(repo: Repository) -> None:
    """概览必须套用与明细**相同**的过滤条件。

    回归：`summarize` 原先只接 `repo_slug`，于是 `fissue export --status x` 产出的 JSON 里
    `count`（已过滤）与 `summary.total`（全仓库）互相矛盾，Markdown 的「概览」与「明细」
    条数也对不上——读者据此会得出错误结论。
    """
    from fissue.models import ItemStatus

    _seed(repo, number=1)
    other = _seed(repo, number=2)
    repo.set_item_status(other.key, ItemStatus.NEEDS_MANUAL)

    assert exp.summarize(repo)["total"] == 2
    assert exp.summarize(repo, status=ItemStatus.NEW)["total"] == 1

    payload = json.loads(exp.export_json(repo, status=ItemStatus.NEW))
    assert payload["count"] == payload["summary"]["total"] == 1, "一份导出里两组数字必须一致"

    md = exp.export_markdown(repo, status=ItemStatus.NEW)
    assert "| 条目总数 | 1 |" in md, f"Markdown 概览要与明细同口径：{md[:200]}"


# ---------------------------------------------------------------------------
# 队列抢占（同批被两个写者各跑一次 LLM 的根因修复）
# ---------------------------------------------------------------------------


def test_claim_unflushed_is_exclusive(repo: Repository) -> None:
    """一批条目只有第一个写者能抢到。"""
    _seed(repo, number=31)
    eid = repo.enqueue(QueueName.VERIFY, "github:psf/requests#31")
    repo.finish_queue_entry(eid, status="done", result={})

    assert [r.id for r in repo.claim_unflushed(QueueName.VERIFY)] == [eid]
    assert repo.claim_unflushed(QueueName.VERIFY) == [], "已被抢走的批次不该再次交出"


def test_claim_unflushed_releases_crashed_claims(repo: Repository, monkeypatch) -> None:
    """抢占后进程被 kill（行停在 running）要能放回，不能永久卡住。

    用「把陈旧窗口设成负数」模拟超龄，避免依赖 SQLite 里 datetime 的存储格式。
    """
    _seed(repo, number=32)
    eid = repo.enqueue(QueueName.VERIFY, "github:psf/requests#32")
    repo.finish_queue_entry(eid, status="done", result={})

    assert [r.id for r in repo.claim_unflushed(QueueName.VERIFY)] == [eid]
    assert repo.claim_unflushed(QueueName.VERIFY) == [], "未超龄的抢占不该被重复交出"

    monkeypatch.setattr(repo, "CLAIM_STALE_SECONDS", -1)
    assert [r.id for r in repo.claim_unflushed(QueueName.VERIFY)] == [eid], "超龄 running 要放回"


def test_finish_flush_claim_semantics(repo: Repository) -> None:
    """收尾：拿到结论的落 flushed_at 并回 done；缺结论的回 done 但**不落** flushed_at。"""
    _seed(repo, number=33)
    _seed(repo, number=34)
    done_id = repo.enqueue(QueueName.VERIFY, "github:psf/requests#33")
    miss_id = repo.enqueue(QueueName.VERIFY, "github:psf/requests#34")
    for eid in (done_id, miss_id):
        repo.finish_queue_entry(eid, status="done", result={})

    assert {r.id for r in repo.claim_unflushed(QueueName.VERIFY)} == {done_id, miss_id}

    repo.finish_flush_claim(concluded_ids=[done_id], missing_ids=[miss_id])

    assert [r.id for r in repo.queue_unflushed(QueueName.VERIFY)] == [miss_id], "缺结论的要留待下轮"


# ---------------------------------------------------------------------------
# 通知去重
# ---------------------------------------------------------------------------


def test_notification_claim_only_unique_conflict_means_already_sent(repo: Repository) -> None:
    """同一 dedup_key 第二次占位返回 False（真的发过了）。"""
    assert repo.try_claim_notification("k1", "ev", "item", "console", {}) is True
    assert repo.try_claim_notification("k1", "ev", "item", "console", {}) is False


def test_notification_claim_does_not_swallow_db_errors(repo: Repository, monkeypatch) -> None:
    """真正的库错误必须抛出。

    回归：原先裸 ``except Exception: return False``，而调用方把 False 读成「已发过」并
    把该渠道记成发送成功——一次 ``database is locked`` 就能让这条告警永久消失还报成功。
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    def boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise OperationalError("INSERT INTO notifications", {}, Exception("database is locked"))

    monkeypatch.setattr(Session, "flush", boom)

    with pytest.raises(OperationalError):
        repo.try_claim_notification("k2", "ev", "item", "console", {})


# ---------------------------------------------------------------------------
# 内容变更 → 结论作废
# ---------------------------------------------------------------------------


def _upsert_variant(repo: Repository, item: RawItem, **changes) -> None:
    """按原条目改几个字段后再抓取一次（模拟平台侧内容变了）。"""
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    payload = item.model_dump()
    payload.update(changes)
    repo.upsert_item(rid, RawItem(**payload))


def test_material_change_puts_item_back_for_reeval(repo: Repository) -> None:
    """正文这类**实质**变化 → 已有结论作废、条目回到 new 等待重评。

    回归：`upsert_item` 原先只返回 `changed=True`，而 `changed_keys` 全仓**没有任何消费者**，
    `items_needing_eval` 又只取 `new/classified` → 内容变了也永不重评；更糟的是
    `content_hash` 在同一处被无条件覆写，事后连「这条结论过期了」都判不出来，
    报告/看板/派发全都基于过期结论。
    """
    item = _seed(repo, number=41)
    key = item.key
    repo.save_evaluation(key, Evaluation(priority=Priority.TIER1, model="stub"))
    assert repo.get_item_row(key).status == ItemStatus.EVALUATED.value

    _upsert_variant(repo, item, body="改过的正文")

    assert repo.get_item_row(key).status == ItemStatus.NEW.value, "实质变化要让结论作废并重评"
    assert repo.get_item(key).stale_reasons, "作废这件事也必须留痕"
    assert any(i.key == key for i in repo.items_needing_eval()), "应当重新出现在待评测里"


def test_comment_only_change_does_not_invalidate_conclusion(repo: Repository) -> None:
    """只是评论增长：评论表要刷新，但结论不动。

    否则活跃 issue 上每来一条评论就重评测一次，持续烧 token（指纹里的 `len(comments)`
    正是为「要不要重写评论表」服务的，不该拿来判结论过期）。
    """
    item = _seed(repo, number=42)
    key = item.key
    repo.save_evaluation(key, Evaluation(priority=Priority.TIER1, model="stub"))

    _upsert_variant(repo, item, comments=[*item.comments, Comment(author="c2", body="又来一条")])

    assert repo.get_item_row(key).status == ItemStatus.EVALUATED.value, "评论增长不该触发重评"
    assert repo.get_item(key).stale_reasons == []
    assert len(repo.get_item(key).comments) == 2, "但评论表必须刷新"


def test_material_change_on_in_flight_fix_marks_stale_without_rewinding(repo: Repository) -> None:
    """修复已开工时**不回落**（免得重复修复/重复建 PR），但过期必须留痕。"""
    item = _seed(repo, number=43)
    key = item.key
    for status in (ItemStatus.FIX_QUEUED, ItemStatus.FIXING, ItemStatus.PR_CREATED):
        repo.set_item_status(key, status, priority=Priority.TIER1)
        _upsert_variant(repo, item, title=f"标题改成 {status.value}")
        assert repo.get_item_row(key).status == status.value, "已开工的修复不该被回退"
        assert repo.get_item(key).stale_reasons, "但过期必须看得见"


def test_stale_mark_sticks_until_reeval(repo: Repository) -> None:
    """过期标记要黏到「重新出结论」为止。

    否则下一轮抓取时内容已和库里的 hash 一致（`changed=False`），`row.raw` 会被
    `item.raw` 冲掉，过期重新变成不可见——那正是这次要修的「静默」本身。
    """
    item = _seed(repo, number=44)
    key = item.key
    repo.save_evaluation(key, Evaluation(priority=Priority.TIER1, model="stub"))

    _upsert_variant(repo, item, body="改过的正文")
    assert repo.get_item(key).stale_reasons

    _upsert_variant(repo, item, body="改过的正文")          # 内容没再变
    assert repo.get_item(key).stale_reasons, "标记必须黏住，不能被下一次抓取冲掉"

    repo.save_evaluation(key, Evaluation(priority=Priority.TIER1, model="stub"))
    assert repo.get_item(key).stale_reasons == [], "重新出了结论就不再过期"


# ---------------------------------------------------------------------------
# 评论残缺：不得覆盖已入库的讨论
# ---------------------------------------------------------------------------


def _upsert_degraded_comments(repo: Repository, item: RawItem, comments: list, reason: str) -> None:
    """抓一次「评论残缺」的数据（评论为空或更少，raw 里带残缺标记）。"""
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    payload = item.model_dump()
    payload["comments"] = comments
    variant = RawItem(**payload)
    variant.mark_degraded(reason)
    repo.upsert_item(rid, variant)


def test_degraded_comments_never_shrink_stored_discussion(repo: Repository) -> None:
    """本轮评论残缺且比库里少 → 绝不能用它覆盖已入库的讨论。

    回归：`upsert_item` 原先照删照写，一次评论接口抖动就把整条讨论清空；评测随后可能就基于
    「零评论」下结论，而结论会被**回写到平台**、增量抓取又不会再碰这条——错误就此固化在
    库里和平台上。
    """
    item = _seed(repo, number=51)
    assert len(repo.get_item(item.key).comments) == 1

    _upsert_degraded_comments(repo, item, [], "comments_unavailable#51")

    assert len(repo.get_item(item.key).comments) == 1, "残缺结果不得覆盖已有讨论"
    assert repo.get_item(item.key).comments_incomplete, "但「残缺」必须留在条目上"


def test_incomplete_but_not_smaller_comments_still_refresh(repo: Repository) -> None:
    """「残缺但没变少」仍要刷新。

    否则评论数超过上限的条目（`comments_may_be_truncated` 恒为真）会被永久冻住——那是把
    「别丢数据」修成「永远不更新」，同样错。
    """
    item = _seed(repo, number=52)
    _upsert_degraded_comments(
        repo,
        item,
        [Comment(author="c1", body="评论"), Comment(author="c2", body="新的")],
        "comments_may_be_truncated#52",
    )

    assert len(repo.get_item(item.key).comments) == 2


# ---------------------------------------------------------------------------
# 队列中断残留
# ---------------------------------------------------------------------------


def test_reclaim_stuck_queue_entries_returns_items_to_verify(repo: Repository, monkeypatch) -> None:
    """入队后没收尾的残留必须回收，条目放回 evaluated 重做。

    回归：该行永久停在 `pending` —— `queue_unflushed` 只认 `done`，它永远不会被 flush；
    `queue_pending_count` 恒 > 0，于是 `should_flush` 的「队列已无待处理条目」这条兜底被
    **永久废掉**；条目卡在 `queued`，而 `verify` 只取 `evaluated`，它再也不会被碰。
    """
    _seed(repo, number=61)
    key = "github:psf/requests#61"
    repo.set_item_status(key, ItemStatus.QUEUED)
    repo.enqueue(QueueName.VERIFY, key)          # 没有 finish_queue_entry → 就是中断点

    monkeypatch.setattr(repo, "STUCK_QUEUE_ENTRY_SECONDS", -1)      # 模拟超龄

    assert repo.reclaim_stuck_queue_entries() == [key]
    assert repo.queue_pending_count(QueueName.VERIFY) == 0, "残留不清，兜底就永久失效"
    assert repo.get_item_row(key).status == ItemStatus.EVALUATED.value, "条目要能重验"


def test_reclaim_leaves_fresh_and_finished_rows_alone(repo: Repository) -> None:
    """刚入队的（可能真在跑）与已收尾的都不能动。"""
    _seed(repo, number=62)
    _seed(repo, number=63)
    fresh = "github:psf/requests#62"
    done = "github:psf/requests#63"
    repo.enqueue(QueueName.VERIFY, fresh)
    eid = repo.enqueue(QueueName.VERIFY, done)
    repo.finish_queue_entry(eid, status="done", result={})

    assert repo.reclaim_stuck_queue_entries() == []
    assert repo.queue_pending_count(QueueName.VERIFY) == 1
    assert len(repo.queue_unflushed(QueueName.VERIFY)) == 1


# ---------------------------------------------------------------------------
# 导出：聚合计数 + 批量取结论
# ---------------------------------------------------------------------------


def test_summarize_aggregates_totals_in_sql(repo: Repository, monkeypatch) -> None:
    """概览的计数走聚合查询：不再把条目对象全加载进内存。

    回归：`summarize` 原先 `list_items(limit=100000)` —— 十万条领域对象（含标签、正文
    元数据）只为数几个数。
    """
    first = _seed(repo, number=81)
    _seed(repo, number=82)
    repo.set_item_status("github:psf/requests#82", ItemStatus.NEEDS_MANUAL)
    repo.save_evaluation(first.key, Evaluation(scores=Scores(importance=DimensionScore(score=70))))

    def boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("概览不该再把条目对象全加载进内存")

    monkeypatch.setattr(repo, "list_items", boom)

    stats = exp.summarize(repo)

    assert stats["total"] == 2
    # 第一条被 save_evaluation 推到 evaluated（既有语义），第二条被显式置为 needs_manual
    assert stats["by_status"] == {"evaluated": 1, "needs_manual": 1}
    assert stats["avg_scores"]["importance"] == 70.0


def test_export_fetches_conclusions_in_one_query(repo: Repository, monkeypatch) -> None:
    """导出批量取结论，不再逐条查。

    回归：原先每条一次 `latest_batch_conclusion`、每次还新开一个 session；
    `GET /api/export?limit=10000` 正好允许一万条这个规模。
    """
    item = _seed(repo, number=83)
    repo.save_batch_conclusion(
        item.key, "b1", verdict="fix", labels=["reproduced"],
        priority=Priority.TIER1, reason="低难度高重要性", confidence=0.9, model="stub",
    )

    def boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("不该逐条查结论")

    monkeypatch.setattr(repo, "latest_batch_conclusion", boom)

    payload = json.loads(exp.export_json(repo))
    assert payload["items"][0]["batch_conclusion"]["verdict"] == "fix"
    assert "fix" in exp.export_markdown(repo)


def test_latest_batch_conclusions_matches_the_single_key_version(repo: Repository) -> None:
    """批量版取的是**每条最新一条**，与逐条版本同口径。"""
    _seed(repo, number=84)
    _seed(repo, number=85)
    key, other = "github:psf/requests#84", "github:psf/requests#85"
    for verdict in ("skip", "fix"):
        repo.save_batch_conclusion(
            key, f"b-{verdict}", verdict=verdict, labels=[],
            priority=Priority.NONE, reason="r", confidence=0.5, model="stub",
        )

    out = repo.latest_batch_conclusions([key, other])

    assert out[key].verdict == "fix", "同 key 要取最新那条"
    assert other not in out, "没有结论的条目不该出现在结果里"
    assert out[key].verdict == repo.latest_batch_conclusion(key).verdict, "与逐条版本同口径"


# ---------------------------------------------------------------------------
# SQLite 并发：WAL 与 busy 超时
# ---------------------------------------------------------------------------


def test_sqlite_uses_wal_and_a_longer_busy_timeout(tmp_path) -> None:
    """SQLite 要开 WAL 并把 busy 超时明确调大。

    实测（sqlite 3.54 / Python 3.11）：驱动**本来就**有 5s busy 超时——写锁被占 0.8s 时
    第二个写者能等过去，但把超时压到 0.5s 就立刻 ``database is locked``。也就是说真正的上限
    是那 5s；而没有 WAL 意味着回滚日志模式下长**读**事务（导出、看板）会挡住写者的提交。
    """
    from fissue.store.db import Database

    db = Database(f"sqlite:///{tmp_path}/w.db")
    db.create_all()

    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        (busy,) = conn.exec_driver_sql("PRAGMA busy_timeout").one()
        assert int(busy) >= 30_000, f"busy 超时要 >= 30s，实际 {busy}ms"


def test_sqlite_concurrent_writers_do_not_lock_each_other_out(tmp_path) -> None:
    """并发写不该因写锁互撞而失败（WAL + 30s busy 超时兜住）。"""
    import threading

    from fissue.store.db import Database

    db = Database(f"sqlite:///{tmp_path}/c.db")
    db.create_all()
    repo = Repository(db)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            repo.upsert_item(
                rid,
                RawItem(
                    platform=Platform.GITHUB, repo="psf/requests", number=n,
                    item_type=ItemType.ISSUE, title=f"t{n}", body="b",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"并发写不该失败：{errors}"
    assert {str(i.number) for i in repo.list_items(limit=20)} == {str(n) for n in range(1, 9)}
