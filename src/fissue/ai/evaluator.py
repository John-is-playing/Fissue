"""评估器：把「条目 + 提示词 + LLM」组合成结构化的评测结果。

职责
----
* 关键词预筛分类 → 不确定才调 LLM（省钱）。
* 四维评分解析与矫正（0-100、比例值归一、缺字段兜底）。
* 依据配置阈值计算优先级（tier1 / tier2 / none）与建议动作。
* 反刷子信号的合并与规则兜底。
* 结果写库（通过 :class:`~fissue.store.repository.Repository`）。

不了解网络与存储细节之外的东西，是一个纯粹的「业务编排层」。
"""

from __future__ import annotations

import re as _re
from dataclasses import dataclass
from typing import Any, Sequence

from ..config import ClassificationConfig, EvaluationConfig, FixPolicyConfig, Settings
from ..logging_setup import get_logger
from ..models import (
    Action,
    Category,
    DimensionScore,
    Evaluation,
    ItemType,
    Priority,
    RawItem,
    Scores,
    SpamSignal,
)
from ..store.repository import Repository
from . import prompts
from .client import LLMClient, Usage, clamp01, clamp_score, extract_json

log = get_logger(__name__)

DIMENSIONS = ("authenticity", "importance", "feasibility", "pr_quality", "difficulty")

# 纯 ASCII 关键词（可含空格/连字符/下划线）
_ASCII_KEYWORD = _re.compile(r"^[a-z0-9][a-z0-9 _.-]*$")

# reasons 文本里的条目引用（如「与 Issue #2 完全重合」）→ 回填 duplicate_of
_DUP_REF = _re.compile(r"[#＃]\s*(\d+)")


def keyword_hit(keyword: str, lowered_text: str) -> bool:
    """判断关键词是否命中。

    * 纯 ASCII 关键词用**词边界**匹配：避免 ``request`` 误命中库名 ``requests.get``，
      也不会把 ``bugfix`` 当成 ``bug``。
    * 中文等非 ASCII 关键词用子串匹配（中文没有词边界概念）。
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    if _ASCII_KEYWORD.match(kw):
        pattern = rf"(?<![a-z0-9_]){_re.escape(kw)}(?![a-z0-9_])"
        return _re.search(pattern, lowered_text) is not None
    return kw in lowered_text


# ---------------------------------------------------------------------------
# 关键词预筛
# ---------------------------------------------------------------------------


@dataclass
class KeywordVerdict:
    """关键词预判结果。"""

    category: Category
    confident: bool
    hits_bug: list[str]
    hits_feature: list[str]


def keyword_classify(text: str, cfg: ClassificationConfig) -> KeywordVerdict:
    """用关键词快速预判 BUG / FEATURE。

    只在「一边倒」时返回 confident=True，其余交给 LLM（``fallback_to_llm``）。
    """
    lowered = (text or "").lower()
    hits_bug = [k for k in cfg.bug_keywords if keyword_hit(k, lowered)]
    hits_feature = [k for k in cfg.feature_keywords if keyword_hit(k, lowered)]

    if hits_bug and not hits_feature:
        return KeywordVerdict(Category.BUG, True, hits_bug, hits_feature)
    if hits_feature and not hits_bug:
        return KeywordVerdict(Category.FEATURE, True, hits_bug, hits_feature)
    if hits_bug and hits_feature:
        return KeywordVerdict(Category.UNKNOWN, False, hits_bug, hits_feature)
    return KeywordVerdict(Category.UNKNOWN, False, hits_bug, hits_feature)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_dimension(raw: Any) -> DimensionScore | None:
    """把模型返回的单维评分解析成 :class:`DimensionScore`。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float, str)):
        return DimensionScore(score=clamp_score(raw), reason="", confidence=0.3)
    if not isinstance(raw, dict):
        return None

    score = clamp_score(raw.get("score", raw.get("value", 50)))
    reason = str(raw.get("reason") or raw.get("explanation") or "")[:2000]
    evidence_raw = raw.get("evidence") or raw.get("evidences") or []
    if isinstance(evidence_raw, str):
        evidence = [evidence_raw]
    elif isinstance(evidence_raw, list):
        evidence = [str(e)[:500] for e in evidence_raw if e]
    else:
        evidence = []
    confidence = clamp01(raw.get("confidence", 0.5))
    return DimensionScore(score=score, reason=reason, evidence=evidence, confidence=confidence)


