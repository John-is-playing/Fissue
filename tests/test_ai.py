"""AI 层测试：JSON 提取、评分解析、分类预筛、优先级、评估器（打桩 LLM）。"""

from __future__ import annotations

import pytest

from fissue.ai.client import extract_json, clamp01, clamp_score
from fissue.ai.evaluator import (
    Evaluator,
    apply_rule_adjustments,
    compute_priority,
    keyword_classify,
    parse_action,
    parse_category,
    parse_evaluation,
    parse_labels,
    parse_scores,
    parse_spam,
)
from fissue.config import ClassificationConfig, FixPolicyConfig
from fissue.models import Action, Category, ItemType, Priority, RawItem


# ---------------------------------------------------------------------------
# JSON 提取
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('前言 {"a": {"b": "x{y}"}} 后语', {"a": {"b": "x{y}"}}),
        ('[{"a": 1}]', {"items": [{"a": 1}]}),
        ("not json at all", None),
        ("", None),
        ('{"s": "带 \\" 转义"}', {"s": '带 " 转义'}),
    ],
)
def test_extract_json(text: str, expected) -> None:
    assert extract_json(text) == expected


def test_clamp_helpers() -> None:
    assert clamp_score(150) == 100
    assert clamp_score(-3) == 0
    assert clamp_score(0.85) == 85          # 比例值自动放大
    assert clamp_score("abc", default=42) == 42
    assert clamp01("0.7") == pytest.approx(0.7)
    assert clamp01(None, default=0.3) == 0.3
    assert clamp01(5) == 1.0


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_scores_full() -> None:
    scores = parse_scores(
        {
            "scores": {
                "authenticity": {"score": 90, "reason": "r", "evidence": ["e"], "confidence": 0.8},
                "importance": 70,
                "difficulty": {"value": 20},
            }
        }
    )
    assert scores.authenticity.score == 90
    assert scores.importance.score == 70
    assert scores.difficulty.score == 20
    assert scores.pr_quality is None
    assert scores.authenticity.evidence == ["e"]


def test_parse_scores_compat_top_level() -> None:
    """模型偶尔不套 scores 层，分数直接放顶层。"""
    scores = parse_scores({"authenticity": 55, "importance": 65})
    assert scores.authenticity.score == 55
    assert scores.importance.score == 65


@pytest.mark.parametrize(
    "raw,expected",
    [("bug", Category.BUG), ("BUG", Category.BUG), ("crash", Category.BUG),
     ("feature", Category.FEATURE), ("enhancement", Category.FEATURE),
     ("whatever", Category.UNKNOWN), (None, Category.UNKNOWN)],
)
def test_parse_category(raw, expected: Category) -> None:
    assert parse_category(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("fix_now", Action.FIX_NOW), ("fix", Action.FIX_NOW), ("close", Action.CLOSE),
     ("invalid", Action.CLOSE), ("answer", Action.ANSWER), ("???", Action.TRIAGE)],
)
def test_parse_action(raw: str, expected: Action) -> None:
    assert parse_action(raw) == expected


def test_parse_labels_variants() -> None:
    assert parse_labels("bug, needs-info") == ["bug", "needs-info"]
    assert parse_labels("bug，needs info") == ["bug", "needs-info"]
    assert parse_labels(["Bug", "bug", "A B"]) == ["bug", "a-b"]
    assert parse_labels(None) == []


def test_parse_spam_normalizes() -> None:
    spam = parse_spam({"is_duplicate": True, "duplicate_of": ["12", "bad", 13], "reasons": "相似"})
    assert spam.is_duplicate is True
    assert spam.duplicate_of == [12, 13]
    assert spam.reasons == ["相似"]
    assert spam.suspicious is True


def test_parse_spam_infers_duplicate_from_numbers() -> None:
    """填了 duplicate_of 却忘了 is_duplicate，两个字段不该互相矛盾。"""
    spam = parse_spam({"duplicate_of": [2], "reasons": ["与 #2 重合"]})
    assert spam.is_duplicate is True
    assert spam.duplicate_of == [2]


def test_backfill_duplicate_of_from_reasons() -> None:
    """is_duplicate 为真但数组为空时，从 reasons 文本回填编号。"""
    from fissue.ai.evaluator import backfill_duplicate_of

    spam = parse_spam({
        "is_duplicate": True,
        "duplicate_of": [],
        "reasons": ["核心问题描述与 Issue #2 完全重合", "另见 #7"],
    })
    out = backfill_duplicate_of(spam, exclude=5)      # 自身是 #5
    assert out.duplicate_of == [2, 7]

    # 不误把自己算成重复对象
    self_ref = parse_spam({"is_duplicate": True, "reasons": ["与 #5 相同"]})
    assert backfill_duplicate_of(self_ref, exclude=5).duplicate_of == []

    # 已填了数组就不覆盖
    keep = parse_spam({"is_duplicate": True, "duplicate_of": [3], "reasons": ["与 #2 重合"]})
    assert backfill_duplicate_of(keep, exclude=9).duplicate_of == [3]

    # 不是重复就别乱填
    not_dup = parse_spam({"is_duplicate": False, "reasons": ["提到 #2"]})
    assert backfill_duplicate_of(not_dup, exclude=9).duplicate_of == []


