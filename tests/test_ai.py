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