def parse_scores(raw: dict[str, Any]) -> Scores:
    """解析四维 + 难度评分（兼容模型把分数放在顶层的情况）。"""
    container = raw.get("scores") if isinstance(raw.get("scores"), dict) else raw
    kwargs: dict[str, DimensionScore | None] = {}
    for name in DIMENSIONS:
        kwargs[name] = parse_dimension(container.get(name))
    return Scores(**kwargs)


def parse_spam(raw: Any) -> SpamSignal:
    if not isinstance(raw, dict):
        return SpamSignal()
    dup_of = raw.get("duplicate_of") or []
    if not isinstance(dup_of, list):
        dup_of = [dup_of]
    reasons = raw.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    parsed_nums: list[int] = []
    for d in dup_of:
        try:
            parsed_nums.append(int(d))
        except (TypeError, ValueError):
            continue
    return SpamSignal(
        is_spam=bool(raw.get("is_spam")),
        # 填了 duplicate_of 就说明是重复，两个字段不该互相矛盾
        is_duplicate=bool(raw.get("is_duplicate")) or bool(parsed_nums),
        is_ai_generated=bool(raw.get("is_ai_generated")),
        duplicate_of=parsed_nums,
        reasons=[str(r)[:300] for r in reasons if r],
    )


def backfill_duplicate_of(spam: SpamSignal, *, exclude: int | None = None) -> SpamSignal:
    """``is_duplicate`` 为真但 ``duplicate_of`` 为空时，从 reasons 文本回填编号。

    模型常把重复对象写成 reasons 里的文字（如「与 Issue #2 完全重合」）却忘了
    填数组字段，导致结构化字段恒为空、导出与看板跳不到具体条目。``exclude``
    传条目自身编号，避免把自己算成重复对象。
    """
    if not spam.is_duplicate or spam.duplicate_of:
        return spam
    nums: list[int] = []
    for reason in spam.reasons:
        for found in _DUP_REF.findall(reason):
            n = int(found)
            if n != exclude and n not in nums:
                nums.append(n)
    if nums:
        spam.duplicate_of = nums[:10]
    return spam


def parse_category(value: Any) -> Category:
    text = str(value or "").strip().lower()
    if text in ("bug", "defect", "error", "crash"):
        return Category.BUG
    if text in ("feature", "enhancement", "proposal", "request"):
        return Category.FEATURE
    return Category.UNKNOWN


def parse_action(value: Any) -> Action:
    text = str(value or "").strip().lower()
    for a in Action:
        if a.value == text:
            return a
    # 常见同义词
    alias = {
        "fix": Action.FIX_NOW,
        "repair": Action.FIX_NOW,
        "review": Action.TRIAGE,
        "ignore": Action.CLOSE,
        "invalid": Action.CLOSE,
        "question": Action.ANSWER,
    }
    return alias.get(text, Action.TRIAGE)


def parse_labels(value: Any) -> list[str]:
    def _norm(name: str) -> str:
        return str(name).strip().lower().replace(" ", "-")

    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",")]
        return [n for p in parts if (n := _norm(p))][:12]
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            name = _norm(v)
            if name and name not in out:
                out.append(name)
        return out[:12]
    return []


def parse_evaluation(raw: dict[str, Any], *, model: str, usage: Usage | None = None) -> Evaluation:
    """把模型 JSON 转成 :class:`Evaluation`。"""
    scores = parse_scores(raw)
    spam = parse_spam(raw.get("spam"))
    labels = parse_labels(raw.get("labels_suggested") or raw.get("labels"))
    summary = str(raw.get("summary") or raw.get("conclusion") or "")[:4000]

    return Evaluation(
        scores=scores,
        category=parse_category(raw.get("category")),
        labels_suggested=labels,
        action=parse_action(raw.get("action")),
        summary=summary,
        spam=spam,
        model=model,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        cost_usd=usage.cost_usd if usage else 0.0,
    )


# ---------------------------------------------------------------------------
# 规则后处理
# ---------------------------------------------------------------------------


def compute_priority(
    evaluation: Evaluation,
    *,
    category: Category,
    policy: FixPolicyConfig,
    item_type: ItemType | None = None,
    verified: bool = False,
) -> Priority:
    """按 Q1 的规则算自动修复优先级。

    tier1 = 难度低 + 重要性高；tier2 = 难度低 + 重要性低；其余 none。
    ``policy.only_issues`` 为真时（默认）只有 Issue 才自动修复；PR 一律 none，
    否则报告里会给出误导性的 ``fix_now``（实际不会被自动修复）。
    疑似重复/刷量的条目同样不修——DESIGN 的策略闸门要求「排除疑似刷量/重复」，
    否则会自动为一条重复 Issue 生成第二份补丁。
    """
    if category is not Category.BUG:
        return Priority.NONE
    if evaluation.spam.suspicious:
        return Priority.NONE
    if policy.only_issues and item_type is not None and item_type is not ItemType.ISSUE:
        return Priority.NONE
    difficulty = evaluation.difficulty
    importance = evaluation.importance

    t1, t2 = policy.tier1, policy.tier2
    if difficulty <= t1.max_difficulty and importance >= t1.min_importance:
        return Priority.TIER1
    if difficulty <= t2.max_difficulty and importance >= t2.min_importance:
        return Priority.TIER2
    return Priority.NONE


