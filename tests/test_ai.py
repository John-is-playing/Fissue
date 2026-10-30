"""AI 层测试：JSON 提取、评分解析、分类预筛、优先级、评估器（打桩 LLM）。"""

from __future__ import annotations

import pytest

from fissue.ai.client import clamp01, clamp_score, extract_json
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
from fissue.models import Action, Category, DimensionScore, ItemType, Priority, RawItem

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


def _dim(score: DimensionScore | None) -> DimensionScore:
    """取某一维的分数对象——断言它确实被解析出来了，省得每条断言前都写一遍 None 检查。"""
    assert score is not None, "该维度应当被解析出来"
    return score


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
    assert _dim(scores.authenticity).score == 90
    assert _dim(scores.importance).score == 70
    assert _dim(scores.difficulty).score == 20
    assert scores.pr_quality is None
    assert _dim(scores.authenticity).evidence == ["e"]


def test_parse_scores_compat_top_level() -> None:
    """模型偶尔不套 scores 层，分数直接放顶层。"""
    scores = parse_scores({"authenticity": 55, "importance": 65})
    assert _dim(scores.authenticity).score == 55
    assert _dim(scores.importance).score == 65


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


def test_compute_priority_dual_strategy_takes_stricter_tier() -> None:
    """dual 策略：难度与重要性各判一档，取更严的那一档。

    当 tier1 的难度门槛比 tier2 更紧时，难度这一维会真正参与分档——
    这是 ``importance`` 策略做不到的（它把难度当单一硬门槛）。
    """
    from fissue.models import DimensionScore, Evaluation, Scores

    def ev(diff: int, imp: int) -> Evaluation:
        return Evaluation(scores=Scores(difficulty=DimensionScore(score=diff),
                                        importance=DimensionScore(score=imp)))

    policy = FixPolicyConfig(tier_strategy="dual")
    policy.tier1.max_difficulty, policy.tier1.min_importance = 20, 70
    policy.tier2.max_difficulty, policy.tier2.min_importance = 40, 40

    # 难度够 tier1、重要性只够 tier2 → 取更严的 tier2（importance 策略会给 tier1）
    assert compute_priority(ev(20, 50), category=Category.BUG, policy=policy) is Priority.TIER2
    # 两维都够 tier1
    assert compute_priority(ev(20, 90), category=Category.BUG, policy=policy) is Priority.TIER1
    # 难度超 tier1 但仍在 tier2 内、重要性够 tier1 → 取更严的 tier2
    assert compute_priority(ev(30, 90), category=Category.BUG, policy=policy) is Priority.TIER2
    # 难度超出 tier2 → none
    assert compute_priority(ev(50, 90), category=Category.BUG, policy=policy) is Priority.NONE
    # 重要性低于 tier2 门槛 → none
    assert compute_priority(ev(10, 10), category=Category.BUG, policy=policy) is Priority.NONE


def test_percentile_strategy_ranks_within_batch() -> None:
    """percentile 策略：档位由**批内排名**定，不再看绝对重要性分数。

    实测里模型的 importance 密集落在 62~82、而绝对门槛 80 正压在右端，
    单次评分抖动就能让同一条目跨线；分位让跨运行稳定的是比例。
    """
    from fissue.ai.evaluator import resolve_percentile_tiers

    policy = FixPolicyConfig(tier_strategy="percentile")
    policy.tier1_share = 0.5
    policy.percentile_min_batch = 3
    policy.tier1.max_difficulty = policy.tier2.max_difficulty = 40

    rows = [
        ("a", 10, 82),
        ("b", 10, 78),
        ("c", 10, 74),
        ("d", 10, 66),
        ("e", 10, 62),
    ]
    tiers = resolve_percentile_tiers(rows, policy)
    # ceil(5 * 0.5) = 3 条进 tier1，其余 tier2
    assert tiers == {
        "a": Priority.TIER1,
        "b": Priority.TIER1,
        "c": Priority.TIER1,
        "d": Priority.TIER2,
        "e": Priority.TIER2,
    }

    # 同样这 5 条整体平移（分数全降 30），档位**一个都不变**——
    # 这正是绝对阈值做不到的：门槛 80 下它们会全部落到 tier2。
    shifted = [(k, d, imp - 30) for k, d, imp in rows]
    assert resolve_percentile_tiers(shifted, policy) == tiers

    # 难度仍是硬门槛：超出 tier2 的候选不参与，tier1 的难度门槛也不放宽
    hard = [("a", 10, 90), ("b", 10, 85), ("c", 50, 99), ("d", 10, 70)]
    got = resolve_percentile_tiers(hard, policy)
    assert "c" not in got, "难度超 tier2 门槛的条目不该被分位捞回来"
    assert got["a"] is Priority.TIER1


