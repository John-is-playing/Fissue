"""EdgeJev（Jev / TypeSafe System One）本地决策模型接入。

协议
----
只有 ``POST /v1/systemone``：请求体 ``{model, state, questions}``，返回
``{model, answers, usage}``。**不支持** ``/chat/completions``，也不支持流式，
所以它不是 ``llm`` 的替代品，只作为数值维度的旁路打分器。

``questions`` 的键是问题 ID，值是问题定义：

* ``noul``  —— 某个陈述是否为真（返回 ``noul`` ∈ [0,1]）；
* ``choice``—— 在给定选项里选一个（返回 ``choice`` / ``probabilities`` / ``confidence``）；
* ``score`` —— 属于哪一档（返回 ``score`` / ``legend`` / ``probabilities`` / ``confidence``）。

本模块只用 ``score``：把 0-100 的连续维度切成有序档位，取 ``score`` 换算回 0-100。
``score`` 可以落在两档之间（如 2.05），因此比直接问 ``choice`` 更适合做数值评分。

融合模式（``edgejev.relation``）
--------------------------------
* ``second_opinion``：保留主模型分数，分歧超过阈值时写告警；
* ``weighted``：主模型与 EdgeJev 按 ``weight`` 加权；
* ``replace``：该维度改用 EdgeJev 换算分；
* ``shadow``：只记录，不改结论。

任何模式下，连接失败、返回结构异常或置信度低于 ``min_confidence`` 都回退到
主模型结论，并把原因写进 ``Evaluation.alerts``。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from ..config import EdgeJevConfig
from ..errors import LLMError
from ..logging_setup import get_logger
from ..models import DimensionScore, Evaluation, ItemType, RawItem
from ..net import httpx_verify
from .client import Usage, UsageSink

log = get_logger(__name__)

#: 每个维度的有序档位描述（从「最低」到「最高」）。``score`` 的档位编号即数组下标，
#: 因此换算回 0-100 时用 ``score / (len - 1) * 100``。
DIMENSION_BANDS: dict[str, list[str]] = {
    # 可行性：越高越好做
    "feasibility": [
        "几乎不可行：信息严重缺失，或需要推翻现有架构",
        "较难：需要跨模块重构，或依赖尚未具备的外部前提",
        "中等：需要定位多个模块，方案仍有不确定性",
        "较容易：局部改动，方案基本明确",
        "非常可行：单点修改，路径清晰可直接动手",
    ],
    # 难度：越高越难（与项目「difficulty 越低越好修」的口径一致）
    "difficulty": [
        "极低：一行级修改，几乎无风险",
        "较低：单文件内的局部改动",
        "中等：涉及若干文件或需要小范围设计",
        "较高：跨模块改动，需要方案设计",
        "很高：牵一发动全身，或根因尚未定位",
    ],
    # PR 质量：越高越好
    "pr_quality": [
        "很低：与描述不符、夹带无关改动，或明显刷量",
        "偏低：缺少测试，或未处理兼容性",
        "中等：基本可用，但仍需返工",
        "较好：改动聚焦，有基本测试",
        "很高：聚焦、有测试、无兼容风险，接近可合并",
    ],
}

_DIM_CN = {"feasibility": "可行性", "difficulty": "修复难度", "pr_quality": "PR 质量"}

#: 发给模型的 state 上限，避免超长正文拖慢本地推理（各后端对超长 state 处理不一）。
_MAX_STATE_CHARS = 6000


class SystemOneClient:
    """最薄的 ``/v1/systemone`` 客户端：重试 + 用量记账。"""

    def __init__(
        self,
        cfg: EdgeJevConfig,
        *,
        usage_sink: UsageSink | None = None,
        repo_slug: str = "",
    ) -> None:
        self.cfg = cfg
        self.usage_sink = usage_sink
        self.repo_slug = repo_slug

    async def ask(
        self,
        state: str,
        questions: dict[str, Any],
        *,
        purpose: str = "edgejev",
    ) -> dict[str, Any]:
        """一次性求值一组问题，返回完整响应 dict。

        失败按指数退避重试；最终失败抛 :class:`LLMError`，由调用方降级。
        """
        url = self.cfg.systemone_url
        payload = {"model": self.cfg.model, "state": state, "questions": questions}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        delay = 0.5
        last_exc: Exception | None = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.timeout_seconds, connect=5.0),
            verify=httpx_verify(),
        ) as client:
            for attempt in range(1, self.cfg.max_retries + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt >= self.cfg.max_retries:
                        raise LLMError(f"EdgeJev 连接失败（{url}）：{exc}") from exc
                    log.warning("EdgeJev 网络异常（第 %d 次）：%s", attempt, exc)
                    await asyncio.sleep(delay + random.uniform(0, 0.2))
                    delay = min(delay * 2, 8.0)
                    continue

                if resp.status_code == 200:
                    data = resp.json()
                    await self._record(data, purpose)
                    return data

                body = resp.text[:400]
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = LLMError(f"EdgeJev 服务错误 {resp.status_code}：{body}")
                    if attempt >= self.cfg.max_retries:
                        raise last_exc
                    log.warning("EdgeJev 服务错误 %s，稍后重试", resp.status_code)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8.0)
                    continue
                raise LLMError(f"EdgeJev 请求失败 {resp.status_code}：{body}")

        raise LLMError(f"EdgeJev 请求重试耗尽：{last_exc}")  # pragma: no cover

    async def _record(self, data: dict[str, Any], purpose: str) -> None:
        if self.usage_sink is None:
            return
        raw = data.get("usage") or {}
        usage = Usage(
            purpose=f"{purpose}:{self.repo_slug}" if self.repo_slug else purpose,
            model=str(data.get("model") or self.cfg.model),
            prompt_tokens=int(raw.get("input_tokens") or 0),
            completion_tokens=int(raw.get("output_tokens") or 0),
            # 本地模型无 API 费用；成本恒 0，但 token 仍记账，便于统计调用量。
            cost_usd=0.0,
        )
        try:
            result = self.usage_sink(usage)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # 记账失败不影响主流程
            log.warning("记录 EdgeJev 用量失败：%s", exc)


def build_state(item: RawItem) -> str:
    """把条目拼成 EdgeJev 的 ``state``。"""
    parts = [item.title or "", item.body or ""]
    text = "\n\n".join(p for p in parts if p).strip()
    if len(text) > _MAX_STATE_CHARS:
        text = text[:_MAX_STATE_CHARS] + "\n…（正文已截断）"
    return text


def build_questions(dimensions: list[str]) -> dict[str, Any]:
    """为选中的维度生成 ``score`` 型问题定义。"""
    return {
        dim: {
            "type": "score",
            "instructions": f"对这条 Issue/PR 评估其{_DIM_CN[dim]}（档位从低到高）",
            "criteria": list(DIMENSION_BANDS[dim]),
        }
        for dim in dimensions
        if dim in DIMENSION_BANDS
    }


def _derive_dimension(dim: str, answer: Any) -> DimensionScore | None:
    """把 ``score`` 型答案换算成 0-100 的 :class:`DimensionScore`。"""
    if not isinstance(answer, dict):
        return None
    bands = DIMENSION_BANDS[dim]
    try:
        raw_score = float(answer["score"])
    except (KeyError, TypeError, ValueError):
        return None
    pct = raw_score / (len(bands) - 1) * 100.0
    score = max(0, min(100, int(round(pct))))
    confidence = max(0.0, min(1.0, float(answer.get("confidence") or 0.0)))

    probs = answer.get("probabilities") or {}
    top_band = ""
    if isinstance(probs, dict) and probs:
        try:
            idx = max(probs.items(), key=lambda kv: float(kv[1] or 0))[0]
            top_band = bands[int(idx)]
        except (TypeError, ValueError, IndexError):
            top_band = ""
    reason = f"EdgeJev 分档 {raw_score:.2f}/{len(bands) - 1}"
    if top_band:
        reason += f"，最可能档位：{top_band}"
    evidence: list[str] = []
    if isinstance(probs, dict):
        for idx, p in sorted(probs.items(), key=lambda kv: -float(kv[1] or 0))[:2]:
            try:
                evidence.append(f"{bands[int(idx)]}：{float(p):.2f}")
            except (TypeError, ValueError, IndexError):
                continue
    return DimensionScore(score=score, reason=reason, evidence=evidence, confidence=confidence)


class EdgeJevAdvisor:
    """把 EdgeJev 的读数按配置的 ``relation`` 合并进 :class:`Evaluation`。"""

    def __init__(
        self,
        cfg: EdgeJevConfig,
        *,
        usage_sink: UsageSink | None = None,
        repo_slug: str = "",
    ) -> None:
        self.cfg = cfg
        self.client = SystemOneClient(cfg, usage_sink=usage_sink, repo_slug=repo_slug)

    def dimensions_for(self, item: RawItem) -> list[str]:
        dims = list(self.cfg.dimensions)
        # PR 质量只对 PR 有意义；Issue 上问它纯属浪费一次推理。
        if item.item_type is not ItemType.PR and "pr_quality" in dims:
            dims.remove("pr_quality")
        return dims

    async def advise(self, item: RawItem, evaluation: Evaluation) -> None:
        """就地修改 ``evaluation``：融合分数、记录读数、写入降级告警。"""
        dims = self.dimensions_for(item)
        if not dims:
            return

        state = build_state(item)
        questions = build_questions(dims)
        try:
            data = await self.client.ask(state, questions, purpose=f"edgejev:{item.repo}")
        except Exception as exc:
            evaluation.alerts.append(
                f"EdgeJev 未参与评分（{_DIM_CN_TEXT(dims)}）：{exc}；已回退主模型结论"
            )
            log.warning("EdgeJev 调用失败，回退主模型结论：%s", exc)
            return

        answers = data.get("answers")
        if not isinstance(answers, dict):
            evaluation.alerts.append(
                "EdgeJev 返回结构异常（缺少 answers）；已回退主模型结论"
            )
            log.warning("EdgeJev 返回结构异常：%s", str(data)[:300])
            return

        derived: dict[str, DimensionScore] = {}
        for dim in dims:
            parsed = _derive_dimension(dim, answers.get(dim))
            if parsed is None:
                evaluation.alerts.append(
                    f"EdgeJev {_DIM_CN[dim]} 读数无法解析；该维度已回退主模型结论"
                )
                continue
            if parsed.confidence < self.cfg.min_confidence:
                evaluation.alerts.append(
                    f"EdgeJev {_DIM_CN[dim]} 置信度 {parsed.confidence:.2f} 低于阈值 "
                    f"{self.cfg.min_confidence:.2f}；该维度已回退主模型结论"
                )
                continue
            derived[dim] = parsed

        used = self._merge(evaluation, derived)
        evaluation.edgejev = {
            "model": str(data.get("model") or self.cfg.model),
            "relation": self.cfg.relation,
            "answers": {dim: answers.get(dim) for dim in dims},
            "applied": used,
        }

    def _merge(self, evaluation: Evaluation, derived: dict[str, DimensionScore]) -> list[str]:
        """按关系模式合并；返回实际生效的维度列表。"""
        used: list[str] = []
        relation = self.cfg.relation
        if relation == "shadow":
            return used

        for dim, jev in derived.items():
            main = evaluation.scores.get(dim)
            if relation == "replace":
                setattr(evaluation.scores, dim, jev)
                used.append(dim)
                continue
            if main is None:
                # 主模型没给这一维（如 Issue 上的 pr_quality），直接补上更有信息量。
                setattr(evaluation.scores, dim, jev)
                used.append(dim)
                continue
            if relation == "weighted":
                w = self.cfg.weight
                blended = int(round(main.score * (1 - w) + jev.score * w))
                merged = DimensionScore(
                    score=blended,
                    reason=f"{main.reason}｜EdgeJev 加权 {jev.score}（权重 {w:.2f}）",
                    evidence=list(main.evidence) + jev.evidence,
                    confidence=min(main.confidence, jev.confidence),
                )
                setattr(evaluation.scores, dim, merged)
                used.append(dim)
                continue
            # second_opinion：主模型结论不变，分歧超阈值才告警
            gap = abs(main.score - jev.score)
            if gap >= self.cfg.divergence_threshold:
                evaluation.alerts.append(
                    f"EdgeJev 与主模型在{_DIM_CN[dim]}上分歧 {gap} 分"
                    f"（主模型 {main.score} / EdgeJev {jev.score}，阈值 "
                    f"{self.cfg.divergence_threshold}），建议人工复核"
                )
        return used


def _DIM_CN_TEXT(dims: list[str]) -> str:
    return "、".join(_DIM_CN.get(d, d) for d in dims)