def apply_rule_adjustments(
    evaluation: Evaluation,
    item: RawItem,
    *,
    cfg: EvaluationConfig,
    verified: bool = False,
) -> Evaluation:
    """规则兜底：把明显不可信 / 不可行的结论压制住，避免误自动修复。"""
    th = cfg.thresholds

    # 真实性过低 → 疑似误报/刷子，禁止自动修复
    if evaluation.authenticity and evaluation.authenticity < th.authenticity_min:
        evaluation.spam.reasons.append(
            f"真实性评分 {evaluation.authenticity} 低于阈值 {th.authenticity_min}"
        )
        if evaluation.action is Action.FIX_NOW:
            evaluation.action = Action.TRIAGE

    # 已被合并/关闭的条目不再自动修复
    if item.item_type is ItemType.PR and item.merged:
        evaluation.priority = Priority.NONE

    # 疑问句/求助类多半不是 BUG
    text = f"{item.title}\n{item.body}".lower()
    if item.item_type is ItemType.ISSUE and evaluation.category is Category.BUG:
        question_markers = ("how to", "how do i", "怎么", "如何", "是否支持", "能不能")
        if any(m in text for m in question_markers) and evaluation.importance < th.importance_high:
            evaluation.action = Action.ANSWER

    return evaluation


def merge_checklist_result(evaluation: Evaluation, result: dict[str, Any]) -> Evaluation:
    """把清单判定结果合并进评测（清单通过说明问题可复现）。"""
    passed = bool(result.get("passed"))
    conclusion = str(result.get("conclusion") or "")
    if conclusion:
        evaluation.summary = (evaluation.summary + "｜" + conclusion)[:4000]
    return evaluation


# ---------------------------------------------------------------------------
# 评估器
# ---------------------------------------------------------------------------


