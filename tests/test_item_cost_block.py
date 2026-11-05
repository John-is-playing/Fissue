"""成本归因与卡住原因：用量聚合、详情页展示、单条成本上限。

三件事各自的来历：
* **成本归因**：`llm_usage` 原先只有 `repo_key`，回答不了"这条 PR 花了多少钱"；
* **单条上限**：按仓库/按天的预算管不住"某一条反复修十几轮"，一条难 issue 能吃掉
  整天额度，本来能修好的条目连机会都没有；
* **卡住原因**：只记 `needs_manual` 不记原因时，用户只能自己去翻日志猜。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fissue.fixer.autofix import AutoFixer
from fissue.models import (
    BlockReason,
    Category,
    Evaluation,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    RawItem,
    RepoRef,
)
from fissue.store.repository import Repository
from fissue.store.tables import ItemRow
from fissue.web.app import create_app

# ---------------------------------------------------------------------------
# 用量聚合
# ---------------------------------------------------------------------------


def test_usage_for_item_sums_success_and_failure(repo):
    """失败调用也算钱：模型被调用了就产生费用。

    把失败排除会让"最贵的那条"看起来最便宜——那条通常正是反复失败、反复重试的。
    """
    key = "github:acme/web#1"
    repo.record_usage(
        repo_key="acme/web", item_key=key, purpose="evaluate:acme/web", model="m",
        prompt_tokens=1200, completion_tokens=300, cost_usd=0.0042,
    )
    repo.record_usage(
        repo_key="acme/web", item_key=key, purpose="fix:acme/web", model="m",
        prompt_tokens=8000, completion_tokens=2000, cost_usd=0.031, success=False,
    )
    # 别的条目、以及没有归属的调用都不该算进来
    repo.record_usage(
        repo_key="acme/web", item_key="github:acme/web#2", purpose="fix:acme/web", model="m",
        prompt_tokens=1, completion_tokens=1, cost_usd=9.0,
    )
    repo.record_usage(
        repo_key="acme/web", purpose="batch:acme/web", model="m",
        prompt_tokens=1, completion_tokens=1, cost_usd=9.0,
    )

    stats = repo.usage_for_item(key)
    assert stats["calls"] == 2
    assert stats["prompt_tokens"] == pytest.approx(9200)
    assert stats["cost_usd"] == pytest.approx(0.0352)


def test_usage_for_item_empty(repo):
    stats = repo.usage_for_item("github:acme/web#404")
    assert stats == {
        "prompt_tokens": 0.0,
        "completion_tokens": 0.0,
        "cost_usd": 0.0,
        "calls": 0.0,
    }


# ---------------------------------------------------------------------------
# 卡住原因的读写
# ---------------------------------------------------------------------------


def _item(repo: Repository, number: int = 1) -> RawItem:
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="acme", name="web"))
    raw = RawItem(
        platform=Platform.GITHUB, repo="acme/web", number=number,
        item_type=ItemType.ISSUE, title="t", body="b",
    )
    repo.upsert_item(rid, raw)
    return raw


def _row(repo: Repository, key: str) -> ItemRow:
    from sqlalchemy import select

    with repo.db.session() as session:
        row = session.scalar(select(ItemRow).where(ItemRow.key == key))
        session.expunge(row)
        return row


def test_set_item_status_writes_and_clears_reason(repo):
    item = _item(repo)
    repo.set_item_status(
        item.key, ItemStatus.NEEDS_MANUAL, block_reason=BlockReason.PERMISSION.value
    )
    assert _row(repo, item.key).block_reason == BlockReason.PERMISSION.value

    # None = 不动这一列（默认），空串 = 显式清除：两者语义不同
    repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL)
    assert _row(repo, item.key).block_reason == BlockReason.PERMISSION.value

    repo.set_item_status(item.key, ItemStatus.QUEUED, block_reason="")
    assert _row(repo, item.key).block_reason in ("", None)


# ---------------------------------------------------------------------------
# 详情页
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def _detail(client, key: str) -> str:
    response = client.get("/item/" + quote(key, safe=""))
    assert response.status_code == 200
    return response.text


def test_detail_shows_cost_block_collapsed(repo, client):
    item = _item(repo)
    repo.record_usage(
        repo_key="acme/web", item_key=item.key, purpose="evaluate:acme/web", model="m",
        prompt_tokens=1000, completion_tokens=200, cost_usd=0.0123,
    )
    html = _detail(client, item.key)
    assert "这条花了多少钱" in html
    assert "0.0123" in html
    # 默认折叠：成本不该一进页面就铺开
    assert "<details" in html.split("这条花了多少钱")[0][-200:]


def test_detail_shows_block_reason_and_actions(repo, client):
    item = _item(repo)
    repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL, block_reason="missing_test")
    html = _detail(client, item.key)
    assert "为什么卡住" in html
    assert "缺少可执行的验证" in html
    assert "test_hint" in html           # 下一步要具体到能照做
    assert "重跑验证" in html and "/settings" in html


def test_detail_without_reason_has_no_block(repo, client):
    item = _item(repo)
    html = _detail(client, item.key)
    assert "为什么卡住" not in html


def test_unknown_reason_falls_back_to_generic(repo, client):
    """历史数据或将来新增的枚举值不能让详情页 500。"""
    item = _item(repo)
    repo.set_item_status(item.key, ItemStatus.NEEDS_MANUAL, block_reason="something_new")
    html = _detail(client, item.key)
    assert "原因未分类" in html


# ---------------------------------------------------------------------------
# 单条成本上限
# ---------------------------------------------------------------------------


async def test_per_item_budget_stops_fix_and_explains(fixer_ctx, repo, settings):
    settings.llm.budget.per_item_usd = 0.01
    item = _item(repo)
    repo.save_evaluation(
        item.key, Evaluation(category=Category.BUG, priority=Priority.TIER1, model="s")
    )
    repo.set_item_status(item.key, ItemStatus.FIX_QUEUED, priority=Priority.TIER1)
    repo.record_usage(
        repo_key="acme/web", item_key=item.key, purpose="evaluate:acme/web", model="m",
        prompt_tokens=100, completion_tokens=10, cost_usd=0.02,
    )

    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(item.key))

    assert attempt.outcome is FixOutcome.SKIPPED
    assert "成本已达上限" in (attempt.error or "")
    row = _row(repo, item.key)
    assert row.status == ItemStatus.NEEDS_MANUAL.value
    assert row.block_reason == BlockReason.BUDGET.value


async def test_per_item_limit_off_by_default(fixer_ctx, repo, settings):
    """默认 0 = 不限：不能让升级本身把已有部署的修复全停掉。"""
    assert settings.llm.budget.per_item_usd == 0.0
    item = _item(repo)
    repo.record_usage(
        repo_key="acme/web", item_key=item.key, purpose="evaluate:acme/web", model="m",
        prompt_tokens=1, completion_tokens=1, cost_usd=99.0,
    )
    # 预算闸门不该在"没有评测结果"之前抢答：这条会先被策略闸门拦下
    attempt = await AutoFixer(fixer_ctx).fix_item(repo.get_item(item.key))
    assert attempt.outcome is FixOutcome.SKIPPED
    assert "成本" not in (attempt.error or "")