def test_percentile_strategy_small_batch_falls_back_to_conservative_tier() -> None:
    """候选不足 min_batch 时不算分位，各条保留单条口径的保守值 tier2。"""
    from fissue.ai.evaluator import resolve_percentile_tiers
    from fissue.models import DimensionScore, Evaluation, Scores

    policy = FixPolicyConfig(tier_strategy="percentile")
    policy.percentile_min_batch = 3
    policy.tier1.max_difficulty = policy.tier2.max_difficulty = 40

    assert resolve_percentile_tiers([("a", 10, 99), ("b", 10, 98)], policy) == {}

    def ev(diff: int, imp: int) -> Evaluation:
        return Evaluation(scores=Scores(difficulty=DimensionScore(score=diff),
                                        importance=DimensionScore(score=imp)))

    # 单条调用没有批内参照 → tier2（宁可先标低优先，也不吹成 tier1）
    assert compute_priority(ev(10, 99), category=Category.BUG, policy=policy) is Priority.TIER2
    # 难度超门槛仍然是 none：分位只改「重要性怎么用」，不放宽难度
    assert compute_priority(ev(80, 99), category=Category.BUG, policy=policy) is Priority.NONE


def test_fix_policy_percentile_params_are_validated() -> None:
    """分位参数写错要在加载期挡下，而不是跑起来才发现档位没在分。"""
    import pytest

    from fissue.errors import ConfigError

    assert FixPolicyConfig(tier_strategy="percentile").tier_strategy == "percentile"
    for bad in (0.0, 1.0, 1.5, -0.2):
        with pytest.raises(ConfigError):
            FixPolicyConfig(tier1_share=bad)
    with pytest.raises(ConfigError):
        FixPolicyConfig(percentile_min_batch=0)


