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


def test_api_disabled_registers_no_rest_routes(settings) -> None:
    """`api.enabled=false` 时 REST 路由真的不注册（此前这个开关从未被读取）。

    关的是 API，不是整个 Web——仪表盘页面仍然在。
    """
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.api.enabled = False
    with TestClient(create_app(s)) as c:
        assert c.get(f"{s.api.prefix}/health").status_code == 404
        assert c.get(f"{s.api.prefix}/stats").status_code == 404
        assert c.get("/").status_code == 200, "页面不该被一起关掉"


# ---------------------------------------------------------------------------
# 写端点的互斥与「经代理进来」的拒绝
# ---------------------------------------------------------------------------


def test_write_endpoint_rejects_proxied_unauthenticated_request(client) -> None:
    """未开鉴权时，带反向代理转发头的写请求必须被拒。

    回归：`Settings._validate` 只能看**绑定地址**，所以 `web.host=127.0.0.1` + 同机 nginx
    反代到本端口时，无鉴权的写控制面（打平台标签、推进修复队列）其实已经对外开放。
    见到转发头就 403，把「静默开放」变成「响亮拒绝」。

    注意这是补刀、不是边界：真正的边界是 `FISSUE_API_TOKEN`。
    """
    r = client.post(
        "/api/v1/queues/flush",
        json={"queue": "verify", "force": True},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert r.status_code == 403, r.text
    assert "FISSUE_API_TOKEN" in r.text

    # 读接口不受影响（只读，没有副作用）
    assert client.get("/api/v1/stats", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200
    # 同样的写请求，不带转发头照常放行（本机直接访问）
    assert client.post("/api/v1/queues/flush", json={"queue": "verify", "force": True}).status_code == 200


def test_write_endpoint_409_when_another_writer_holds_the_gate(client, monkeypatch) -> None:
    """同进程里已有写操作在跑时，写端点回 409（而不是并行再跑一遍）。

    这一条守的是 `WRITE_GATE`：`fissue daemon --web` 把 uvicorn 与 daemon 的 flush/fix
    循环放在同一个事件循环里，`flock` 那侧走的是重入旁路，挡不住同进程的另一处写操作。
    """
    from fissue import lockfile

    monkeypatch.setattr(lockfile.WRITE_GATE, "_owner", object())    # 假装别的任务正持有

    r = client.post("/api/v1/queues/flush", json={"queue": "verify", "force": True})
    assert r.status_code == 409, r.text


def test_write_endpoint_rejects_a_remote_peer_when_unauthenticated(settings, repo) -> None:
    """未开鉴权时，**来源不是回环**的写请求一律 403。

    启动检查读的是 `web.host`，把 `create_app()` 嵌进别人的服务器（或换绑定地址、经
    `docker -p` 暴露）时它看不见；这一道直接看对端地址，把那种情况兜住。读接口不受影响。
    """
    from fastapi.testclient import TestClient

    from fissue.web.app import create_app

    with TestClient(create_app(settings), client=("203.0.113.9", 4321)) as c:
        r = c.post("/api/v1/queues/flush", json={"queue": "verify", "force": True})
        assert r.status_code == 403, r.text
        assert "FISSUE_API_TOKEN" in r.text
        assert c.get("/api/v1/stats").status_code == 200, "只读接口不受影响"


def test_write_endpoint_allows_an_unparseable_peer(client) -> None:
    """对端地址解析不出来时**不拦**（测试客户端/UDS 都是这种）。

    「不知道对端是谁」不等于「可疑」——拿这一点去拦会把本机正常用法也挡掉。
    """
    r = client.post("/api/v1/queues/flush", json={"queue": "verify", "force": True})
    assert r.status_code == 200, r.text
