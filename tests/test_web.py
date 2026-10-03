"""Web / REST API 测试（TestClient，全部本地 SQLite）。"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from fissue.models import (
    Category,
    DimensionScore,
    Evaluation,
    FileChange,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    QueueName,
    RawItem,
    RepoRef,
    Scores,
    VerifierKind,
    VerifierSpec,
)
from fissue.web.app import create_app


@pytest.fixture
def client(settings, repo, sample_issue):
    """已完成 lifespan 的测试客户端。"""
    from fastapi.testclient import TestClient

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)
    repo.save_evaluation(
        sample_issue.key,
        Evaluation(
            category=Category.BUG,
            priority=Priority.TIER1,
            model="test-model",
            summary="真实缺陷",
            scores=Scores(
                authenticity=DimensionScore(score=92, reason="有复现步骤"),
                importance=DimensionScore(score=88, reason="影响核心"),
                difficulty=DimensionScore(score=20),
                feasibility=DimensionScore(score=80),
            ),
        ),
    )
    repo.save_verifier(
        sample_issue.key,
        VerifierSpec(
            kind=VerifierKind.EXECUTABLE,
            name="repro",
            files={"tests/test_repro.py": "def test_x():\n    assert False\n"},
            command="python -m pytest tests/test_repro.py -q",
            notes="复现超时崩溃",
        ),
    )
    with TestClient(create_app(settings)) as c:
        c.repo = repo
        yield c


KEY = "github:psf/requests#42"
KEY_ENC = quote(KEY, safe="")


# ---------------------------------------------------------------------------
# 只读接口
# ---------------------------------------------------------------------------


def test_health_and_stats(client) -> None:
    assert client.get("/api/v1/health").json()["status"] == "ok"
    stats = client.get("/api/v1/stats").json()
    assert stats["total"] >= 1
    assert stats["by_category"]["bug"] >= 1
    assert "queues" in stats and "llm_usage_today" in stats


def test_list_items_and_filters(client) -> None:
    rows = client.get("/api/v1/items", params={"limit": 10}).json()
    assert rows["count"] >= 1
    assert rows["items"][0]["key"] == KEY

    assert client.get("/api/v1/items", params={"category": "feature"}).json()["count"] == 0
    assert client.get("/api/v1/items", params={"item_type": "pr"}).json()["count"] == 0
    assert client.get("/api/v1/items", params={"status": "new"}).json()["count"] == 0


def test_get_item_detail_has_scores_verifier_runs(client) -> None:
    detail = client.get(f"/api/v1/items/{KEY_ENC}").json()
    assert detail["key"] == KEY
    assert detail["evaluation"]["scores"]["importance"] == 88
    assert detail["evaluation"]["priority"] == "tier1"
    assert detail["verifier"]["kind"] == "executable"
    assert detail["verifier"]["command"].startswith("python -m pytest")
    assert detail["verifier_runs"] == []
    assert detail["fix_attempts"] == []
    assert detail["detail_url"] == f"/item/{KEY_ENC}"


def test_get_item_404(client) -> None:
    r = client.get(f"/api/v1/items/{quote('github:a/b#999', safe='')}")
    assert r.status_code == 404


def test_queues_endpoint(client) -> None:
    data = client.get("/api/v1/queues").json()
    assert set(data["pending"]) == {"verify", "fix_bug", "fix_feature"}
    assert "verify" in data["decisions"]
    assert data["decisions"]["verify"]["should_flush"] is False


def test_export_endpoints(client) -> None:
    js = client.get("/api/v1/export", params={"format": "json"}).json()
    assert js["count"] >= 1
    md = client.get("/api/v1/export", params={"format": "markdown"}).text
    assert "# Fissue 评测报告" in md


# ---------------------------------------------------------------------------
# 网页
# ---------------------------------------------------------------------------


def test_dashboard_renders_and_encodes_links(client) -> None:
    html = client.get("/").text
    assert "Fissue" in html
    assert "真实缺陷" in html
    assert quote(KEY, safe="") in html or "%23" in html


def test_item_page_renders_sections(client) -> None:
    html = client.get(f"/item/{KEY_ENC}").text
    assert "AI 评测" in html
    assert "验证器" in html
    assert "python -m pytest" in html
    assert "复现超时崩溃" in html


def test_item_page_404(client) -> None:
    assert client.get(f"/item/{quote('github:a/b#404', safe='')}").status_code == 404


# ---------------------------------------------------------------------------
# 鉴权与只读
# ---------------------------------------------------------------------------


def test_auth_required(settings) -> None:
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.api.auth_required = True
    s.api.token = "s3cret"
    with TestClient(create_app(s)) as c:
        assert c.get("/api/v1/health").status_code == 200        # health 不需要鉴权
        assert c.get("/api/v1/stats").status_code == 401
        assert c.get("/api/v1/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/v1/stats", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_read_only_blocks_actions(settings, repo, sample_issue) -> None:
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.web.read_only = True
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)
    with TestClient(create_app(s)) as c:
        assert c.get("/api/v1/items").status_code == 200          # 读仍然可以
        r = c.post("/api/v1/actions/evaluate", json={"keys": [KEY]})
        assert r.status_code == 403
        assert c.post("/api/v1/queues/flush", json={"force": True}).status_code == 403


# ---------------------------------------------------------------------------
# 动作接口
# ---------------------------------------------------------------------------


def test_flush_action_noop_when_empty(client) -> None:
    r = client.post("/api/v1/queues/flush", json={"queue": "verify", "force": True})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results[0]["queue"] == "verify"
    assert results[0]["flushed"] == 0


def test_evaluate_action_with_missing_key(client) -> None:
    r = client.post("/api/v1/actions/evaluate", json={"keys": ["github:a/b#777"]})
    assert r.status_code == 200
    assert r.json()["evaluated"] == 0


def test_fix_action_disabled(settings, repo) -> None:
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.auto_fix.enabled = False
    with TestClient(create_app(s)) as c:
        r = c.post("/api/v1/actions/fix", json={"dry_run": True})
        assert r.status_code == 403


def test_scan_action_without_repos(settings) -> None:
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.repos = []
    with TestClient(create_app(s)) as c:
        r = c.post("/api/v1/actions/scan", json={})
        assert r.status_code == 400


def test_approve_merge_rejects_non_pr(client) -> None:
    r = client.post("/api/v1/actions/approve-merge", params={"key": KEY}, json={"note": "ok"})
    assert r.status_code == 400          # sample_issue 是 Issue，不是 PR


def test_approve_merge_404(client) -> None:
    r = client.post("/api/v1/actions/approve-merge",
                    params={"key": "github:a/b#12345"}, json={})
    assert r.status_code == 404
