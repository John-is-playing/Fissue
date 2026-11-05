"""PR 合并/关闭回写：webhook 事件解析 + 条目状态回写。

这是"真的修好了多少条"的唯一来源。之前状态机停在 `pr_created`：PR 合了还是被
关了，条目都不会变——于是既算不出闭环率，也没人知道"提了但没人合"的堆了多少。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from fissue.config import Settings
from fissue.models import (
    BlockReason,
    FixAttempt,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    RawItem,
    RepoRef,
)
from fissue.store.db import build_database
from fissue.store.repository import Repository
from fissue.store.tables import ItemRow
from fissue.web.hooks import parse_event

# ---------------------------------------------------------------------------
# 事件解析
# ---------------------------------------------------------------------------


def test_github_merged_pr():
    event = parse_event(
        Platform.GITHUB,
        {
            "action": "closed",
            "repository": {"full_name": "acme/web"},
            "pull_request": {"number": 7, "merged": True},
        },
    )
    assert event.is_pr_closure is True
    assert event.merged is True
    assert event.number == 7
    assert event.repo_slug == "acme/web"


def test_github_closed_without_merge():
    event = parse_event(
        Platform.GITHUB,
        {
            "action": "closed",
            "repository": {"full_name": "acme/web"},
            "pull_request": {"number": 7, "merged": False},
        },
    )
    assert event.is_pr_closure is True
    assert event.merged is False


def test_gitlab_merged_mr():
    event = parse_event(
        Platform.GITHUB,
        {
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "acme/web"},
            "object_attributes": {"iid": 9, "state": "merged"},
        },
    )
    assert event.is_pr_closure is True
    assert event.merged is True
    assert event.number == 9


def test_issue_event_is_not_pr_closure():
    """普通 Issue 事件不能被当成"PR 关闭"，否则会误改状态。"""
    event = parse_event(
        Platform.GITHUB,
        {
            "action": "opened",
            "repository": {"full_name": "acme/web"},
            "issue": {"number": 3},
        },
    )
    assert event.is_pr_closure is False
    assert event.item_type is ItemType.ISSUE


def test_open_pr_is_not_closure():
    event = parse_event(
        Platform.GITHUB,
        {
            "action": "opened",
            "repository": {"full_name": "acme/web"},
            "pull_request": {"number": 7, "merged": False},
        },
    )
    # 打开事件里 merged 恒为 False，不能当成"关闭未合并"去改状态
    assert event.is_pr_closure is False


# ---------------------------------------------------------------------------
# 回写
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    settings = Settings()
    settings.app.data_dir = str(tmp_path)
    settings.database.url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    db = build_database(settings)
    db.create_all()
    return Repository(db)


def _seed_item(repo: Repository, pr_number: int, *, repo_slug: str = "acme/web") -> str:
    owner, name = repo_slug.split("/", 1)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner=owner, name=name))
    raw = RawItem(
        platform=Platform.GITHUB,
        repo=repo_slug,
        number=1,
        item_type=ItemType.ISSUE,
        title="t",
        body="b",
    )
    repo.upsert_item(rid, raw)
    repo.save_fix_attempt(
        FixAttempt(
            item_key=raw.key,
            outcome=FixOutcome.SUCCESS,
            branch="fissue/fix-1",
            pr_number=pr_number,
            pr_url=f"https://example.invalid/{pr_number}",
        )
    )
    return raw.key


def _row(repo: Repository, key: str) -> ItemRow:
    with repo.db.session() as session:
        row = session.scalar(select(ItemRow).where(ItemRow.key == key))
        session.expunge(row)
        return row


def test_merged_marks_item_and_records_time(repo):
    key = _seed_item(repo, 77)
    assert repo.apply_pr_outcome(repo_slug="acme/web", pr_number=77, merged=True) == key
    row = _row(repo, key)
    assert row.status == ItemStatus.MERGED.value
    assert row.merged_at is not None
    assert row.block_reason is None


def test_closed_without_merge_goes_needs_manual(repo):
    """没合并通常意味着人不想合（范围大/方向不对）。回 `verified` 会让它反复进队列烧钱。"""
    key = _seed_item(repo, 88)
    assert repo.apply_pr_outcome(repo_slug="acme/web", pr_number=88, merged=False) == key
    row = _row(repo, key)
    assert row.status == ItemStatus.NEEDS_MANUAL.value
    assert row.block_reason == BlockReason.PR_CLOSED.value


def test_unknown_pr_is_ignored(repo):
    """别人的 PR 关闭事件也会推过来：查不到对应尝试就什么都不动。"""
    key = _seed_item(repo, 77)
    before = _row(repo, key)
    assert repo.apply_pr_outcome(repo_slug="acme/web", pr_number=999, merged=True) is None
    after = _row(repo, key)
    assert (before.status, before.block_reason) == (after.status, after.block_reason)


def test_repo_mismatch_is_ignored(repo):
    """同号不同仓库不能互相串：PR 编号只在仓库内唯一。"""
    _seed_item(repo, 77)
    assert repo.apply_pr_outcome(repo_slug="other/repo", pr_number=77, merged=True) is None