def test_compute_priority_custom_strategy_uses_user_function(tmp_path, monkeypatch) -> None:
    """custom 策略由用户函数判定，且重要性门槛参数**不生效**。"""
    from fissue.models import DimensionScore, Evaluation, Scores

    (tmp_path / "my_tier_policy.py").write_text(
        "from fissue.models import Priority\n"
        "def tier_of(difficulty, importance, policy):\n"
        "    if difficulty <= 30:\n"
        "        return Priority.TIER1\n"
        "    if difficulty <= 60:\n"
        "        return 'tier2'\n"
        "    return 'none'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    def ev(diff: int, imp: int) -> Evaluation:
        return Evaluation(scores=Scores(difficulty=DimensionScore(score=diff),
                                        importance=DimensionScore(score=imp)))

    policy = FixPolicyConfig(tier_strategy="custom", custom_tier="my_tier_policy:tier_of")
    # 门槛抬到极值：若它生效，下面 diff=20 的条目不可能被判 tier1
    policy.tier1.min_importance = 99
    policy.tier2.min_importance = 99

    assert compute_priority(ev(20, 0), category=Category.BUG, policy=policy) is Priority.TIER1
    assert compute_priority(ev(45, 0), category=Category.BUG, policy=policy) is Priority.TIER2
    assert compute_priority(ev(80, 0), category=Category.BUG, policy=policy) is Priority.NONE


def test_compute_priority_custom_strategy_falls_back_when_unavailable() -> None:
    """custom 函数加载失败时回退默认策略，而不是「配错就静默不修」。"""
    from fissue.models import DimensionScore, Evaluation, Scores

    def ev(diff: int, imp: int) -> Evaluation:
        return Evaluation(scores=Scores(difficulty=DimensionScore(score=diff),
                                        importance=DimensionScore(score=imp)))

    policy = FixPolicyConfig(tier_strategy="custom", custom_tier="no.such.module:tier_of")
    assert compute_priority(ev(20, 90), category=Category.BUG, policy=policy) is Priority.TIER1
    assert compute_priority(ev(20, 30), category=Category.BUG, policy=policy) is Priority.TIER2
    assert compute_priority(ev(90, 90), category=Category.BUG, policy=policy) is Priority.NONE


def test_compute_priority_custom_strategy_handles_bad_return() -> None:
    """用户函数抛错或返回无法识别的值时，不得让整条流水线崩掉。"""
    from fissue.models import DimensionScore, Evaluation, Scores

    def ev(diff: int, imp: int) -> Evaluation:
        return Evaluation(scores=Scores(difficulty=DimensionScore(score=diff),
                                        importance=DimensionScore(score=imp)))

    # 模块名指向本测试文件（可导入），属性不存在 → 加载失败 → 回退默认
    policy = FixPolicyConfig(tier_strategy="custom", custom_tier="tests.test_ai:not_a_function")
    assert compute_priority(ev(20, 90), category=Category.BUG, policy=policy) is Priority.TIER1


def test_fix_policy_rejects_invalid_tier_strategy() -> None:
    """非法策略名与「custom 却没给函数」都要在配置期就报错。"""
    from fissue.config import ConfigError as CfgErr

    with pytest.raises(CfgErr):
        FixPolicyConfig(tier_strategy="bogus")
    with pytest.raises(CfgErr):
        FixPolicyConfig(tier_strategy="custom")


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


def test_evaluation_prompt_distinguishes_design_preference_from_defect() -> None:
    """提示词必须把「行为/API 设计偏好变更」与「真缺陷」分开。

    回归（ratekit #9 实测）：Issue 提议「parse_amount 解析失败返回 0 而非抛异常」，
    这不是缺陷而是 API 设计偏好，却被判 authenticity=90。authenticity 若衡量的只是
    「诉求是否合理」，就会把它推成高真实性、甚至 fix_now；必须明确它衡量的是
    「这是不是一个真实存在的问题」。
    """
    from fissue.ai.prompts import evaluation_prompt
    from fissue.models import ItemType, Platform, RawItem

    item = RawItem(
        platform=Platform.GITHUB, repo="x/y", number=9, item_type=ItemType.ISSUE,
        title="parse_amount 解析非法输入时应该返回 0 而不是抛异常",
        body="表单用户乱填就 500 了，建议解析失败返回 0。",
    )
    user = evaluation_prompt(item)[1]["content"]

    # 维度说明里点明 authenticity 衡量的是「是否存在真实问题」
    assert "这是不是一个真实存在的问题/需求" in user
    assert "不是" in user and "这个诉求是否合理" in user
    # 打分要求里给出可执行判据：设计偏好变更 → authenticity ≤40、category=feature
    assert "行为/API 设计偏好变更 ≠ 缺陷" in user
    assert "≤40" in user
    assert "category 取 feature" in user
    # 反向也要说清：不符合自身契约的才是缺陷，避免把真 Bug 也压成低真实性
    assert "不符合其自身文档/契约/常识预期" in user
    # 范围必须限定在「替换现有行为」：否则「新增功能」请求会被一起误压。
    # 回归（重评实测）：#7「支持按小时计费」曾被这条规则误伤，真实性 95 → 35。
    assert "仅限" in user and "替换现有行为" in user
    assert "新增功能不属此类" in user
    assert "不要**因为它是「新做法」就压低真实性" in user


def test_evaluation_prompt_does_not_suppress_new_feature_requests() -> None:
    """「新增功能」是对新能力的真实需求，不该被设计偏好规则压低真实性。

    回归（重评实测）：加了「设计偏好变更 ≠ 缺陷」后，#7「支持按小时计费与自定义
    计费周期」真实性从 95 掉到 35，reason 写着「属于明确的功能增强与 API 设计偏好
    变更」——把「新需求」误当成了「偏好变更」。提示词必须显式排除这一类。
    """
    from fissue.ai.prompts import evaluation_prompt
    from fissue.models import ItemType, Platform, RawItem

    item = RawItem(
        platform=Platform.GITHUB, repo="x/y", number=7, item_type=ItemType.ISSUE,
        title="支持按小时计费与自定义计费周期",
        body="目前只支持按天分摊，希望增加按小时/周/月计费。",
    )
    user = evaluation_prompt(item)[1]["content"]

    assert "目前不支持 X，希望增加 X" in user
    assert "是对新能力的真实需求" in user
    assert "清晰可实现的合理需求应给高分" in user


def test_evaluation_prompt_pr_variant_keeps_guidance() -> None:
    """PR 分支同样带上该指引（dims 按 PR/Issue 分支构建，别只改了一侧）。"""
    from fissue.ai.prompts import evaluation_prompt
    from fissue.models import ItemType, Platform, RawItem

    pr = RawItem(platform=Platform.GITHUB, repo="x/y", number=7, item_type=ItemType.PR,
                 title="fix: 调整默认返回值为 0", body="d")
    user = evaluation_prompt(pr)[1]["content"]

    assert "行为/API 设计偏好变更 ≠ 缺陷" in user
    assert "pr_quality" in user          # PR 专属维度仍在
    assert "≤40" in user


# ---------------------------------------------------------------------------
# 修复 Agent 的单步提示（重写：验收标准前置 + 逼出判断）
# ---------------------------------------------------------------------------


def _an_issue() -> RawItem:
    from fissue.models import ItemType, Platform, RawItem

    return RawItem(platform=Platform.GITHUB, repo="x/y", number=1,
                   item_type=ItemType.ISSUE, title="t", body="b")


def _fix_step(**over) -> str:
    """取单步提示的 user 正文（各用例共用）。"""
    from fissue.ai.prompts import fix_agent_step_prompt

    kwargs: dict = dict(
        repo_context="（仓库上下文占位）",
        verifier_command="python -m pytest tests/test_reproduce.py -q",
        last_test_output=None,
        round_index=1,
        max_rounds=12,
        read_files=None,
        verifier_files=None,
    )
    kwargs.update(over)
    return fix_agent_step_prompt(_an_issue(), **kwargs)[1]["content"]


def test_fix_step_puts_acceptance_criteria_up_front() -> None:
    """验证器自带的测试文件必须**第 1 轮**就摆在提示里，且排在仓库上下文之前。

    实测：模型宁可反复读仓库文件，也不主动去读验证器自带的那份测试——而它正是
    「要满足什么」的规格。所以每轮直接给，别指望它「想起来去读」。
    """
    text = _fix_step(
        verifier_files={"tests/test_reproduce.py": "def test_x():\n    assert 1 == 2\n"}
    )

    assert "验收标准" in text
    assert "tests/test_reproduce.py" in text
    assert "assert 1 == 2" in text, "规格内容本身也要给到"
    assert text.index("验收标准") < text.index("仓库上下文"), "先看目标，再看素材"


def test_fix_step_tells_the_model_to_write_when_it_knows_enough() -> None:
    """把「重读同一批＝耗轮次」「不确定就先写一版」这两条纪律写进提示。

    实测 12 轮只读不写就卡在这里：它不缺信息、也不缺动作，缺的是「够了就动手」。
    """
    text = _fix_step()

    assert "只读你还没读过的" in text
    assert "先写一版" in text
    assert "give_up" in text


def test_fix_agent_uses_its_own_system_prompt() -> None:
    """修复 Agent 用独立系统提示词：`SYSTEM_FIX` 还被 PR 文案生成共用，不能串。"""
    from fissue.ai.prompts import SYSTEM_FIX, SYSTEM_FIX_AGENT, fix_agent_step_prompt

    msgs = fix_agent_step_prompt(
        _an_issue(), repo_context="x", verifier_command="pytest",
        last_test_output=None, round_index=1, max_rounds=12,
    )

    assert msgs[0]["content"] == SYSTEM_FIX_AGENT
    assert msgs[0]["content"] != SYSTEM_FIX
    assert "轮次预算" in msgs[0]["content"]


def sample_platform():
    from fissue.models import Platform

    return Platform.GITHUB


# ---------------------------------------------------------------------------
# chat_json：解析失败要带原始输出重试（推理模型截断场景）
# ---------------------------------------------------------------------------


async def test_chat_json_retries_once_when_content_unparseable() -> None:
    """首次返回空/半截 JSON → 带原始输出重问一次，而不是直接认输返回空 dict。

    实测根因：推理模型先用 token 写 reasoning_content，max_tokens 被吃光后
    finish_reason=length、content 为空（实测长度 0）。这类截断是偶发的，
    原样重问一次通常就能拿到完整对象；此前直接返回空 dict，等于让上层白跑一轮
    （多余的重生成 / 重试），envkit 实测 #8 花 4 轮、#12 花 3 轮。
    """
    from fissue.ai.client import LLMClient, LLMResponse, Usage
    from fissue.config import LLMConfig

    client = LLMClient(LLMConfig(api_key="k"))
    seen: list[list] = []

    async def fake_chat(messages, **kwargs):
        seen.append(list(messages))
        if len(seen) == 1:
            # 第一次：被长度截断，content 为空
            return LLMResponse(content="", model="m", usage=Usage(),
                               raw={"choices": [{"finish_reason": "length"}]})
        return LLMResponse(content='{"category": "bug"}', model="m", usage=Usage(),
                           raw={"choices": [{"finish_reason": "stop"}]})

    client.chat = fake_chat  # type: ignore[method-assign]
    data, usage = await client.chat_json([{"role": "user", "content": "x"}], purpose="test")

    assert data == {"category": "bug"}
    assert len(seen) == 2                       # 确实重试了一次
    # 第二轮把上一轮的原始输出与「别再截断」的指令喂回去了
    assert len(seen[1]) > len(seen[0])
    assert "JSON" in seen[1][-1]["content"]


async def test_chat_json_handles_a_tool_call_response(caplog) -> None:
    """模型把回复放进 `tool_calls` 时：既要重试，也要把工具名与参数报出来。

    回归：自维护实测（Fissue #1）修复 Agent 收到 `finish_reason=tool_calls`、content 长度 0，
    日志里只有一句「无法解析为 JSON」，运维无法判断是端点行为还是模型自己的选择；而重试提示
    也没让它别调工具，于是第二轮大概率再撞一次。
    """
    import logging

    from fissue.ai.client import LLMClient, LLMResponse, Usage
    from fissue.config import LLMConfig

    client = LLMClient(LLMConfig(api_key="k"))
    seen: list[list] = []

    async def fake_chat(messages, **kwargs):
        seen.append(list(messages))
        if len(seen) == 1:
            return LLMResponse(
                content="",
                model="m",
                usage=Usage(),
                raw={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path": "a.py"}',
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        return LLMResponse(content='{"action": "read"}', model="m", usage=Usage(),
                           raw={"choices": [{"finish_reason": "stop"}]})

    client.chat = fake_chat  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        data, _usage = await client.chat_json([{"role": "user", "content": "x"}], purpose="test")

    assert data == {"action": "read"}
    assert len(seen) == 2, "遇到 tool_calls 也要重试一次"
    assert "不要调用工具" in seen[1][-1]["content"], "重试提示必须明确要求纯 JSON"
    assert "工具调用" in caplog.text, "要把「这是工具调用」说清楚"
    assert "read_file" in caplog.text, "要报出工具名"
    assert "a.py" in caplog.text, "要报出参数（便于判断是不是端点切模板搞的）"


async def test_chat_json_gives_default_after_all_attempts_fail() -> None:
    """每轮都解析不出来 → 仍返回 default 且不抛异常（上层可降级继续）。"""
    from fissue.ai.client import LLMClient, LLMResponse, Usage
    from fissue.config import LLMConfig

    client = LLMClient(LLMConfig(api_key="k"))
    calls = {"n": 0}

    async def fake_chat(messages, **kwargs):
        calls["n"] += 1
        return LLMResponse(content="完全不是 JSON", model="m", usage=Usage(),
                           raw={"choices": [{"finish_reason": "stop"}]})

    client.chat = fake_chat  # type: ignore[method-assign]
    data, usage = await client.chat_json([{"role": "user", "content": "x"}],
                                         purpose="test", default={"fallback": True})

    assert data == {"fallback": True}
    assert calls["n"] == 2                      # 试满次数才放弃


async def test_chat_json_single_attempt_when_disabled() -> None:
    """max_parse_attempts=1 时保持旧行为（只问一次）。"""
    from fissue.ai.client import LLMClient, LLMResponse, Usage
    from fissue.config import LLMConfig

    client = LLMClient(LLMConfig(api_key="k"))
    calls = {"n": 0}

    async def fake_chat(messages, **kwargs):
        calls["n"] += 1
        return LLMResponse(content="", model="m", usage=Usage(), raw={})

    client.chat = fake_chat  # type: ignore[method-assign]
    data, _ = await client.chat_json([{"role": "user", "content": "x"}],
                                     purpose="test", max_parse_attempts=1)
    assert data == {}
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 长度截断自动加码（推理模型 reasoning 吃光预算）
# ---------------------------------------------------------------------------


def test_escalate_tokens_doubles_and_floors() -> None:
    """截断后加码：翻倍，且不低于 16384，但不超过 ceiling。"""
    from fissue.ai.client import _escalate_tokens

    assert _escalate_tokens(512, 32768) == 16384      # 极小值 → 抬到下限
    assert _escalate_tokens(8192, 32768) == 16384
    assert _escalate_tokens(16384, 32768) == 32768    # 翻倍到上限
    assert _escalate_tokens(20000, 32768) == 32768
    assert _escalate_tokens(8192, 12000) == 12000     # ceiling 更低时按 ceiling
    assert _escalate_tokens(40000, 32768) == 40000    # 已超 ceiling 不回退


async def test_chat_json_escalates_max_tokens_on_length_truncation() -> None:
    """被长度截断 → 下一轮自动加大 max_tokens，而不是原样重问。

    实测：推理模型用 token 写 reasoning_content，同一提示的推理长度在
    14207~17264 字符间波动，固定预算迟早撞顶（撞顶时 finish_reason=length、
    content 为空）。原样重问很可能再撞一次，必须加码。
    """
    from fissue.ai.client import LLMClient, LLMResponse, Usage
    from fissue.config import LLMConfig

    cfg = LLMConfig(api_key="k", max_tokens=8192, max_tokens_ceiling=32768, concurrency=1)
    client = LLMClient(cfg)
    seen: list[int | None] = []

    async def fake_chat(messages, **kwargs):
        seen.append(kwargs.get("max_tokens"))
        if len(seen) == 1:
            return LLMResponse(content="", model="m", usage=Usage(),
                               raw={"choices": [{"finish_reason": "length"}]})
        return LLMResponse(content='{"ok": true}', model="m", usage=Usage(),
                           raw={"choices": [{"finish_reason": "stop"}]})

    client.chat = fake_chat  # type: ignore[method-assign]
    data, _ = await client.chat_json([{"role": "user", "content": "x"}], purpose="t")

    assert data == {"ok": True}
    # 第一轮不显式传（由 config.max_tokens=8192 兜底），第二轮加码到 16384
    assert seen[0] is None
    assert seen[1] == 16384


async def test_chat_json_does_not_escalate_on_plain_parse_failure() -> None:
    """非长度原因的解析失败（如模型啰嗦）不该加码——加码解决不了它。"""
    from fissue.ai.client import LLMClient, LLMResponse, Usage
    from fissue.config import LLMConfig

    cfg = LLMConfig(api_key="k", max_tokens=8192, max_tokens_ceiling=32768, concurrency=1)
    client = LLMClient(cfg)
    seen: list[int | None] = []

    async def fake_chat(messages, **kwargs):
        seen.append(kwargs.get("max_tokens"))
        return LLMResponse(content="这不是 JSON", model="m", usage=Usage(),
                           raw={"choices": [{"finish_reason": "stop"}]})

    client.chat = fake_chat  # type: ignore[method-assign]
    data, _ = await client.chat_json([{"role": "user", "content": "x"}], purpose="t")

    assert data == {}
    assert seen == [None, None]        # 两轮都不加码


# ---------------------------------------------------------------------------
# max_retries 的语义：**总尝试次数**，不是「额外重试几次」
# ---------------------------------------------------------------------------


async def test_a_zero_retry_configuration_is_rejected_loudly() -> None:
    """`max_retries=0` 会让一次请求都发不出去——必须明确报错。

    回归一次真实事故：探活为了「快速失败」把它设成 0，于是 `range(1, 0+1)` 是空循环、
    一个请求都没发出去，直接掉到函数末尾那句「LLM 请求重试耗尽」。表面现象是「测试连通
    永远失败」，而真实原因（401 / 模型名不存在）根本问不出来。
    """
    from fissue.ai.client import LLMClient
    from fissue.config import LLMConfig
    from fissue.errors import LLMError

    calls: list[str] = []

    class _Recorder:
        async def post(self, url, json=None):  # noqa: ANN001, ARG002
            calls.append(str(url))
            raise AssertionError("配置成 0 时不该发出任何请求")

    client = LLMClient(LLMConfig(api_key="k", max_retries=0), client=_Recorder())  # type: ignore[arg-type]
    with pytest.raises(LLMError) as exc:
        await client._post_with_retry({"model": "m", "messages": []}, use_json=False)  # noqa: SLF001

    assert "max_retries" in str(exc.value), f"报错要说清是配置问题：{exc.value}"
    assert "1" in str(exc.value)
    assert calls == [], "不该有任何请求发出去"


async def test_one_attempt_reaches_the_endpoint_and_reports_the_real_error() -> None:
    """`max_retries=1` = 尝试一次、不重试，并且**把真实错误报出来**。

    这条守着探活的可诊断性：端点是 401 就该说 401（连响应体一起），而不是含糊地说
    「重试耗尽」——用户需要据此判断是 key 错、模型名错还是地址错。
    """
    import httpx

    from fissue.ai.client import LLMClient
    from fissue.config import LLMConfig
    from fissue.errors import LLMError

    seen: list[str] = []

    class _Unauthorized:
        async def post(self, url, json=None):  # noqa: ANN001, ARG002
            seen.append(str(url))
            return httpx.Response(401, json={"error": {"message": "Authentication Fails"}})

    client = LLMClient(LLMConfig(api_key="bad", max_retries=1), client=_Unauthorized())  # type: ignore[arg-type]
    with pytest.raises(LLMError) as exc:
        await client._post_with_retry({"model": "m", "messages": []}, use_json=False)  # noqa: SLF001

    assert len(seen) == 1, "不重试时应当只打一次"
    assert "401" in str(exc.value) and "Authentication Fails" in str(exc.value)


# ---------------------------------------------------------------------------
# 公开 API 契约进上下文：让模型不必猜方法名/返回类型
# ---------------------------------------------------------------------------


def _sig_files() -> dict[str, str]:
    return {
        "envkit/cache.py": (
            "class TTLCache:\n"
            "    def __init__(self, ttl: float, *, clock=None) -> None: ...\n"
            "    def set(self, key: str, value) -> None: ...\n"
            "    def stats(self) -> dict[str, int]: ...\n"
            "    def _private(self): ...\n"
            "\n"
            "def helper(x: int) -> str: ...\n"
            "_secret = 1\n"
            "VERSION = '0.6.0'\n"
        ),
        "tests/test_cache.py": "def test_x():\n    pass\n",
        "README.md": "# envkit\n",
    }


def test_extract_api_signatures_includes_public_api() -> None:
    """公开类/方法/函数与顶层常量都要抽出来——它们是模型写验证器的依据。"""
    from fissue.workspace import extract_api_signatures

    files = _sig_files()
    out = extract_api_signatures(list(files), lambda r: files[r], max_files=10, limit=50)

    assert "class TTLCache" in out
    assert "def set(self, key: str, value)" in out
    assert "def stats(self) -> dict[str, int]" in out      # 返回类型是关键契约
    assert "def helper(x: int) -> str" in out
    assert "VERSION = '0.6.0'" in out


def test_extract_api_signatures_skips_private_and_tests() -> None:
    """私有名与测试文件不进契约——前者不是契约，后者会污染上下文。"""
    from fissue.workspace import extract_api_signatures

    files = _sig_files()
    out = extract_api_signatures(list(files), lambda r: files[r], max_files=10, limit=50)

    assert "_private" not in out
    assert "_secret" not in out
    assert "test_cache" not in out


def test_extract_api_signatures_is_bounded_and_resilient() -> None:
    """必须能限长，且遇到语法错误的文件要跳过而不是崩掉。"""
    from fissue.workspace import extract_api_signatures

    files = {
        "bad.py": "def broken(:\n",                       # 语法错 → 跳过
        "a.py": "def f1() -> int: ...\ndef f2() -> int: ...",
        "b.py": "def g1() -> int: ...",
        "c.py": "def h1() -> int: ...",
    }
    out = extract_api_signatures(list(files), lambda r: files[r], max_files=2, limit=50)
    assert "broken" not in out
    assert out.count("# ") == 2                            # max_files 生效

    many = {"m.py": "\n".join(f"def fn{i}(): ..." for i in range(50))}
    limited = extract_api_signatures(list(many), lambda r: many[r], max_files=5, limit=3)
    assert limited.count("def fn") == 3                    # limit 生效


def test_extract_api_signatures_empty_when_no_python() -> None:
    from fissue.workspace import extract_api_signatures

    files = {"README.md": "# hi", "go.mod": "module x"}
    assert extract_api_signatures(list(files), lambda r: files[r]) == ""


def test_render_repo_context_includes_api_contract() -> None:
    """API 契约要出现在仓库上下文里，并明确要求「不要臆测」。"""
    from fissue.ai.prompts import render_repo_context

    text = render_repo_context(
        language_hint="python",
        file_tree=["envkit/cache.py"],
        readme="# envkit",
        api_signatures="class TTLCache\n    def stats(self) -> dict[str, int]",
    )
    assert "公开 API 契约" in text
    assert "dict[str, int]" in text
    assert "不要臆测" in text


def test_render_repo_context_omits_empty_api_contract() -> None:
    """没有契约时不要冒出空标题（否则模型会以为仓库没有公开 API）。"""
    from fissue.ai.prompts import render_repo_context

    text = render_repo_context(language_hint="python", api_signatures="")
    assert "公开 API 契约" not in text


# ---------------------------------------------------------------------------
# 跨模块调用链（给模型定位根因用）
#
# 回归：demo 实测 pipekit #3 —— 报错点在 stats.count_batches，根因却在
# filterops.in_ranges。只给文件树 + API 签名时，Agent 盯着报错点所在文件
# 连改 10 轮、零进展；把调用链摆出来才能顺着链走到根因模块。
# ---------------------------------------------------------------------------


def _chain_files() -> dict[str, str]:
    """复刻 pipekit 的调用链形态：stats → pipeline → filterops。"""
    return {
        "pipekit/filterops.py": (
            "def in_ranges(point, ranges):\n"
            "    return any(s <= point < e for s, e in ranges)\n"
        ),
        "pipekit/pipeline.py": (
            "from . import filterops\n\n"
            "def drop_ranges(slices, ranges):\n"
            "    return [s for s in slices if not filterops.in_ranges(s[1], ranges)]\n"
        ),
        "pipekit/stats.py": (
            "from . import pipeline\n\n"
            "def count_batches(slices, ranges):\n"
            "    return len(pipeline.drop_ranges(slices, ranges))\n"
        ),
    }


def test_extract_call_graph_shows_cross_module_chain() -> None:
    """调用链必须跨模块——这正是「顺着报错点找根因」的依据。"""
    from fissue.workspace import extract_call_graph

    files = _chain_files()
    out = extract_call_graph(list(files), lambda r: files[r])

    assert "stats.count_batches → pipeline.drop_ranges" in out
    assert "pipeline.drop_ranges → filterops.in_ranges" in out


def test_extract_call_graph_ignores_stdlib_and_unknown_modules() -> None:
    """只认本仓库模块：json.dumps / os.path 这类不该进链，否则是噪声。"""
    from fissue.workspace import extract_call_graph

    files = {
        "pkg/a.py": (
            "import json, os\n"
            "from . import b\n\n"
            "def f(x):\n"
            "    print(json.dumps(x))\n"
            "    os.path.join('a', 'b')\n"
            "    return b.helper(x)\n"
        ),
        "pkg/b.py": "def helper(x):\n    return x\n",
    }
    out = extract_call_graph(list(files), lambda r: files[r])

    assert "a.f → b.helper" in out
    assert "json" not in out and "os." not in out


def test_extract_call_graph_skips_tests_dunder_and_bad_syntax() -> None:
    """测试文件、__init__、语法错的文件都不参与；且不得抛异常。"""
    from fissue.workspace import extract_call_graph

    files = {
        "pkg/__init__.py": "from . import core\n",
        "pkg/core.py": "def run():\n    return 1\n",
        "tests/test_core.py": "from pkg.core import run\ndef test_x():\n    run()\n",
        "pkg/broken.py": "def oops(:\n",
    }
    out = extract_call_graph(list(files), lambda r: files[r])

    assert "__init__" not in out and "test_core" not in out and "broken" not in out


def test_extract_call_graph_is_bounded_and_empty_safe() -> None:
    """必须能限长；没有可分析文件时返回空串而不是崩。"""
    from fissue.workspace import extract_call_graph

    files = {
        "p/a.py": "from . import b\ndef f1():\n    return b.g()\ndef f2():\n    return b.g()",
        "p/b.py": "def g():\n    return 1\n",
    }
    assert extract_call_graph(list(files), lambda r: files[r], limit=1).count("→") == 1
    assert extract_call_graph([], lambda r: "") == ""
    assert extract_call_graph(["README.md"], lambda r: "# x") == ""


def test_render_repo_context_includes_call_graph() -> None:
    """调用链要进仓库上下文，并点明「报错点常常不是根因」。"""
    from fissue.ai.prompts import render_repo_context

    text = render_repo_context(
        language_hint="python",
        call_graph="stats.count_batches → pipeline.drop_ranges",
    )
    assert "跨模块调用链" in text
    assert "stats.count_batches → pipeline.drop_ranges" in text
    assert "根因" in text


def test_render_repo_context_omits_empty_call_graph() -> None:
    """没有调用链时不要冒出空标题。"""
    from fissue.ai.prompts import render_repo_context

    text = render_repo_context(language_hint="python", call_graph="")
    assert "跨模块调用链" not in text


def test_agent_repo_context_carries_call_graph(settings, tmp_path) -> None:
    """端到端接线：修复阶段的仓库上下文里真的带上了调用链。

    这是防止「实现写了但忘了接线」的守卫——上下文不含链的话，
    模型依然只能看见文件树与签名，pipekit #3 式的空转就会复发。
    """
    import subprocess

    from fissue.config import RepoConfig
    from fissue.fixer.autofix import AutoFixer
    from fissue.models import Platform
    from fissue.workspace import RepoWorkspace

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text(
        "from . import b\n\ndef f(x):\n    return b.g(x)\n", encoding="utf-8"
    )
    (root / "pkg" / "b.py").write_text("def g(x):\n    return x\n", encoding="utf-8")
    # 必须入索引：RepoWorkspace.list_files 走的是 ``git ls-files``，
    # 未跟踪的文件根本不会出现在调用链分析里。
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    ws = RepoWorkspace(root=root, slug="o/n")

    fixer = AutoFixer.__new__(AutoFixer)          # 只测上下文拼装，不需要完整 ctx
    text = fixer._repo_context(ws, RepoConfig(platform=Platform.GITHUB, owner="o", name="n"))

    assert "跨模块调用链" in text
    assert "a.f → b.g" in text
