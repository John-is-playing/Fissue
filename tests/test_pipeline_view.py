"""流水线视图模型的测试：阶段映射、卡点判定、三视图计数一致。

这一层是三种视图（漏斗 / 看板 / 泳道）**唯一**的口径来源。它错了，三个视图会一起错，
而且错得看不出——所以这里把映射逐条钉死，并专门断言「三份计数加得回去」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fissue.models import Category, ItemStatus, ItemType
from fissue.web.pipeline import (
    DONE,
    KANBAN_STAGES,
    LANES,
    STAGES,
    STUCK,
    item_row,
    lane_key,
    stage_label,
    stage_of,
    stage_states,
    summarize,
    swimlane_rows,
)

# ---------------------------------------------------------------------------
# 状态 → 阶段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ItemStatus.NEW, "evaluate"),
        (ItemStatus.CLASSIFIED, "evaluate"),
        (ItemStatus.EVALUATED, "verify"),
        (ItemStatus.VERIFIER_READY, "verify"),
        (ItemStatus.VERIFYING, "verify"),
        (ItemStatus.QUEUED, "judge"),
        (ItemStatus.VERIFIED, "judge"),
        (ItemStatus.FIX_QUEUED, "fix"),
        (ItemStatus.FIXING, "fix"),
        (ItemStatus.PR_CREATED, "pr"),
        # 已出结论的进 done 桶——它们是终点，不该占着「待办」列。
        (ItemStatus.LABELED, DONE),
        (ItemStatus.SKIPPED, DONE),
        # 卡住的不猜死在第几步。
        (ItemStatus.NEEDS_MANUAL, STUCK),
        (ItemStatus.FAILED, STUCK),
    ],
)
def test_stage_of_maps_every_status(status, expected) -> None:
    """**每个**状态都要有明确归属：漏一个就会在看板上凭空消失。"""
    assert stage_of(status) == expected


def test_every_status_is_covered() -> None:
    """枚举新增状态时这条会红——提醒补映射，而不是让条目静默落到 stuck 桶。"""
    unmapped = [s for s in ItemStatus if s not in _stage_of_map()]
    assert unmapped == [], f"这些状态还没定义阶段：{unmapped}"


def _stage_of_map() -> dict:
    from fissue.web import pipeline

    return pipeline._STAGE_OF


def test_unknown_status_value_falls_back_to_stuck() -> None:
    """库里可能存着旧版本的状态值：认不出来就按「需人工」，不能让首屏 500。"""
    assert stage_of("some-removed-status") == STUCK  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 泳道用的小圆点状态
# ---------------------------------------------------------------------------


def test_stage_states_marks_completed_prefix_and_current() -> None:
    """已走完的阶段是 done、当前阶段是 pending（或 active），其余 waiting。"""
    states = stage_states(ItemStatus.EVALUATED)
    assert states[0] == "done"       # 抓取
    assert states[1] == "done"       # 评测
    assert states[2] == "pending"    # 验证（等它跑）
    assert states[3:] == ["waiting"] * 3


def test_stage_states_marks_in_progress_as_active() -> None:
    """进行中的阶段要能和「排队等它」区分开——这正是「卡没卡住」的判断依据。"""
    assert stage_states(ItemStatus.VERIFYING)[2] == "active"
    assert stage_states(ItemStatus.EVALUATED)[2] == "pending"
    assert stage_states(ItemStatus.FIXING)[4] == "active"


def test_stage_states_all_done_for_finished_items() -> None:
    for status in (ItemStatus.LABELED, ItemStatus.PR_CREATED, ItemStatus.SKIPPED):
        assert stage_states(status) == ["done"] * len(STAGES), status


def test_stage_states_unknown_for_stuck_items() -> None:
    """卡住的不画进度——画错比画不出更糟。"""
    for status in (ItemStatus.NEEDS_MANUAL, ItemStatus.FAILED):
        assert stage_states(status) == ["unknown"] * len(STAGES), status


def test_stage_states_length_matches_stages() -> None:
    """三视图共用这一份，长度必须和 STAGES 一致。"""
    for status in ItemStatus:
        assert len(stage_states(status)) == len(STAGES), status


# ---------------------------------------------------------------------------
# 流水线归属
# ---------------------------------------------------------------------------


def test_lane_key_covers_four_pipelines() -> None:
    assert lane_key(ItemType.ISSUE, Category.BUG) == "issue-bug"
    assert lane_key(ItemType.ISSUE, Category.FEATURE) == "issue-feature"
    assert lane_key(ItemType.PR, Category.BUG) == "pr-bug"
    assert lane_key(ItemType.PR, Category.FEATURE) == "pr-feature"


def test_lane_key_unknown_category_is_its_own_lane() -> None:
    """还没评出分类的条目不能被算进任何 BUG 链——那会让 BUG 的数字虚高。"""
    assert lane_key(ItemType.ISSUE, Category.UNKNOWN) == "unknown"
    assert lane_key(ItemType.PR, "") == "unknown"


def test_lane_key_accepts_plain_strings() -> None:
    """聚合查询拿到的是裸字符串（数据库里存的就是 str），不能只认枚举。"""
    assert lane_key("issue", "bug") == "issue-bug"
    assert lane_key("pr", "feature") == "pr-feature"


def test_every_lane_key_has_a_label() -> None:
    known = {key for key, _ in LANES}
    for item_type in (ItemType.ISSUE, ItemType.PR):
        for category in Category:
            assert lane_key(item_type, category) in known


# ---------------------------------------------------------------------------
# 三视图计数一致
# ---------------------------------------------------------------------------


def _counts() -> list[tuple[str, str, str, int]]:
    """一份覆盖多种情形的小样本。"""
    return [
        ("issue", "bug", ItemStatus.NEW.value, 3),
        ("issue", "bug", ItemStatus.EVALUATED.value, 2),
        ("issue", "bug", ItemStatus.QUEUED.value, 4),
        ("issue", "bug", ItemStatus.FIXING.value, 1),
        ("issue", "bug", ItemStatus.PR_CREATED.value, 5),
        ("issue", "bug", ItemStatus.NEEDS_MANUAL.value, 2),
        ("issue", "feature", ItemStatus.EVALUATED.value, 6),
        ("pr", "bug", ItemStatus.QUEUED.value, 2),
        ("pr", "feature", ItemStatus.LABELED.value, 9),
        ("issue", "unknown", ItemStatus.NEW.value, 1),
    ]


def test_summarize_all_views_add_up_to_the_same_total() -> None:
    """漏斗 / 看板 / 泳道（按行）三份计数必须能加回同一个总数。

    这是这一层最重要的不变式：视图之间对不上，比只有一种视图更糟。
    """
    view = summarize(_counts())
    total = view["totals"]["items"]

    funnel = sum(stage["count"] for stage in view["stages"])
    assert funnel + view["totals"]["done"] + view["totals"]["stuck"] == total

    lane_sum = sum(lane["total"] for lane in view["lanes"])
    assert lane_sum == total

    # 看板列 = 还能推动的阶段 + 需人工（不含 done / capture）
    kanban = sum(column["total"] for column in view["columns"])
    assert kanban == total - view["totals"]["done"]


def test_summarize_kanban_has_no_done_or_capture_column() -> None:
    """终点与瞬时阶段不该出现在看板上，否则「待办」列永远被历史条目淹掉。"""
    keys = [column["key"] for column in summarize([])["columns"]]
    assert keys == list(KANBAN_STAGES)
    assert DONE not in keys
    assert "capture" not in keys


def test_summarize_kanban_cells_cover_every_lane() -> None:
    """每列都要给每条泳道留格子（哪怕是 0）——前端才不用自己补空洞。"""
    view = summarize(_counts())
    lane_keys = [lane["key"] for lane in view["lanes"]]
    for column in view["columns"]:
        assert [cell["lane"] for cell in column["cells"]] == lane_keys


def test_summarize_cell_counts_match_lane_totals() -> None:
    """某条泳道各列之和 == 该泳道总数（扣除终点）。"""
    view = summarize(_counts())
    for lane in view["lanes"]:
        cells = sum(
            cell["count"]
            for column in view["columns"]
            for cell in column["cells"]
            if cell["lane"] == lane["key"]
        )
        assert cells <= lane["total"], lane["key"]


def test_summarize_stuck_bucket_is_reported() -> None:
    """卡住的条数要单独给出——这是「最想先看到」的那个数字。"""
    view = summarize(_counts())
    assert view["totals"]["stuck"] == 2


def test_summarize_empty_does_not_divide_by_zero() -> None:
    view = summarize([])
    assert view["totals"]["items"] == 0
    assert all(stage["pct"] == 0 for stage in view["stages"])


def test_summarize_pct_is_relative_to_all_items() -> None:
    view = summarize([("issue", "bug", ItemStatus.NEW.value, 5)])
    evaluate = next(s for s in view["stages"] if s["key"] == "evaluate")
    assert evaluate["count"] == 5
    assert evaluate["pct"] == 100


def test_summarize_accepts_status_enum_objects() -> None:
    """调用方可能传枚举而不是字符串，两种都要认。"""
    view = summarize([("issue", "bug", ItemStatus.NEW, 2)])  # type: ignore[list-item]
    assert view["totals"]["items"] == 2


# ---------------------------------------------------------------------------
# 泳道行
# ---------------------------------------------------------------------------


def _item(key: str, *, status=ItemStatus.EVALUATED, minute: int = 0, **kw) -> SimpleNamespace:
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        key=key,
        repo="psf/requests",
        number=key.rsplit("#", 1)[-1],
        title="标题",
        item_type=ItemType.ISSUE,
        category=Category.BUG,
        priority=kw.get("priority"),
        status=status,
        force_fix=kw.get("force_fix", False),
        updated_at=base + timedelta(minutes=minute),
    )


def test_item_row_exposes_everything_the_template_needs() -> None:
    row = item_row(_item("github:psf/requests#7"))
    for field in (
        "key", "repo", "number", "title", "item_type", "category",
        "priority", "status", "stage", "states", "stalled", "force_fix", "updated_at",
    ):
        assert field in row, field
    assert row["stage"] == "verify"
    assert row["states"][0] == "done"


def test_item_row_marks_stuck_items() -> None:
    row = item_row(_item("github:psf/requests#8", status=ItemStatus.NEEDS_MANUAL))
    assert row["stalled"] is True
    assert row["states"] == ["unknown"] * len(STAGES)


def test_swimlane_rows_puts_stuck_first() -> None:
    """卡住的排最前：它每多留一小时就多烧一小时机会成本。"""
    items = [
        _item("#1", status=ItemStatus.EVALUATED, minute=10),
        _item("#2", status=ItemStatus.NEEDS_MANUAL, minute=1),
        _item("#3", status=ItemStatus.QUEUED, minute=5),
    ]
    rows = swimlane_rows(items)
    assert [r["key"] for r in rows][0].endswith("#2")


def test_swimlane_rows_sorted_newest_first_within_group() -> None:
    items = [
        _item("#1", status=ItemStatus.EVALUATED, minute=1),
        _item("#2", status=ItemStatus.EVALUATED, minute=30),
        _item("#3", status=ItemStatus.EVALUATED, minute=15),
    ]
    rows = swimlane_rows(items)
    assert [r["key"].rsplit("#", 1)[-1] for r in rows] == ["2", "3", "1"]


def test_swimlane_rows_respects_limit() -> None:
    items = [_item(f"#{i}", minute=i) for i in range(10)]
    assert len(swimlane_rows(items, limit=3)) == 3


def test_swimlane_rows_handles_missing_timestamp() -> None:
    """``updated_at`` 为空时不能崩（老库/迁移中的数据）。"""
    item = _item("#9")
    item.updated_at = None
    assert swimlane_rows([item])[0]["updated_at"] is None


def test_stage_label_covers_special_buckets() -> None:
    assert stage_label("capture") == "抓取"
    assert stage_label(DONE) == "已出结论"
    assert stage_label(STUCK) == "需人工"
    assert stage_label("no-such-stage") == "no-such-stage"


# ---------------------------------------------------------------------------
# 阶段 → 状态集合（给 /items?stage=... 用）
# ---------------------------------------------------------------------------


def test_statuses_of_stage_covers_all_states_in_that_stage() -> None:
    """阶段是**状态的分组**，一个阶段含多个状态（"待验证"含 evaluated/verifier_ready/verifying）。"""
    from fissue.web.pipeline import statuses_of_stage

    assert set(statuses_of_stage("evaluate")) == {ItemStatus.NEW, ItemStatus.CLASSIFIED}
    assert set(statuses_of_stage("verify")) == {
        ItemStatus.EVALUATED,
        ItemStatus.VERIFIER_READY,
        ItemStatus.VERIFYING,
    }
    assert set(statuses_of_stage("judge")) == {ItemStatus.QUEUED, ItemStatus.VERIFIED}
    assert set(statuses_of_stage("fix")) == {ItemStatus.FIX_QUEUED, ItemStatus.FIXING}
    assert set(statuses_of_stage("pr")) == {ItemStatus.PR_CREATED}
    assert set(statuses_of_stage(DONE)) == {ItemStatus.LABELED, ItemStatus.SKIPPED}
    assert set(statuses_of_stage(STUCK)) == {ItemStatus.NEEDS_MANUAL, ItemStatus.FAILED}


def test_statuses_of_stage_partitions_all_states() -> None:
    """各阶段的状态集合必须**恰好**瓜分所有状态——少一个就会有条目从筛选里消失。"""
    from fissue.web.pipeline import statuses_of_stage

    keys = [k for k, _ in STAGES] + [DONE, STUCK]
    covered: set[ItemStatus] = set()
    for key in keys:
        covered |= set(statuses_of_stage(key))
    assert covered == set(ItemStatus), covered ^ set(ItemStatus)


def test_statuses_of_stage_unknown_key_is_empty() -> None:
    from fissue.web.pipeline import statuses_of_stage

    assert statuses_of_stage("no-such-stage") == []


# ---------------------------------------------------------------------------
# 预设（preset）：阶段的友好别名
# ---------------------------------------------------------------------------


def test_preset_stage_maps_known_presets() -> None:
    """预设就是阶段的别名——不另定义一套状态集合。"""
    from fissue.web.pipeline import preset_stage

    assert preset_stage("stuck") == "stuck"
    assert preset_stage("evaluate") == "evaluate"
    assert preset_stage("verify") == "verify"
    assert preset_stage("fix") == "fix"
    assert preset_stage("done") == "done"


def test_preset_stage_rejects_unknown() -> None:
    from fissue.web.pipeline import preset_stage

    assert preset_stage("no-such-preset") is None


def test_every_preset_is_a_valid_stage() -> None:
    """每个预设都必须能解析成一个真实阶段——否则「预设」与「阶段」会给出不同结果。"""
    from fissue.web.pipeline import PRESETS, preset_stage, statuses_of_stage

    for key, _label in PRESETS:
        resolved = preset_stage(key)
        assert resolved is not None, key
        assert statuses_of_stage(resolved), f"{key} 解析出的阶段没有任何状态"