def test_parse_evaluation_end_to_end() -> None:
    ev = parse_evaluation(
        {
            "category": "bug",
            "scores": {
                "authenticity": {"score": 0.9, "confidence": 0.8},
                "importance": {"score": 85},
                "difficulty": {"score": 25},
            },
            "labels_suggested": "bug,needs-info",
            "action": "fix",
            "summary": "确实崩了",
            "spam": {"is_spam": False},
        },
        model="m",
    )
    assert ev.category is Category.BUG
    assert ev.authenticity == 90
    assert ev.importance == 85
    assert ev.difficulty == 25
    assert ev.labels_suggested == ["bug", "needs-info"]
    assert ev.action is Action.FIX_NOW
    assert ev.summary == "确实崩了"


# ---------------------------------------------------------------------------
# 关键词预筛
# ---------------------------------------------------------------------------


def test_keyword_classify_bug_and_feature() -> None:
    cfg = ClassificationConfig()
    assert keyword_classify("程序崩溃 crash 报错", cfg).category is Category.BUG
    assert keyword_classify("建议新增 feature 支持", cfg).category is Category.FEATURE


def test_keyword_classify_ambiguous_and_empty() -> None:
    cfg = ClassificationConfig()
    mixed = keyword_classify("崩溃 但是建议新增支持", cfg)
    assert mixed.confident is False and mixed.category is Category.UNKNOWN
    assert keyword_classify("随便写点什么", cfg).confident is False


def test_keyword_classify_case_insensitive() -> None:
    assert keyword_classify("CRASH on startup", ClassificationConfig()).category is Category.BUG


# ---------------------------------------------------------------------------
# 优先级与规则兜底
# ---------------------------------------------------------------------------


def test_compute_priority_tiers() -> None:
    policy = FixPolicyConfig()          # tier1: diff<=40 & imp>=70；tier2: diff<=40 & imp>=0
    from fissue.models import DimensionScore, Evaluation, Scores

    def ev(diff: int, imp: int) -> Evaluation:
        return Evaluation(scores=Scores(difficulty=DimensionScore(score=diff), importance=DimensionScore(score=imp)))

    assert compute_priority(ev(20, 90), category=Category.BUG, policy=policy) is Priority.TIER1
    assert compute_priority(ev(20, 30), category=Category.BUG, policy=policy) is Priority.TIER2
    assert compute_priority(ev(80, 90), category=Category.BUG, policy=policy) is Priority.NONE
    # FEATURE 永不自动修复
    assert compute_priority(ev(20, 90), category=Category.FEATURE, policy=policy) is Priority.NONE


def test_compute_priority_respects_only_issues() -> None:
    """PR 不该被标 tier1/tier2（默认 only_issues=True），否则报告给出误导性的 fix_now。"""
    from fissue.models import DimensionScore, Evaluation, Scores

    def ev(diff: int, imp: int) -> Evaluation:
        return Evaluation(scores=Scores(difficulty=DimensionScore(score=diff), importance=DimensionScore(score=imp)))

    policy = FixPolicyConfig()
    assert compute_priority(
        ev(20, 90), category=Category.BUG, policy=policy, item_type=ItemType.ISSUE
    ) is Priority.TIER1
    assert compute_priority(
        ev(20, 90), category=Category.BUG, policy=policy, item_type=ItemType.PR
    ) is Priority.NONE
    # only_issues=False 时 PR 也可入选（保留可配置语义）
    policy.only_issues = False
    assert compute_priority(
        ev(20, 90), category=Category.BUG, policy=policy, item_type=ItemType.PR
    ) is Priority.TIER1


def test_compute_priority_blocks_suspicious_items() -> None:
    """疑似重复/刷量的条目不得进入自动修复（DESIGN 的策略闸门）。

    否则会自动为一条「已识别的重复 Issue」生成第二份补丁——例如 demo 里
    #5 被判为 #2 的重复后，仍被排进修复队列。
    """
    from fissue.models import DimensionScore, Evaluation, Scores, SpamSignal

    def ev(diff: int, imp: int, **kw) -> Evaluation:
        return Evaluation(
            scores=Scores(difficulty=DimensionScore(score=diff), importance=DimensionScore(score=imp)),
            **kw,
        )

    policy = FixPolicyConfig()
    # 难度低 + 重要性高，正常本该 tier1
    assert compute_priority(
        ev(20, 90), category=Category.BUG, policy=policy, item_type=ItemType.ISSUE
    ) is Priority.TIER1

    # 判为重复 → 不修
    assert compute_priority(
        ev(20, 90, spam=SpamSignal(is_duplicate=True, duplicate_of=[2])),
        category=Category.BUG, policy=policy, item_type=ItemType.ISSUE,
    ) is Priority.NONE

    # 判为刷量 → 不修
    assert compute_priority(
        ev(20, 90, spam=SpamSignal(is_spam=True)),
        category=Category.BUG, policy=policy, item_type=ItemType.ISSUE,
    ) is Priority.NONE