class Evaluator:
    """把条目评测成 :class:`Evaluation` 并写库。"""

    def __init__(
        self,
        client: LLMClient,
        repo: Repository,
        settings: Settings,
    ) -> None:
        self.client = client
        self.repo = repo
        self.settings = settings
        self.cfg = settings.evaluation
        self.cls_cfg = settings.classification

    # -- 分类 -------------------------------------------------------------

    async def classify(self, item: RawItem) -> tuple[Category, float, str]:
        """判定 BUG / FEATURE。返回 ``(category, confidence, reason)``。"""
        if self.cls_cfg.use_keywords_first:
            verdict = keyword_classify(f"{item.title}\n{item.body}", self.cls_cfg)
            if verdict.confident:
                reason = f"关键词命中：{'/'.join(verdict.hits_bug or verdict.hits_feature)}"
                log.debug("关键词分类 %s -> %s", item.key, verdict.category.value)
                return verdict.category, 0.7, reason
            if not self.cls_cfg.fallback_to_llm:
                return Category.UNKNOWN, 0.2, "关键词无法判定且未启用 LLM 兜底"

        data, _ = await self.client.chat_json(
            prompts.classification_prompt(item), purpose="classify", default={}
        )
        return (
            parse_category(data.get("category")),
            clamp01(data.get("confidence", 0.5)),
            str(data.get("reason") or "")[:1000],
        )

    # -- 评测 -------------------------------------------------------------

    async def evaluate(self, item: RawItem, *, similar: Sequence[dict[str, Any]] | None = None) -> Evaluation:
        """完整评测一个条目：分类 + 四维评分 + 兜底矫正，并写库。"""
        category, _conf, _reason = await self.classify(item)
        if category is Category.UNKNOWN:
            # 分类不明时，用条目类型给个保守默认：PR 更像 feature
            category = Category.BUG if item.item_type is ItemType.ISSUE else Category.FEATURE

        threshold_hint = (
            f"\n参考阈值：重要性 ≥ {self.cfg.thresholds.importance_high} 视为高；"
            f"难度 ≤ {self.cfg.thresholds.difficulty_low} 视为低。"
        )
        data, usage = await self.client.chat_json(
            prompts.evaluation_prompt(item, threshold_hint=threshold_hint, similar_issues=similar),
            purpose="evaluate",
            default={},
        )

        evaluation = parse_evaluation(data, model=usage.model, usage=usage)
        evaluation.category = category  # 以分类阶段结论为准
        # 模型常把重复对象只写进 reasons 文字，导致 duplicate_of 恒为空、跳不到条目
        evaluation.spam = backfill_duplicate_of(evaluation.spam, exclude=item.number)
        evaluation.priority = compute_priority(
            evaluation,
            category=category,
            policy=self.settings.fix_policy,
            item_type=item.item_type,
        )
        evaluation = apply_rule_adjustments(evaluation, item, cfg=self.cfg)

        self.repo.save_evaluation(item.key, evaluation)
        log.info(
            "评测完成 %s｜分类=%s｜真实性=%s 重要性=%s 难度=%s｜优先级=%s",
            item.key,
            category.value,
            evaluation.authenticity,
            evaluation.importance,
            evaluation.difficulty,
            evaluation.priority.value,
        )
        return evaluation

    async def deep_pr_review(self, item: RawItem, *, diff: str) -> Evaluation | None:
        """按需升级：拉 diff 做深度代码质量评审（Q1 分层）。"""
        if item.item_type is not ItemType.PR:
            return None
        data, usage = await self.client.chat_json(
            prompts.pr_quality_prompt(item, diff=diff), purpose="pr_review", default={}
        )
        if not data:
            return None

        base = self.repo.latest_evaluation(item.key) or Evaluation(category=Category.FEATURE)
        quality = parse_dimension(data.get("pr_quality"))
        if quality is not None:
            base.scores.pr_quality = quality
        labels = parse_labels(data.get("labels_suggested"))
        for lbl in labels:
            if lbl not in base.labels_suggested:
                base.labels_suggested.append(lbl)
        notes = data.get("risk_notes") or []
        if isinstance(notes, list) and notes:
            base.spam.reasons.extend(str(n)[:200] for n in notes if n)
        if data.get("summary"):
            base.summary = str(data["summary"])[:4000]
        base.model = usage.model
        base.prompt_tokens += usage.prompt_tokens
        base.completion_tokens += usage.completion_tokens
        base.cost_usd += usage.cost_usd

        self.repo.save_evaluation(item.key, base)
        return base

    # -- 批量优先级 -------------------------------------------------------

    async def plan_fix_priority(self, entries: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """批量决定修复优先级（Q1 规则）。返回 ``{key: {...}}``。"""
        if not entries:
            return {}
        data, _ = await self.client.chat_json(
            prompts.fix_plan_prompt(items=entries), purpose="fix_plan", default={}
        )
        out: dict[str, dict[str, Any]] = {}
        for r in data.get("results") or []:
            if not isinstance(r, dict) or not r.get("key"):
                continue
            prio_raw = str(r.get("priority") or "none").lower()
            prio = {
                "tier1": Priority.TIER1,
                "tier2": Priority.TIER2,
                "none": Priority.NONE,
            }.get(prio_raw, Priority.NONE)
            out[str(r["key"])] = {
                "priority": prio,
                "reason": str(r.get("reason") or "")[:1000],
                "confidence": clamp01(r.get("confidence", 0.5)),
            }
        return out

    # -- 批量 flush 结论 --------------------------------------------------

    async def conclude_batch(self, *, queue_kind: str, entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """队列攒够一批后统一出结论并打标签（Q1 的批量环节）。"""
        if not entries:
            return []
        data, usage = await self.client.chat_json(
            prompts.batch_conclusion_prompt(queue_kind=queue_kind, entries=entries),
            purpose="batch_conclusion",
            default={},
        )
        results = data.get("results")
        if not isinstance(results, list):
            log.warning("批量结论返回结构异常，期望 results 数组；收到 keys=%s", list(data.keys()))
            return []

        by_key = {str(e.get("key")): e for e in entries}
        parsed: list[dict[str, Any]] = []
        for r in results:
            if not isinstance(r, dict):
                continue
            key = str(r.get("key") or "")
            if key not in by_key:
                continue
            prio_raw = str(r.get("priority") or "none").lower()
            parsed.append(
                {
                    "key": key,
                    "verdict": str(r.get("verdict") or "needs_review")[:32],
                    "labels": parse_labels(r.get("labels")),
                    "priority": {
                        "tier1": Priority.TIER1,
                        "tier2": Priority.TIER2,
                        "none": Priority.NONE,
                    }.get(prio_raw, Priority.NONE),
                    "reason": str(r.get("reason") or "")[:2000],
                    "confidence": clamp01(r.get("confidence", 0.5)),
                    "model": usage.model,
                }
            )
        return parsed
