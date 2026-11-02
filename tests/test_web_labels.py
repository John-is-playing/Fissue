"""界面展示词汇（web/labels.py）的测试。

这些映射看着「只是文案」，但它们把**数据库/API 的 token** 挡在界面之外：用户看到的
是「需人工」而不是 ``needs_manual``。所以两条约束值得钉住：

1. 枚举的每个取值都必须有中文——漏一个，页面上就会突然冒出一个英文单词，
   而这种缺失没人会主动报告（大家只会觉得「这里怪怪的」）。
2. 未知值必须原样返回——上游新增取值时，宁可在界面上看到 ``future_value``，
   也不要被悄悄替换成「未知」，那会把排查线索抹掉。
"""

from __future__ import annotations

import pytest

from fissue.models import Action, FixOutcome, ItemStatus, VerifierKind
from fissue.web.labels import (
    STATUS_LABELS,
    action_label,
    fix_outcome_label,
    status_label,
    verdict_label,
    verifier_kind_label,
)


def test_every_status_has_a_chinese_label() -> None:
    """ItemStatus 的每个取值都要有标签——新增状态却忘了加词，页面上就会露英文。"""
    missing = [s.value for s in ItemStatus if s.value not in STATUS_LABELS]
    assert not missing, f"这些状态没有中文标签：{missing}"


@pytest.mark.parametrize(
    ("func", "enum_cls"),
    [
        (action_label, Action),
        (fix_outcome_label, FixOutcome),
        (verifier_kind_label, VerifierKind),
    ],
)
def test_every_enum_value_is_translated(func, enum_cls) -> None:  # noqa: ANN001
    """其余枚举同样不允许漏：翻出来的值必须与原始 token 不同（否则等于没翻）。"""
    untranslated = [m.value for m in enum_cls if func(m.value) == m.value]
    assert not untranslated, f"{enum_cls.__name__} 里这些取值没有翻译：{untranslated}"


def test_labels_accept_both_enum_and_string() -> None:
    """模板里传进来的可能是枚举也可能是字符串，两种都要认。"""
    assert status_label(ItemStatus.NEEDS_MANUAL) == "需人工"
    assert status_label("needs_manual") == "需人工"
    assert fix_outcome_label(FixOutcome.NEEDS_MANUAL) == "需人工"
    assert verifier_kind_label(VerifierKind.CHECKLIST) == "清单判定"


def test_unknown_value_passes_through_instead_of_hiding() -> None:
    """未知值原样返回——不猜、不吞。"""
    assert verdict_label("brand_new_verdict") == "brand_new_verdict"
    assert action_label("brand_new_action") == "brand_new_action"


def test_none_becomes_empty_string() -> None:
    """空值给空串，不要渲染出字面的 `None`。"""
    assert status_label(None) == ""
    assert verdict_label(None) == ""


def test_known_verdicts_are_translated() -> None:
    """批量结论的裁决取值见 models.BatchConclusion.verdict 的注释。"""
    for raw in ("merge", "reject", "needs_review", "fix", "skip"):
        assert verdict_label(raw) != raw, f"裁决 {raw} 没有翻译"
