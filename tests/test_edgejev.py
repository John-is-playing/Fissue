"""EdgeJev / System One 旁路打分器：分档换算、四种融合模式与降级告警。"""

from __future__ import annotations

from typing import Any

import pytest

from fissue.ai.system_one import (
    DIMENSION_BANDS,
    EdgeJevAdvisor,
    SystemOneClient,
    _derive_dimension,
    build_questions,
)
from fissue.config import EdgeJevConfig
from fissue.errors import ConfigError
from fissue.models import DimensionScore, Evaluation, ItemType, Scores


def _answer(score: float, confidence: float = 0.8) -> dict[str, Any]:
    return {
        "type": "score",
        "score": score,
        "legend": {str(i): b for i, b in enumerate(DIMENSION_BANDS["feasibility"])},
        "probabilities": {"4": 0.6, "3": 0.3},
        "confidence": confidence,
    }


def _payload(answers: dict[str, Any]) -> dict[str, Any]:
    return {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 50, "output_tokens": 0}}


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_build_questions_only_selected_dimensions() -> None:
    q = build_questions(["feasibility", "difficulty"])
    assert set(q) == {"feasibility", "difficulty"}
    assert q["feasibility"]["type"] == "score"
    assert q["feasibility"]["criteria"] == DIMENSION_BANDS["feasibility"]


def test_derive_dimension_maps_band_to_percent() -> None:
    dim = _derive_dimension("feasibility", _answer(4.0))
    assert dim is not None
    assert dim.score == 100
    assert dim.confidence == pytest.approx(0.8)

    mid = _derive_dimension("feasibility", _answer(2.0))
    assert mid is not None and mid.score == 50

    assert _derive_dimension("difficulty", {"type": "choice"}) is None


def test_relation_validation_rejects_unknown_values() -> None:
    with pytest.raises(ConfigError):
        EdgeJevConfig(relation="bogus")
    with pytest.raises(ConfigError):
        EdgeJevConfig(dimensions=["importance"])


def test_systemone_url_normalisation() -> None:
    assert EdgeJevConfig(base_url="http://127.0.0.1:8009").systemone_url == (
        "http://127.0.0.1:8009/v1/systemone"
    )
    assert EdgeJevConfig(base_url="http://127.0.0.1:8009/v1/systemone").systemone_url == (
        "http://127.0.0.1:8009/v1/systemone"
    )


# ---------------------------------------------------------------------------
# 融合模式
# ---------------------------------------------------------------------------


def _eval_with(**scores: int) -> Evaluation:
    return Evaluation(
        scores=Scores(
            feasibility=DimensionScore(score=scores.get("feasibility", 40), confidence=0.7),
            difficulty=DimensionScore(score=scores.get("difficulty", 30), confidence=0.7),
        )
    )


@pytest.fixture
def advisor() -> EdgeJevAdvisor:
    return EdgeJevAdvisor(EdgeJevConfig(enabled=True))


def _patch_ask(monkeypatch: pytest.MonkeyPatch, advisor: EdgeJevAdvisor, response: dict[str, Any]) -> None:
    async def fake_ask(state: str, questions: dict[str, Any], *, purpose: str = "") -> dict[str, Any]:
        return response

    monkeypatch.setattr(advisor.client, "ask", fake_ask)


async def _advise(advisor: EdgeJevAdvisor, evaluation: Evaluation, item_type: ItemType = ItemType.ISSUE) -> None:
    from fissue.models import Platform, RawItem

    item = RawItem(
        platform=Platform.GITHUB,
        repo="a/b",
        number=1,
        item_type=item_type,
        title="t",
        body="b",
    )
    await advisor.advise(item, evaluation)


async def test_second_opinion_keeps_main_score_and_flags_divergence(
    monkeypatch: pytest.MonkeyPatch, advisor: EdgeJevAdvisor
) -> None:
    advisor.cfg.relation = "second_opinion"
    _patch_ask(monkeypatch, advisor, _payload({"feasibility": _answer(4.0), "difficulty": _answer(3.0)}))
    ev = _eval_with(feasibility=40, difficulty=75)
    await _advise(advisor, ev)

    # 主模型结论不变
    assert ev.scores.feasibility is not None and ev.scores.feasibility.score == 40
    # 可行性分歧 60 分、难度分歧 0 分 → 只应有一条告警
    assert any("可行性" in a and "分歧" in a for a in ev.alerts)
    assert ev.edgejev is not None and ev.edgejev["relation"] == "second_opinion"