def test_rule_adjustments_block_low_authenticity(settings, sample_issue: RawItem) -> None:
    from fissue.models import Action, DimensionScore, Evaluation, Scores

    ev = Evaluation(
        scores=Scores(authenticity=DimensionScore(score=10), importance=DimensionScore(score=90)),
        action=Action.FIX_NOW,
    )
    out = apply_rule_adjustments(ev, sample_issue, cfg=settings.evaluation)
    assert out.action is Action.TRIAGE
    assert out.spam.reasons


def test_rule_adjustments_merged_pr_no_fix(settings, sample_pr: RawItem) -> None:
    from fissue.models import Evaluation

    sample_pr.merged = True
    ev = Evaluation(priority=Priority.TIER1)
    out = apply_rule_adjustments(ev, sample_pr, cfg=settings.evaluation)
    assert out.priority is Priority.NONE


# ---------------------------------------------------------------------------
# 评估器（打桩 LLM）
# ---------------------------------------------------------------------------


async def test_evaluator_uses_keywords_and_skips_llm(settings, repo, sample_issue, stub_llm) -> None:
    """一边倒的关键词命中时不应调用 LLM（省钱）。"""
    evaluator = Evaluator(stub_llm, repo, settings)
    category, conf, reason = await evaluator.classify(sample_issue)
    assert category is Category.BUG
    assert stub_llm.calls == []


async def test_evaluator_falls_back_to_llm(settings, repo, stub_llm) -> None:
    item = RawItem(platform=sample_platform(), repo="a/b", number=1, item_type=ItemType.ISSUE,
                   title="随便", body="没有关键词")
    stub_llm.responses = [{"category": "feature", "confidence": 0.9, "reason": "新增能力"}]
    evaluator = Evaluator(stub_llm, repo, settings)
    category, conf, _ = await evaluator.classify(item)
    assert category is Category.FEATURE
    assert len(stub_llm.calls) == 1


async def test_evaluator_evaluate_saves_and_computes_priority(settings, repo, sample_issue, stub_llm) -> None:
    from fissue.store.repository import Repository

    rid = repo.ensure_repo(__import__("fissue.models", fromlist=["RepoRef"]).RepoRef(
        platform=sample_issue.platform, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)

    stub_llm.responses = [
        {   # evaluate
            "category": "bug",
            "scores": {
                "authenticity": {"score": 92},
                "importance": {"score": 88},
                "feasibility": {"score": 80},
                "difficulty": {"score": 20},
            },
            "labels_suggested": ["bug"],
            "action": "fix_now",
            "summary": "真实缺陷",
        }
    ]
    evaluator = Evaluator(stub_llm, repo, settings)
    ev = await evaluator.evaluate(sample_issue)
    assert ev.priority is Priority.TIER1
    assert ev.importance == 88

    saved = repo.latest_evaluation(sample_issue.key)
    assert saved is not None and saved.importance == 88


async def test_evaluator_conclude_batch(settings, repo, stub_llm) -> None:
    stub_llm.responses = [
        {
            "results": [
                {"key": "k1", "verdict": "fix", "labels": ["reproduced"], "priority": "tier1",
                 "reason": "低难度高重要性", "confidence": 0.9},
                {"key": "k2", "verdict": "skip", "labels": [], "priority": "none",
                 "reason": "验证器不可靠", "confidence": 0.6},
            ]
        }
    ]
    evaluator = Evaluator(stub_llm, repo, settings)
    out = await evaluator.conclude_batch(queue_kind="verify", entries=[{"key": "k1"}, {"key": "k2"}])
    assert [r["key"] for r in out] == ["k1", "k2"]
    assert out[0]["priority"] is Priority.TIER1
    assert out[0]["verdict"] == "fix"


async def test_conclude_batch_ignores_unknown_keys(settings, repo, stub_llm) -> None:
    stub_llm.responses = [{"results": [{"key": "not-in-batch", "verdict": "fix"}]}]
    evaluator = Evaluator(stub_llm, repo, settings)
    out = await evaluator.conclude_batch(queue_kind="verify", entries=[{"key": "k1"}])
    assert out == []


async def test_conclude_batch_handles_bad_shape(settings, repo, stub_llm) -> None:
    stub_llm.responses = [{"unexpected": True}]
    evaluator = Evaluator(stub_llm, repo, settings)
    assert await evaluator.conclude_batch(queue_kind="verify", entries=[{"key": "k1"}]) == []


def sample_platform():
    from fissue.models import Platform

    return Platform.GITHUB
