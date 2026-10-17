"""Web / REST API 测试（TestClient，全部本地 SQLite）。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from fissue.models import (
    Category,
    DimensionScore,
    Evaluation,
    Platform,
    Priority,
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


def test_invalid_enum_query_param_returns_400_not_500(client) -> None:
    """非法枚举查询参数是**客户端**错误 → 400 且说明可选值，而不是 500 + 堆栈。

    回归：这些参数原先直接 `ItemStatus(status)`：FastAPI 的 Query 只做 str→str，非法值在处理
    函数里抛 `ValueError`，被兜成 500——调用方看不出是自己参数写错了，只看到服务器故障。
    """
    r = client.get("/api/v1/items", params={"status": "nope"})
    assert r.status_code == 400, r.text
    assert "status" in r.json()["detail"] and "可选" in r.json()["detail"]

    r2 = client.get("/api/v1/items", params={"category": "nope"})
    assert r2.status_code == 400, r2.text

    # 合法取值照旧
    assert client.get("/api/v1/items", params={"status": "new"}).status_code == 200


def test_items_list_reports_real_comment_counts(client) -> None:
    """API 列表里的 `comments` 也要是真数（与导出同一口径）。"""
    data = client.get("/api/v1/items", params={"limit": 10}).json()
    assert data["items"], "夹具应当至少有一条条目"
    assert any(i["comments"] >= 1 for i in data["items"]), f"评论数不该全是 0：{data['items']}"


# ---------------------------------------------------------------------------
# 入站 webhook
# ---------------------------------------------------------------------------


def _github_headers(secret: str, body: bytes) -> dict[str, str]:
    import hashlib
    import hmac

    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def _github_payload(repo: str = "psf/requests", number: int = 42) -> bytes:
    import json

    return json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": repo},
            "issue": {"number": number},
        }
    ).encode()


@pytest.fixture
def hook_client(settings, repo, monkeypatch):
    """开了 webhook 的客户端；平台密钥固定 `hook-secret`，并把「后台那一轮」打桩。

    真跑那一轮会烧 token、还会写平台，所以这里只记录「被调度过」。
    """
    from fastapi.testclient import TestClient

    from fissue.service.daemon import FissueService
    from fissue.web.app import create_app

    settings.webhooks.enabled = True
    settings.webhooks.secrets.github = "hook-secret"
    settings.webhooks.secrets.gitlab = "hook-secret"

    rounds: list[str] = []

    async def fake_round(self) -> dict[str, Any]:      # noqa: ANN001
        rounds.append("round")
        return {}

    monkeypatch.setattr(FissueService, "run_once", fake_round)
    with TestClient(create_app(settings)) as c:
        c.rounds = rounds
        yield c


def test_webhook_endpoint_is_404_until_enabled(settings, repo, monkeypatch) -> None:
    """没开 webhook 时端点就当不存在（404），开了之后**同一请求必须变成 202**。

    为什么写成前后对比：单看「未启用 → 404」是**判别不出来**的——端点根本不存在时也是 404。
    只有把「开了就 202」摆在一起，才说明那个 404 来自「没开」而不是「没实现」。
    """
    from fastapi.testclient import TestClient

    from fissue.service.daemon import FissueService
    from fissue.web.app import create_app

    async def fake_round(self) -> dict[str, Any]:      # noqa: ANN001
        return {}

    monkeypatch.setattr(FissueService, "run_once", fake_round)
    body = _github_payload()
    headers = _github_headers("hook-secret", body)

    with TestClient(create_app(settings)) as c:
        r = c.post("/api/v1/webhooks/github", content=body, headers=headers)
        assert r.status_code == 404, r.text

    settings.webhooks.enabled = True
    settings.webhooks.secrets.github = "hook-secret"
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/v1/webhooks/github", content=body, headers=headers)
        assert r.status_code == 202, r.text


def test_webhook_rejects_a_bad_signature(hook_client) -> None:
    """签名不对一律 403，而且**不触发任何一轮**（否则任何人都能拿平台当跳板。"""
    r = hook_client.post(
        "/api/v1/webhooks/github",
        content=_github_payload(),
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert r.status_code == 403
    assert hook_client.rounds == [], "校验没过就不该调度任何工作"


def test_webhook_fails_closed_when_the_secret_is_missing(hook_client, settings) -> None:
    """密钥为空时**拒绝处理**（fail-closed）——这个端点没有别的身份证明。

    配置层在启动时就拒绝「开着却没配密钥」，这里守的是运行期被改掉/漏配的那条路。
    """
    body = _github_payload()
    headers = _github_headers("hook-secret", body)
    settings.webhooks.secrets.github = ""

    r = hook_client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert r.status_code == 403
    assert hook_client.rounds == []


def test_webhook_accepts_a_github_push_and_schedules_a_round(hook_client) -> None:
    """签名对 + 仓库已登记 → 202 立刻返回，并且**后台确实触发了一轮**。"""
    body = _github_payload()
    r = hook_client.post("/api/v1/webhooks/github", content=body, headers=_github_headers("hook-secret", body))

    assert r.status_code == 202, r.text
    data = r.json()
    assert data["accepted"] is True and data["repo"] == "psf/requests" and data["number"] == 42
    assert hook_client.rounds == ["round"], "应当调度一次后台一轮"


def test_webhook_ignores_an_unregistered_repo(hook_client) -> None:
    """未登记 / 未启用的仓库一律忽略——事件体说了不算。"""
    body = _github_payload(repo="someone/else")
    r = hook_client.post("/api/v1/webhooks/github", content=body, headers=_github_headers("hook-secret", body))

    assert r.status_code == 202
    assert r.json()["accepted"] is False
    assert hook_client.rounds == []


def test_webhook_ignores_non_item_events(hook_client) -> None:
    """不是条目类事件（比如 GitHub 的 ping）→ 202 但要写明忽略原因。"""
    import json

    body = json.dumps({"zen": "Design for failure.", "hook_id": 1}).encode()
    r = hook_client.post("/api/v1/webhooks/github", content=body, headers=_github_headers("hook-secret", body))

    assert r.status_code == 202
    assert r.json()["accepted"] is False
    assert hook_client.rounds == []


def test_webhook_rejects_unknown_platform_and_bad_json(hook_client) -> None:
    """未知平台 404；签名过了但事件体不是 JSON → 400（而不是 500）。"""
    assert hook_client.post("/api/v1/webhooks/nope", content=b"{}").status_code == 404

    bad = b"{not json"
    r = hook_client.post("/api/v1/webhooks/github", content=bad, headers=_github_headers("hook-secret", bad))
    assert r.status_code == 400


def test_webhook_works_behind_api_auth_without_a_bearer_token(hook_client, settings) -> None:
    """**这是这个端点的设计要点**：平台不会带 bearer 令牌，所以它在鉴权开启时也要照常工作。

    而普通 API 路由在同样设置下必须要求令牌——两条一起断言，免得将来有人「顺手」给 webhook
    挂上 `Depends(auth)`，把真实部署（尤其挂了反代）变成永远收不到推送。
    """
    settings.api.auth_required = True
    settings.api.token = "api-token"

    body = _github_payload()
    r = hook_client.post("/api/v1/webhooks/github", content=body, headers=_github_headers("hook-secret", body))
    assert r.status_code == 202, r.text

    assert hook_client.get("/api/v1/items").status_code == 401, "普通 API 路由仍必须要求令牌"


def test_webhook_accepts_a_gitlab_token_header(hook_client, settings) -> None:
    """GitLab 用的是共享密钥头（`X-Gitlab-Token`），不是 HMAC。"""
    import json

    from fissue.config import RepoConfig

    # 夹具里只登记了 github 的 psf/requests；平台必须对得上，所以补一条 gitlab 登记。
    settings.repos.append(RepoConfig(platform=Platform.GITLAB, owner="psf", name="requests"))
    body = json.dumps(
        {
            "object_attributes": {"iid": 7, "action": "open"},
            "project": {"path_with_namespace": "psf/requests"},
        }
    ).encode()

    r = hook_client.post(
        "/api/v1/webhooks/gitlab", content=body, headers={"X-Gitlab-Token": "hook-secret"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] is True and r.json()["item_type"] == "pr"

    bad = hook_client.post(
        "/api/v1/webhooks/gitlab", content=body, headers={"X-Gitlab-Token": "wrong"}
    )
    assert bad.status_code == 403


def test_parse_event_handles_the_v5_shapes() -> None:
    """GitLab / Gitee（v5 同形状）的两种事件体都要认得出来。"""
    from fissue.web.hooks import parse_event

    mr = parse_event(
        Platform.GITLAB,
        {"object_attributes": {"iid": 12, "action": "update"}, "project": {"path_with_namespace": "a/b"}},
    )
    assert (mr.repo_slug, mr.number, mr.item_type.value) == ("a/b", 12, "pr")

    issue = parse_event(
        Platform.GITEE,
        {"repository": {"full_name": "a/b"}, "issue": {"number": 3}},
    )
    assert (issue.repo_slug, issue.number, issue.item_type.value) == ("a/b", 3, "issue")

    nothing = parse_event(Platform.GITHUB, {"zen": "hi"})
    assert nothing.usable is False and nothing.reason


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