async def test_replace_uses_edgejev_scores(monkeypatch: pytest.MonkeyPatch, advisor: EdgeJevAdvisor) -> None:
    advisor.cfg.relation = "replace"
    _patch_ask(monkeypatch, advisor, _payload({"feasibility": _answer(4.0), "difficulty": _answer(0.0)}))
    ev = _eval_with()
    await _advise(advisor, ev)

    assert ev.scores.feasibility is not None and ev.scores.feasibility.score == 100
    assert ev.scores.difficulty is not None and ev.scores.difficulty.score == 0


async def test_weighted_blends_both_scores(monkeypatch: pytest.MonkeyPatch, advisor: EdgeJevAdvisor) -> None:
    advisor.cfg.relation = "weighted"
    advisor.cfg.weight = 0.4
    _patch_ask(monkeypatch, advisor, _payload({"feasibility": _answer(4.0), "difficulty": _answer(0.0)}))
    ev = _eval_with(feasibility=40, difficulty=30)
    await _advise(advisor, ev)

    # 40*0.6 + 100*0.4 = 64；30*0.6 + 0*0.4 = 18
    assert ev.scores.feasibility is not None and ev.scores.feasibility.score == 64
    assert ev.scores.difficulty is not None and ev.scores.difficulty.score == 18


async def test_shadow_never_changes_scores(monkeypatch: pytest.MonkeyPatch, advisor: EdgeJevAdvisor) -> None:
    advisor.cfg.relation = "shadow"
    _patch_ask(monkeypatch, advisor, _payload({"feasibility": _answer(4.0)}))
    ev = _eval_with(feasibility=10)
    await _advise(advisor, ev)

    assert ev.scores.feasibility is not None and ev.scores.feasibility.score == 10
    assert ev.edgejev is not None and ev.edgejev["applied"] == []


async def test_low_confidence_falls_back_with_alert(
    monkeypatch: pytest.MonkeyPatch, advisor: EdgeJevAdvisor
) -> None:
    advisor.cfg.relation = "replace"
    _patch_ask(monkeypatch, advisor, _payload({"feasibility": _answer(4.0, confidence=0.05)}))
    ev = _eval_with(feasibility=40)
    await _advise(advisor, ev)

    assert ev.scores.feasibility is not None and ev.scores.feasibility.score == 40
    assert any("置信度" in a for a in ev.alerts)


async def test_unreachable_service_falls_back_with_alert(monkeypatch: pytest.MonkeyPatch, advisor: EdgeJevAdvisor) -> None:
    async def boom(state: str, questions: dict[str, Any], *, purpose: str = "") -> dict[str, Any]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(advisor.client, "ask", boom)
    ev = _eval_with(feasibility=40)
    await _advise(advisor, ev)

    assert ev.scores.feasibility is not None and ev.scores.feasibility.score == 40
    assert ev.alerts and "已回退主模型结论" in ev.alerts[0]


# ---------------------------------------------------------------------------
# 存储往返
# ---------------------------------------------------------------------------


def test_repository_roundtrips_alerts_and_edgejev(settings, repo, sample_issue) -> None:
    from fissue.models import RepoRef

    rid = repo.ensure_repo(RepoRef(platform=sample_issue.platform, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)

    ev = Evaluation(alerts=["EdgeJev 降级"], edgejev={"relation": "shadow", "answers": {"feasibility": {"score": 3}}})
    repo.save_evaluation(sample_issue.key, ev)

    saved = repo.latest_evaluation(sample_issue.key)
    assert saved is not None
    assert saved.alerts == ["EdgeJev 降级"]
    assert saved.edgejev == {"relation": "shadow", "answers": {"feasibility": {"score": 3}}}


async def test_systemone_client_posts_and_records_usage(settings, sample_issue) -> None:
    """用 respx 拦截本地端点，验证请求形状与用量记账。"""
    import httpx
    import respx

    cfg = EdgeJevConfig(enabled=True, base_url="http://127.0.0.1:8009/v1/systemone")
    recorded: list[Any] = []

    async def import_sink(usage: Any) -> None:
        recorded.append(usage)

    client = SystemOneClient(cfg, usage_sink=import_sink)
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://127.0.0.1:8009/v1/systemone").mock(
            return_value=httpx.Response(200, json=_payload({"feasibility": _answer(3.0)}))
        )
        data = await client.ask("state", build_questions(["feasibility"]), purpose="edgejev:a/b")

    assert data["answers"]["feasibility"]["score"] == 3.0
    assert recorded and recorded[0].purpose == "edgejev:a/b"
    assert recorded[0].prompt_tokens == 50
    assert recorded[0].cost_usd == 0.0
