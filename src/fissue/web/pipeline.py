"""流水线视图模型：把条目状态翻译成「阶段」，供三种视图共用一份口径。

## 为什么要有这一层

WebUI 原先的问题是**同一件事散在四个页面**：一条 Issue 的评测结论在条目页、验证状态在
队列页、执行过程在任务页、历史在总览页。要看懂一条得来回跳，于是「它到底卡在哪」这个
最基本的问题反而最难回答。

这里把「条目现在走到哪一步」收敛成**一个纯函数**（:func:`stage_of`），三种视图
（漏斗 / 看板 / 泳道）都从同一份计数派生。**只有一份阶段口径**是刻意的：视图之间口径
不一致，比只有一种视图更糟。

## 为什么不新增一个「阶段」字段

阶段完全由 ``ItemStatus`` 推导，不落库。多存一个字段就有第二个真相，而状态机每次
推进都要记得同步它——漏一次就是两条数据互相矛盾。状态机本身已经够表达了。

## 卡住为什么单列一桶

``needs_manual`` / ``failed`` **不携带「死在第几阶段」**。硬猜会把泳道画错（看起来像
正常走到了某一步），比画不出更糟。所以它们进 ``stuck`` 桶，由详情页给出真实原因——
宁可不猜。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ..models import Category, ItemStatus, ItemType

#: 流水线的阶段（键, 中文标签）。顺序即时间顺序。
STAGES: tuple[tuple[str, str], ...] = (
    ("capture", "抓取"),
    ("evaluate", "评测"),
    ("verify", "验证"),
    ("judge", "裁决"),
    ("fix", "修复"),
    ("pr", "提 PR"),
)

#: 两个非阶段桶：``done``（已出结论，等开发者）与 ``stuck``（需人工）。
DONE = "done"
STUCK = "stuck"

#: 看板的列：只列**还能推动**的阶段，外加「需人工」。
#: 刻意不含 ``capture``（抓取是瞬时的，没有「待抓取」这种积压）与 ``done``（终点不是工作队列）。
KANBAN_STAGES: tuple[str, ...] = ("evaluate", "verify", "judge", "fix", "pr", STUCK)

#: 四条流水线（与 ``docs/DESIGN.md`` 的「四条流水线」一致）+ 未分类。
#: 分类（BUG/FEATURE）决定走哪条链，所以行按「类型 × 分类」划分，而不是只按类型。
LANES: tuple[tuple[str, str], ...] = (
    ("issue-bug", "Issue · BUG"),
    ("issue-feature", "Issue · FEATURE"),
    ("pr-bug", "PR · BUG"),
    ("pr-feature", "PR · FEATURE"),
    ("unknown", "未分类"),
)

#: 状态 → 当前所在阶段的序号。``len(STAGES)`` 表示「已走完全程」。
_CURSOR: dict[ItemStatus, int] = {
    ItemStatus.NEW: 1,
    ItemStatus.CLASSIFIED: 1,
    ItemStatus.EVALUATED: 2,
    ItemStatus.VERIFIER_READY: 2,
    ItemStatus.VERIFYING: 2,
    ItemStatus.QUEUED: 3,
    ItemStatus.VERIFIED: 3,
    ItemStatus.FIX_QUEUED: 4,
    ItemStatus.FIXING: 4,
    ItemStatus.PR_CREATED: len(STAGES),
    ItemStatus.LABELED: len(STAGES),
    ItemStatus.SKIPPED: len(STAGES),
}

#: 正在这一步「进行中」（而不是「排队等它」）的状态。
_IN_PROGRESS: frozenset[ItemStatus] = frozenset({ItemStatus.VERIFYING, ItemStatus.FIXING})

#: 卡住的状态——不携带「死在第几阶段」，见模块 docstring。
_STUCK: frozenset[ItemStatus] = frozenset({ItemStatus.NEEDS_MANUAL, ItemStatus.FAILED})

#: 状态 → 看板列（只对 ``KANBAN_STAGES`` 里的列有意义）。
_STAGE_OF: dict[ItemStatus, str] = {
    ItemStatus.NEW: "evaluate",
    ItemStatus.CLASSIFIED: "evaluate",
    ItemStatus.EVALUATED: "verify",
    ItemStatus.VERIFIER_READY: "verify",
    ItemStatus.VERIFYING: "verify",
    ItemStatus.QUEUED: "judge",
    ItemStatus.VERIFIED: "judge",
    ItemStatus.FIX_QUEUED: "fix",
    ItemStatus.FIXING: "fix",
    ItemStatus.PR_CREATED: "pr",
    ItemStatus.LABELED: DONE,
    ItemStatus.SKIPPED: DONE,
    ItemStatus.NEEDS_MANUAL: STUCK,
    ItemStatus.FAILED: STUCK,
}


def stage_of(status: ItemStatus) -> str:
    """条目当前所在的阶段键（``STAGES`` 的键 / ``done`` / ``stuck``）。"""
    return _STAGE_OF.get(status, STUCK)


def stage_label(key: str) -> str:
    """阶段键 → 中文标签（非阶段桶也认）。"""
    if key == DONE:
        return "已出结论"
    if key == STUCK:
        return "需人工"
    return dict(STAGES).get(key, key)


def stage_states(status: ItemStatus) -> list[str]:
    """六个阶段各自的呈现状态，供泳道画点。

    ``done`` 已完成 / ``active`` 进行中 / ``pending`` 排队等这一步 /
    ``waiting`` 还没轮到 / ``unknown`` 卡住了但不知道死在哪一步。
    """
    if status in _STUCK:
        # 不猜：见模块 docstring。前端据此画成灰色「?」。
        return ["unknown"] * len(STAGES)
    cursor = _CURSOR.get(status, 1)
    active = status in _IN_PROGRESS
    states: list[str] = []
    for index in range(len(STAGES)):
        if index < cursor:
            states.append("done")
        elif index == cursor:
            states.append("active" if active else "pending")
        else:
            states.append("waiting")
    return states


def lane_key(item_type: ItemType | str, category: Category | str) -> str:
    """条目属于哪条流水线（``LANES`` 的键）。"""
    kind = item_type.value if isinstance(item_type, ItemType) else str(item_type)
    cat = category.value if isinstance(category, Category) else str(category)
    if cat not in (Category.BUG.value, Category.FEATURE.value):
        # 还没评测出分类（或评测失败）→ 单列，否则会被误算进某条 BUG 链。
        return "unknown"
    side = "issue" if kind == ItemType.ISSUE.value else "pr"
    return f"{side}-{cat}"


def summarize(counts: Iterable[tuple[str, str, str, int]]) -> dict[str, Any]:
    """把 ``(item_type, category, status, count)`` 聚合成三视图共用的计数。

    输入直接来自一条 ``GROUP BY``（见 :meth:`Repository.pipeline_summary`）——不从
    「取回条目再在 Python 里数」派生：远程 PostgreSQL 上把几千条逐条取回只为数个数，
    仅首屏就是秒级。
    """
    totals: dict[str, int] = {key: 0 for key, _ in STAGES}
    totals[DONE] = 0
    totals[STUCK] = 0
    lanes: dict[str, dict[str, Any]] = {
        key: {"key": key, "label": label, "total": 0, "cells": {}}
        for key, label in LANES
    }

    for item_type, category, status, count in counts:
        lane = lane_key(item_type, category)
        bucket = stage_of(_as_status(status))
        lane_row = lanes.setdefault(
            lane, {"key": lane, "label": lane, "total": 0, "cells": {}}
        )
        lane_row["total"] += count
        lane_row["cells"][bucket] = lane_row["cells"].get(bucket, 0) + count
        totals[bucket] = totals.get(bucket, 0) + count

    total_items = sum(totals.values())

    funnel = [
        {
            "key": key,
            "label": label,
            "count": totals.get(key, 0),
            # 相对**首阶段**的比例：这是「还剩多少走到这一步」的直观比，
            # 而不是相对总数的存活率（两者在首阶段为 0 时都会退化，见下）。
            "pct": _pct(totals.get(key, 0), total_items),
        }
        for key, label in STAGES
    ]

    columns = [
        {
            "key": key,
            "label": stage_label(key),
            "total": totals.get(key, 0),
            "cells": [
                {
                    "lane": lane["key"],
                    "lane_label": lane["label"],
                    "count": lane["cells"].get(key, 0),
                }
                for lane in lanes.values()
            ],
        }
        for key in KANBAN_STAGES
    ]

    return {
        "stages": funnel,
        "columns": columns,
        "lanes": [
            {"key": lane["key"], "label": lane["label"], "total": lane["total"]}
            for lane in lanes.values()
        ],
        "totals": {
            "items": total_items,
            "stuck": totals.get(STUCK, 0),
            "done": totals.get(DONE, 0),
            "active": total_items - totals.get(STUCK, 0) - totals.get(DONE, 0),
        },
    }


def _pct(part: int, whole: int) -> int:
    """百分比（向下取整）；``whole`` 为 0 时返回 0，不抛除零。"""
    if whole <= 0:
        return 0
    return int(part * 100 // whole)


def _as_status(value: Any) -> ItemStatus:
    """把 ``str`` / ``ItemStatus`` 统一成 ``ItemStatus``；认不出的按「卡住」处理。

    为什么不抛：库里可能存着**旧版本写下、新版本已删掉**的状态值。首屏因为一个陌生
    字符串整页 500，比把它显示成「需人工」然后由人处置糟糕得多。
    """
    if isinstance(value, ItemStatus):
        return value
    try:
        return ItemStatus(str(value))
    except ValueError:
        return ItemStatus.NEEDS_MANUAL


def item_row(item: Any, *, stalled: bool = False) -> dict[str, Any]:
    """一条条目在泳道视图里的一行。"""
    status = _as_status(getattr(item, "status", None))
    return {
        "key": item.key,
        "repo": item.repo,
        "number": item.number,
        "title": getattr(item, "title", "") or "",
        "item_type": _enum_value(getattr(item, "item_type", "")),
        "category": _enum_value(getattr(item, "category", "")),
        "priority": _enum_value(getattr(item, "priority", "")),
        "status": status.value,
        "stage": stage_of(status),
        "states": stage_states(status),
        "stalled": stalled or status in _STUCK,
        "force_fix": bool(getattr(item, "force_fix", False)),
        "updated_at": _iso(getattr(item, "updated_at", None)),
    }


def swimlane_rows(items: Sequence[Any], *, limit: int = 60) -> list[dict[str, Any]]:
    """泳道视图的行：**卡住的排最前**，其余按最近更新。

    优先级由「用户打开页面最想先看到什么」决定：卡住的每多留一小时就多烧一小时的机会
    成本，而已出结论的条目放着不动没什么损失。
    """
    rows = [item_row(item) for item in items]
    # 一维定分组（卡住的在前），二维定组内时间序（新→旧）。
    rows.sort(key=lambda row: (0 if row["stalled"] else 1, _neg(row["updated_at"])))
    return rows[:limit]


def _neg(iso: str | None) -> float:
    """把 ISO 时间转成「可升序排列即等价于降序」的键（``None`` 排最后）。"""
    if not iso:
        return 0.0
    from datetime import datetime

    try:
        return -datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
