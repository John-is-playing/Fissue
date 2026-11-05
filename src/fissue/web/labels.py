"""界面展示词汇：枚举值 → 中文。

为什么单独一个模块
------------------
这些映射是**给人看的词汇**，与调度/判定逻辑无关。放在这里而不是 ``models.py``，是因为
CLI 与 REST API 仍应输出英文原值（脚本要能稳定解析 ``needs_manual`` 这类 token），
只有面向人的 Web 界面才需要翻译。两套词汇分开住，就不会有人在「让 API 更友好」时
顺手改掉枚举值。

约定的两条
----------
* **未知值原样返回**，不猜、不吞。上游新增一个状态而这里没跟上时，页面上会显示英文原值——
  难看但真实；换成「未知」反而把信息抹掉了，排查时更难受。
* 每个 ``*_label`` 同时接受枚举与字符串（``ItemStatus.NEEDS_MANUAL`` 与 ``"needs_manual"``）。
  Jinja 里传进来的可能是任一种，让调用方去 ``.value`` 等于把转换负担摊到每个模板。
"""

from __future__ import annotations

from typing import Any

#: 条目在流水线中的状态。
STATUS_LABELS: dict[str, str] = {
    "new": "新建",
    "classified": "已分类",
    "evaluated": "已评测",
    "verifier_ready": "验证器就绪",
    "queued": "已排队",
    "verifying": "验证中",
    "verified": "已验证",
    "labeled": "已出结论",
    "fix_queued": "待修复",
    "fixing": "修复中",
    "pr_created": "已提 PR",
    "merged": "已合并",
    "needs_manual": "需人工",
    "skipped": "已跳过",
    "failed": "失败",
}

#: 批量结论的裁决结果。
VERDICT_LABELS: dict[str, str] = {
    "merge": "可合并",
    "reject": "建议关闭",
    "needs_review": "需人工复核",
    "fix": "建议修复",
    "skip": "跳过",
}

#: 评测给出的建议动作。
ACTION_LABELS: dict[str, str] = {
    "fix_now": "立即修复",
    "triage": "待分诊",
    "answer": "可直接答复",
    "backlog": "进待办",
    "close": "建议关闭",
    "needs_info": "缺信息",
}

#: 自动修复的结果。
FIX_OUTCOME_LABELS: dict[str, str] = {
    "success": "成功",
    "failed": "失败",
    "needs_manual": "需人工",
    "skipped": "已跳过",
}

#: 验证器的形态。
VERIFIER_KIND_LABELS: dict[str, str] = {
    "executable": "可执行测试",
    "checklist": "清单判定",
    "shell": "自定义脚本",
}

#: 条目在**平台上**的状态（`open` / `closed` 这类原值来自平台 API）。
#:
#: 为什么不翻译成「打开」：在 Issue/PR 的语境里 ``open`` 更接近「还开着 / 未关闭」，
#: 「打开」会让人误以为是个按钮动作。合并态单独有词，因为 PR 的「已合并」与「已关闭」
#: 是两种完全不同的结局，混成一个词会把「合进去了」说成「被关掉了」。
PLATFORM_STATE_LABELS: dict[str, str] = {
    "open": "未关闭",
    "closed": "已关闭",
    "merged": "已合并",
    "draft": "草稿",
}


def money(amount_usd: Any, rate: float = 0.0) -> str:
    """美元金额 → 人话。

    ``$0.0352`` 对多数人没有直觉、``约 ¥0.25`` 才有——这是"成本人话化"的全部意义。
    汇率只影响**展示**（库里 `cost_usd` 始终是美元），``rate <= 0`` 时就只显示美元。

    小额保留 4 位：一次评测可能只花 $0.0001，四舍五入到分全是 ¥0.00，等于没显示。
    """
    try:
        usd = float(amount_usd or 0.0)
    except (TypeError, ValueError):
        return "—"
    digits = 4 if abs(usd) < 1 else 2
    text = f"${usd:.{digits}f}"
    if rate and rate > 0:
        text += f"（约 ¥{usd * rate:.2f}）"
    return text


def _label(mapping: dict[str, str], value: object) -> str:
    """查表；未知值原样返回（不吞信息）。"""
    raw = getattr(value, "value", value)
    key = str(raw) if raw is not None else ""
    return mapping.get(key, key)


def status_label(status: object) -> str:
    """流水线状态 → 中文。"""
    return _label(STATUS_LABELS, status)


def verdict_label(verdict: object) -> str:
    """批量结论裁决 → 中文。"""
    return _label(VERDICT_LABELS, verdict)


def action_label(action: object) -> str:
    """建议动作 → 中文。"""
    return _label(ACTION_LABELS, action)


def fix_outcome_label(outcome: object) -> str:
    """修复结果 → 中文。"""
    return _label(FIX_OUTCOME_LABELS, outcome)


def verifier_kind_label(kind: object) -> str:
    """验证器形态 → 中文。"""
    return _label(VERIFIER_KIND_LABELS, kind)


def platform_state_label(state: object) -> str:
    """条目在平台上的状态 → 中文。"""
    return _label(PLATFORM_STATE_LABELS, state)
