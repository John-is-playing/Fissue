"""存储层与导出测试（SQLite 内存/临时库，无需 PostgreSQL）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

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
