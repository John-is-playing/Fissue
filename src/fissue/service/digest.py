"""每日摘要（日报）：今天修好了什么、花了多少钱、还剩什么卡着。

为什么值得单独做
----------------
这个产品的价值是**异步**的：它在你睡觉时抓取、评测、验证、提 PR。没有一个"今天发生了什么"
的汇总，用户只能自己去翻五个页面拼出结论——而"没被看见的价值等于没有价值"。

口径
----
* 窗口默认是 **UTC 自然日**（与 `usage_today`、每日预算同一口径），可用 ``since`` 覆盖；
* 成本按窗口内实际调用累加（**失败调用也算**：模型被调用了就产生费用）；
* 「卡住」只统计**当前**状态，不看窗口——它是"现在需要你处理的事"，不是"今天新出现的"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..logging_setup import get_logger
from ..models import BLOCK_REASON_TEXT
from ..store.repository import Repository
from ..store.tables import (
    EvaluationRow,
    FixAttemptRow,
    ItemRow,
    LLMUsageRow,
    VerifierRunRow,
)
from ..web.labels import money

log = get_logger(__name__)


@dataclass(frozen=True)
class Digest:
    """一份日报的全部数字。字段都是**算好的**，渲染层不再查库。"""

    since: datetime
    until: datetime
    new_items: int = 0
    evaluated: int = 0
    verified: int = 0
    fix_success: int = 0
    fix_needs_manual: int = 0
    fix_failed: int = 0
    merged: int = 0
    needs_manual: int = 0
    #: 卡住原因 → 条数（降序）
    blocked: list[tuple[str, int]] = field(default_factory=list)
    #: 本次窗口内的花费（美元）
    cost_usd: float = 0.0
    month_cost_usd: float = 0.0
    month_forecast_usd: float = 0.0
    #: 窗口内被合并的条目（最多 5 条，给日报当"成果"展示）
    merged_items: list[tuple[str, str]] = field(default_factory=list)


def _day_start(now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)


def build_digest(
    repo: Repository,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Digest:
    """聚合一份日报。只读，不改任何状态。"""
    start = since or _day_start()
    end = until or datetime.now(timezone.utc)
    if end <= start:
        # 传了个反窗口就是调用方的 bug；别静默产出全 0 的日报（那看起来像"今天没活干"）
        start = end - timedelta(days=1)

    with repo.db.session() as session:
        new_items = int(
            session.scalar(
                select(func.count()).select_from(ItemRow).where(ItemRow.created_at >= start)
            )
            or 0
        )
        evaluated = int(
            session.scalar(
                select(func.count())
                .select_from(EvaluationRow)
                .where(EvaluationRow.created_at >= start)
            )
            or 0
        )
        verified = int(
            session.scalar(
                select(func.count())
                .select_from(VerifierRunRow)
                .where(VerifierRunRow.created_at >= start)
            )
            or 0
        )

        outcomes = dict(
            session.execute(
                select(FixAttemptRow.outcome, func.count())
                .where(FixAttemptRow.created_at >= start)
                .group_by(FixAttemptRow.outcome)
            ).all()
        )

        merged = int(
            session.scalar(
                select(func.count()).select_from(ItemRow).where(ItemRow.merged_at >= start)
            )
            or 0
        )
        merged_items = [
            (str(key), str(title or ""))
            for key, title in session.execute(
                select(ItemRow.key, ItemRow.title)
                .where(ItemRow.merged_at >= start)
                .order_by(ItemRow.merged_at.desc())
                .limit(5)
            ).all()
        ]

        blocked = [
            (str(reason or "unknown"), int(count))
            for reason, count in session.execute(
                select(ItemRow.block_reason, func.count())
                .where(ItemRow.status == "needs_manual")
                .group_by(ItemRow.block_reason)
                .order_by(func.count().desc())
            ).all()
        ]

        cost = float(
            session.scalar(
                select(func.coalesce(func.sum(LLMUsageRow.cost_usd), 0.0)).where(
                    LLMUsageRow.created_at >= start
                )
            )
            or 0.0
        )

    month = repo.usage_month()
    return Digest(
        since=start,
        until=end,
        new_items=new_items,
        evaluated=evaluated,
        verified=verified,
        fix_success=int(outcomes.get("success", 0)),
        fix_needs_manual=int(outcomes.get("needs_manual", 0)),
        fix_failed=int(outcomes.get("failed", 0)),
        merged=merged,
        needs_manual=sum(count for _, count in blocked),
        blocked=blocked,
        cost_usd=cost,
        month_cost_usd=month["cost_usd"],
        month_forecast_usd=month["forecast_usd"],
        merged_items=merged_items,
    )


def render_markdown(digest: Digest, *, usd_to_cny: float = 0.0, title: str = "") -> str:
    """渲染成人看的日报（Markdown）。

    刻意**不列全部条目**：日报的用途是"要不要现在去处理"，不是替代条目列表。
    真要看清单，正文里给链接。
    """
    head = title or "Fissue 日报"
    lines = [f"## {head}", ""]
    lines.append(
        f"窗口：{digest.since.strftime('%Y-%m-%d %H:%M')} → "
        f"{digest.until.strftime('%Y-%m-%d %H:%M')}（UTC）"
    )
    lines.append("")

    lines.append("### 今天的产出")
    lines.append(f"- 新增条目 **{digest.new_items}**")
    lines.append(f"- 评测 **{digest.evaluated}** 条 · 验证 **{digest.verified}** 次")
    if digest.fix_success or digest.fix_needs_manual or digest.fix_failed:
        lines.append(
            f"- 修复：成功 **{digest.fix_success}** · 转人工 {digest.fix_needs_manual}"
            f" · 失败 {digest.fix_failed}"
        )
    if digest.merged:
        lines.append(f"- **合并 {digest.merged} 个 PR**（真正修好的）")
        for key, name in digest.merged_items:
            lines.append(f"  - `{key}` {name}")

    lines.append("")
    lines.append("### 花费")
    lines.append(f"- 本窗口 {money(digest.cost_usd, usd_to_cny)}")
    lines.append(
        f"- 本月已花 {money(digest.month_cost_usd, usd_to_cny)}"
        f"，按当前速度预计 {money(digest.month_forecast_usd, usd_to_cny)}"
    )

    lines.append("")
    lines.append("### 需要你处理")
    if digest.needs_manual:
        lines.append(f"共 **{digest.needs_manual}** 条卡住，按原因分组：")
        for reason, count in digest.blocked:
            label, next_step = BLOCK_REASON_TEXT.get(reason, BLOCK_REASON_TEXT["unknown"])
            lines.append(f"- **{label}** × {count} —— {next_step}")
    else:
        lines.append("- 没有卡住的条目。")
    lines.append("")
    return "\n".join(lines)
