"""Web / REST API 测试（TestClient，全部本地 SQLite）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import pytest

from fissue.models import (
    Category,
    DimensionScore,
    Evaluation,
    ItemStatus,
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

    async def fake_round(self, **_kwargs: Any) -> dict[str, Any]:      # noqa: ANN001
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

    async def fake_round(self, **_kwargs: Any) -> dict[str, Any]:      # noqa: ANN001
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


def _seed_admin(settings) -> None:
    """给这个库造一个管理员账号。

    为什么这几条用例非造不可：「还没初始化（没有管理员）」与「你有身份但没带」是**两种**
    状态——前者回 403 + 引导口令指引（登录也不可能成功），后者回 401（去登录就行）。
    不造账号时测到的是前者，断言 401 会失败，而那个失败看着像鉴权坏了。
    """
    from fissue.auth import AuthStore, hash_password
    from fissue.store.db import build_database

    database = build_database(settings)
    database.create_all()
    AuthStore(database).create_user("admin", hash_password("s3cret-pass"))


def test_webhook_works_without_an_identity_on_a_remote_peer(settings, repo, monkeypatch) -> None:
    """**这是这个端点的设计要点**：平台推送时既没有会话、也不会带 Bearer 令牌，
    所以它在鉴权开启、且来源是外网时也必须照常工作。

    这里必须用**非回环**来源来断言：回环本来就免登录，拿它测不出「webhook 有没有被例外放行」。
    而普通 API 路由在同样条件下必须要求身份——两条一起断言，免得将来有人「顺手」给
    webhook 挂上 `Depends(REQUIRE_AUTH)`，把真实部署（尤其挂了反代）变成永远收不到推送。
    """
    from fastapi.testclient import TestClient

    from fissue.service.daemon import FissueService
    from fissue.web.app import create_app

    _seed_admin(settings)
    settings.webhooks.enabled = True
    settings.webhooks.secrets.github = "hook-secret"

    async def fake_round(self, **_kwargs: Any) -> dict[str, Any]:      # noqa: ANN001
        return {}

    monkeypatch.setattr(FissueService, "run_once", fake_round)
    body = _github_payload()
    with TestClient(create_app(settings), client=("203.0.113.9", 4321)) as remote:
        r = remote.post(
            "/api/v1/webhooks/github", content=body, headers=_github_headers("hook-secret", body)
        )
        assert r.status_code == 202, r.text

        assert remote.get("/api/v1/items").status_code == 401, "普通 API 路由仍必须要求身份"


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


def test_overview_and_items_pages_render(client) -> None:
    """总览与「条目」是两个页面：前者回答「有没有问题」，后者回答「这一条是什么」。

    原先什么都堆在一页上——筛选器一铺，告警就被挤到看不见的地方。
    """
    overview = client.get("/").text
    assert "Fissue" in overview
    assert "总览" in overview

    items = client.get("/items").text
    assert "真实缺陷" in items
    assert quote(KEY, safe="") in items or "%23" in items


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
    """授权矩阵：回环免登录；非回环必须有会话或 API Key（docs/AUTH.md §3.1）。"""
    from fastapi.testclient import TestClient

    from fissue.auth import AuthStore, generate_api_key, hash_token
    from fissue.store.db import build_database

    s = settings.model_copy(deep=True)
    _seed_admin(s)
    database = build_database(s)
    database.create_all()
    store = AuthStore(database)
    prefix, secret, plaintext = generate_api_key()
    store.create_key(name="ci", prefix=prefix, key_hash=hash_token(secret))

    with TestClient(create_app(s)) as local:
        # health 无副作用，任何来源都免鉴权（探活要用它）
        assert local.get("/api/v1/health").status_code == 200
        # 回环：三种来源里最省事的那个，免登录
        assert local.get("/api/v1/stats").status_code == 200

    with TestClient(create_app(s), client=("203.0.113.9", 4321)) as remote:
        assert remote.get("/api/v1/health").status_code == 200
        assert remote.get("/api/v1/stats").status_code == 401, "没有身份的对外请求一律拒绝"
        assert remote.get(
            "/api/v1/stats", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert remote.get(
            "/api/v1/stats", headers={"Authorization": f"Bearer {plaintext}"}
        ).status_code == 200


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
    """带反向代理转发头、又没有声明可信网段的写请求必须被拒。

    回归：`Settings._validate` 只能看**绑定地址**，所以 `web.host=127.0.0.1` + 同机 nginx
    反代到本端口时，写控制面（打平台标签、推进修复队列）其实已经对外开放。见到转发头就 403，
    把「静默开放」变成「响亮拒绝」。

    注意这是补刀、不是边界：真正的边界是**身份**（会话或 API Key）。这条对**回环来源**
    同样生效，而且正是它的主场——反代后面时，对端地址就是 127.0.0.1。
    """
    r = client.post(
        "/api/v1/queues/flush",
        json={"queue": "verify", "force": True},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert r.status_code == 403, r.text
    assert "反向代理" in r.text

    # 读接口不受影响（只读，没有副作用）
    assert client.get("/api/v1/stats", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200
    # 同样的写请求，不带转发头照常放行（本机直接访问）
    assert client.post("/api/v1/queues/flush", json={"queue": "verify", "force": True}).status_code == 200


def test_write_endpoint_409_when_another_writer_holds_the_gate(client, monkeypatch) -> None:
    """同进程里已有写操作在跑时，写端点回 409（而不是并行再跑一遍）。

    这一条守的是 `WRITE_GATE`：`fissue serve --with-web` 把 uvicorn 与 daemon 的 flush/fix
    循环放在同一个事件循环里，`flock` 那侧走的是重入旁路，挡不住同进程的另一处写操作。
    """
    from fissue import lockfile

    monkeypatch.setattr(lockfile.WRITE_GATE, "_owner", object())    # 假装别的任务正持有

    r = client.post("/api/v1/queues/flush", json={"queue": "verify", "force": True})
    assert r.status_code == 409, r.text


def test_write_endpoint_rejects_a_remote_peer_when_unauthenticated(settings, repo) -> None:
    """**来源不是回环/可信网段**且没有身份的写请求一律 403。

    启动检查读的是 `web.host`，把 `create_app()` 嵌进别人的服务器（或换绑定地址、经
    `docker -p` 暴露）时它看不见；这一道直接看对端地址，把那种情况兜住。读接口同样要求身份
    （`/api/*` 一律过鉴权依赖），而 `/health` 免鉴权。
    """
    from fastapi.testclient import TestClient

    from fissue.web.app import create_app

    _seed_admin(settings)
    with TestClient(create_app(settings), client=("203.0.113.9", 4321)) as c:
        r = c.post("/api/v1/queues/flush", json={"queue": "verify", "force": True})
        assert r.status_code == 401, r.text
        assert "/login" in r.text, "要把「怎么才能进」说清楚"
        assert c.get("/api/v1/stats").status_code == 401
        assert c.get("/api/v1/health").status_code == 200


def test_write_endpoint_allows_an_unparseable_peer(client) -> None:
    """对端地址解析不出来时**不拦**（测试客户端/UDS 都是这种）。

    「不知道对端是谁」不等于「可疑」——拿这一点去拦会把本机正常用法也挡掉。
    """
    r = client.post("/api/v1/queues/flush", json={"queue": "verify", "force": True})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 新的展示能力：正文 / 评论 / 真实状态 / 搜索 / 分页 / 静态资源
# ---------------------------------------------------------------------------


def test_item_payload_carries_body_comments_and_pipeline_status(client) -> None:
    """详情 payload 要带正文、评论正文与流水线状态。

    回归：`item_to_dict` 原先只发一个评论**计数**，正文与评论内容整颗丢掉——一个分诊工具，
    打开一条 Issue 却读不到它说了什么，只能点「在平台打开」跳出去；而列表 payload 里
    连 `status` 都没有，于是看板的「状态」列只能拿「有没有评测」凑数。
    """
    detail = client.get(f"/api/v1/items/{KEY_ENC}").json()
    assert "复现步骤" in detail["body"]
    assert detail["status"] in {s.value for s in ItemStatus}
    assert detail["comments"] == 1
    assert detail["comment_thread"][0]["author"] == "maintainer"
    assert "已确认" in detail["comment_thread"][0]["body"]


def test_item_page_renders_body_and_comment_thread(client) -> None:
    html = client.get(f"/item/{KEY_ENC}").text
    assert "复现步骤" in html, "详情页必须能读到正文"
    assert "已确认，我来看看" in html, "详情页必须能看到评论内容"
    assert "讨论（1 条）" in html


def test_items_list_reports_total_and_searches(client) -> None:
    """列表要给出**筛选后的总数**与搜索。

    分页靠 `total`：`count` 只是这一页的条数，没有 total 就看不出「这 1 条是全部、
    还是共 300 条里的第 1 条」。
    """
    page = client.get("/api/v1/items", params={"limit": 1}).json()
    assert page["count"] == 1
    assert page["total"] >= 1
    assert page["limit"] == 1 and page["offset"] == 0

    assert client.get("/api/v1/items", params={"q": "崩溃"}).json()["count"] == 1
    miss = client.get("/api/v1/items", params={"q": "绝对不存在的关键词"}).json()
    assert miss["count"] == 0 and miss["total"] == 0


def test_items_list_survives_an_unknown_sort_key(client) -> None:
    """排序键非法不炸。

    `order_by` 原先走 `getattr(ItemRow, name)`，而它拿到的不一定是列——`metadata` 是
    MetaData、`query` 是描述符，都没有 `.desc()`，于是这个参数一旦来自查询串就是 500。
    """
    assert client.get("/api/v1/items", params={"sort": "metadata"}).status_code == 200


def test_static_assets_are_served(client) -> None:
    """样式与脚本是挂出来的静态文件，不再各自内联在两个模板里。"""
    css = client.get("/static/app.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200 and js.status_code == 200
    assert "--accent" in css.text
    assert "window.Fissue" in js.text


def test_overview_shows_alerts_queue_and_usage(client) -> None:
    """总览要落地后端一直算好、旧版却没渲染的东西：告警、队列积压、模型用量。"""
    overview = client.get("/").text
    assert "队列积压" in overview
    assert "模型用量" in overview
    assert "控制面板" in overview
    assert "待定论" in overview
    # 配置告警：这台测试机没配平台 token，必须有一条 warn 提醒（新手最缺的就是这个提示）
    assert "alert" in overview and "未配置任何平台 Token" in overview
    # 最近抓取：scan_runs 一直有写、此前没有读入口
    assert "最近抓取" in overview


def test_items_table_shows_the_real_pipeline_status(client) -> None:
    """表格里的状态列必须是真实的流水线状态。

    旧版写的是 `row.evaluation and '已评测' or '待评测'`——筛了 status、表格里却看不到
    status，而且当时 payload 里根本没有它。
    """
    status = client.get(f"/api/v1/items/{KEY_ENC}").json()["status"]
    items = client.get("/items").text
    assert f"status-{status}" in items, "表格里必须看得到真实的流水线状态"
    assert "待评测</span>" not in items, "旧的假状态列不该回来"


def test_items_page_renders_a_page_not_json_for_a_bad_filter(client) -> None:
    """筛选值非法时页面照常渲染并提示，而不是给浏览器一坨 JSON。

    `_enum_param` 抛的 400 是给 API 用的错误体；同一套解析也挂在 HTML 路由上，
    原先 `?status=foo` 会让用户看到一个 JSON 错误页。
    """
    r = client.get("/items", params={"status": "nope"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "取值非法" in r.text and "可选" in r.text
    # API 那边仍然是 400 —— 那次真的是客户端的错，而且要写清可选值
    api = client.get("/api/v1/items", params={"status": "nope"})
    assert api.status_code == 400 and "status" in api.json()["detail"]


def test_items_page_paginates_and_keeps_filters_in_sort_links(client, sample_issue) -> None:
    """分页可见，且表头排序链接**带着当前筛选**。

    手拼 URL 漏一个参数，用户一点排序就把筛选条件丢了——所以这串是 Python 侧编码好交付的。
    """
    rid = client.repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    client.repo.upsert_item(rid, sample_issue.model_copy(update={"number": 77, "title": "第二个条目"}))

    page1 = client.get("/items", params={"limit": 1, "page": 1}).text
    page2 = client.get("/items", params={"limit": 1, "page": 2}).text
    assert "第 1–1 条 / 共 2 条" in page1
    assert "第 2–2 条 / 共 2 条" in page2

    filtered = client.get("/items", params={"item_type": "issue", "limit": 1, "sort": "title"}).text
    assert "item_type=issue" in filtered, "排序链接必须带上当前筛选"
    assert "sort=title" in filtered


# ---------------------------------------------------------------------------
# 作业（长任务）：提交 / 轮询 / 互斥 / 只读
# ---------------------------------------------------------------------------


def _wait_for_job(client, job_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """轮询到作业结束。"""
    deadline = time.monotonic() + timeout
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    while job["live"] and time.monotonic() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/v1/jobs/{job_id}").json()
    return job


def test_job_lifecycle_reports_progress_and_result(client) -> None:
    """提交 → 202 + job id → 轮询到结束。

    用 flush 做样本：队列空时它不发 LLM、不进沙盒，几毫秒就结束，正好能测「作业化」本身
    （提交、进度日志、结果、结束态）。
    """
    r = client.post("/api/v1/jobs/flush", json={"queue": "verify", "force": True})
    assert r.status_code == 202, r.text
    job_id = r.json()["job"]["id"]
    assert job_id

    job = _wait_for_job(client, job_id)
    assert job["status"] == "done", job
    assert job["kind"] == "flush"
    assert job["log"], "作业应当留下进度日志（前端就靠它显示进行到哪一步）"
    assert job["result"]["results"][0]["queue"] == "verify"
    assert job["duration_seconds"] >= 0

    assert [j["id"] for j in client.get("/api/v1/jobs").json()["jobs"]][0] == job_id


def test_job_unknown_kind_lists_the_valid_ones(client) -> None:
    r = client.post("/api/v1/jobs/rm-rf", json={})
    assert r.status_code == 400
    assert "flush" in r.json()["detail"] and "可选" in r.json()["detail"]


def test_job_404_for_an_unknown_id(client) -> None:
    assert client.get("/api/v1/jobs/nope").status_code == 404


def test_second_job_is_rejected_while_one_is_running(client) -> None:
    """同进程同时只允许一个作业：并行跑只会重复调 LLM、重复写平台。"""
    from fissue.web import jobs as jobs_mod

    registry = client.app.state.jobs
    busy = jobs_mod.Job(id="busy", kind="flush")
    busy.status = jobs_mod.RUNNING
    registry._jobs["busy"] = busy
    try:
        r = client.post("/api/v1/jobs/flush", json={})
        assert r.status_code == 409, r.text
        assert "已有任务在运行" in r.json()["detail"]
    finally:
        registry._jobs.pop("busy", None)


def test_a_running_job_holds_the_write_gate(client, monkeypatch) -> None:
    """作业在跑时，同步写端点是 409 —— 证明闸门是**作业自己**全程握着的。

    如果闸门只是「提交作业的那个请求」握的，请求一返回就放了，而作业还在跑：并发的第二次写
    就能同时进行，重复调 LLM、重复写平台标签——正是 `WRITE_GATE` 要防的事。
    """
    import threading

    from fissue.web import app as webapp

    started = threading.Event()
    release = threading.Event()

    async def slow_work(app, params, report):  # noqa: ANN001, ARG001
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        return {"ok": True}

    monkeypatch.setitem(webapp._JOB_WORK, "flush", slow_work)
    r = client.post("/api/v1/jobs/flush", json={})
    assert r.status_code == 202, r.text
    job_id = r.json()["job"]["id"]
    try:
        assert started.wait(5), "作业没跑起来"
        assert client.post("/api/v1/queues/flush", json={"force": True}).status_code == 409
        assert client.get("/api/v1/stats").status_code == 200, "只读不受影响"
    finally:
        release.set()
    assert _wait_for_job(client, job_id)["status"] == "done"


def test_jobs_are_blocked_in_read_only(settings, repo, sample_issue) -> None:
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.web.read_only = True
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)
    with TestClient(create_app(s)) as c:
        r = c.post("/api/v1/jobs/flush", json={})
        assert r.status_code == 403
        assert c.get("/api/v1/jobs").status_code == 200, "只读时看历史任务仍是只读操作"


# ---------------------------------------------------------------------------
# 取消作业
# ---------------------------------------------------------------------------


def _slow_job(monkeypatch, *, started: Any, release: Any) -> None:
    """把 flush 换成「一直跑，直到 release 被置位」的作业，用来测取消。"""
    from fissue.web import app as webapp

    async def slow_work(app, params, report):  # noqa: ANN001, ARG001
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        return {"ok": True}

    monkeypatch.setitem(webapp._JOB_WORK, "flush", slow_work)


def test_cancel_a_running_job(client, monkeypatch) -> None:
    """取消一个正在跑的作业：状态变 canceled，并且**写闸门被释放**。

    闸门这条是关键：取消靠 `task.cancel()` 在下一个 await 点投递 CancelledError，
    作业的 `async with` 随之退出并放锁。如果实现里手工解锁或漏了某条路径，
    闸门会被永久占住——之后任何写操作都 409，而页面上只会看到「一直失败」。
    """
    import threading

    started, release = threading.Event(), threading.Event()
    _slow_job(monkeypatch, started=started, release=release)

    job_id = client.post("/api/v1/jobs/flush", json={}).json()["job"]["id"]
    assert started.wait(5), "作业没跑起来"

    r = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    assert "已请求取消" in r.json()["note"]

    job = _wait_for_job(client, job_id)
    assert job["status"] == "canceled", job
    assert "取消" in job["error"]
    assert job["live"] is False

    # 闸门必须已经放开：能再提交一个（原实现若漏了解锁，这里会 409）
    release.set()
    again = client.post("/api/v1/jobs/flush", json={})
    assert again.status_code == 202, f"闸门没释放：{again.text}"
    _wait_for_job(client, again.json()["job"]["id"])


def test_cancel_rejects_finished_and_unknown_jobs(client) -> None:
    """已经结束的作业没什么可取消（409），不存在的作业是 404——两者要分清。"""
    job_id = _wait_for_job(client, client.post("/api/v1/jobs/flush", json={}).json()["job"]["id"])
    assert not job_id["live"]

    r = client.post(f"/api/v1/jobs/{job_id['id']}/cancel")
    assert r.status_code == 409, r.text
    assert "已经结束" in r.json()["detail"]

    assert client.post("/api/v1/jobs/nope/cancel").status_code == 404


def test_cancel_is_blocked_in_read_only(settings, repo, sample_issue) -> None:
    """只读部署不该能取消任务——它和提交任务是同一类「写」。"""
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.web.read_only = True
    with TestClient(create_app(s)) as c:
        assert c.post("/api/v1/jobs/whatever/cancel").status_code == 403


# ---------------------------------------------------------------------------
# 删除条目
# ---------------------------------------------------------------------------


def test_delete_item_removes_related_and_queued_rows(client) -> None:
    """删除条目要连关联数据一起清干净。

    关联分两类，处理方式不同（见 `Repository.delete_items`）：
    * `comments` / `evaluations` / `verifiers` 有 `items.id` 的 CASCADE 外键 → 数据库会自动带走；
    * `verifier_runs` / `fix_attempts` / `batch_conclusions` **只按 item_key 字符串关联、没有外键**
      → 必须显式删。否则同一条被重新抓回来（key 由平台+仓库+编号决定，不变）时，
      这些旧记录会冒充它的「历史」：验证结果、修复尝试全都张冠李戴。
    """
    from fissue.models import FixAttempt, FixOutcome, QueueName, VerifierOutcome, VerifierRun

    repo = client.app.state.repo
    verifier = repo.latest_verifier(KEY)
    assert verifier is not None
    repo.save_verifier_run(
        VerifierRun(
            verifier_id=verifier[0], item_key=KEY, stage="base", outcome=VerifierOutcome.FAIL
        )
    )
    repo.save_fix_attempt(FixAttempt(item_key=KEY, outcome=FixOutcome.SKIPPED))
    repo.enqueue(QueueName.VERIFY, KEY)

    # 动手之前，这些数据确实都在
    assert repo.get_item(KEY) is not None
    assert repo.comment_counts([KEY])[KEY] == 1
    assert repo.latest_evaluation(KEY) is not None
    assert len(repo.verifier_runs(KEY)) == 1
    assert repo.fix_attempts(KEY)
    assert repo.queue_pending_count(QueueName.VERIFY) >= 1

    r = client.delete(f"/api/v1/items/{KEY_ENC}")
    assert r.status_code == 200, r.text
    counts = r.json()["counts"]
    assert counts["items"] == 1
    assert counts["verifier_runs"] == 1, counts
    assert counts["fix_attempts"] == 1, counts
    assert counts["queue_entries"] >= 1, counts
    assert counts["comments"] == 1 and counts["evaluations"] == 1, counts
    assert "平台" in r.json()["note"], "要讲清只删了本地数据"

    # 动手之后，一条都不该留下
    assert repo.get_item(KEY) is None
    assert repo.comment_counts([KEY]) == {}
    assert repo.latest_evaluation(KEY) is None
    assert repo.latest_verifier(KEY) is None
    assert repo.verifier_runs(KEY) == []
    assert repo.fix_attempts(KEY) == []
    assert repo.queue_pending_count(QueueName.VERIFY) == 0


def test_delete_nonexistent_item_is_404(client) -> None:
    assert client.delete(f"/api/v1/items/{quote('github:a/b#999', safe='')}").status_code == 404


def test_purge_items_reports_deleted_and_missing(client) -> None:
    """批量删除：不存在的 key 不该让整批失败，但要如实列在 missing 里。"""
    missing_key = "github:psf/requests#999"
    r = client.post("/api/v1/items/purge", json={"keys": [KEY, missing_key]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == [KEY]
    assert body["missing"] == [missing_key]
    assert body["counts"]["items"] == 1
    assert client.app.state.repo.get_item(KEY) is None


def test_purge_items_rejects_an_empty_key_list(client) -> None:
    """空 keys 是调用方搞错了，必须报错——静默「删了 0 条」会被当成成功。"""
    r = client.post("/api/v1/items/purge", json={"keys": []})
    assert r.status_code == 400
    assert "keys" in r.json()["detail"]


def test_delete_item_is_blocked_in_read_only(settings, repo, sample_issue) -> None:
    from fastapi.testclient import TestClient

    s = settings.model_copy(deep=True)
    s.web.read_only = True
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)
    with TestClient(create_app(s)) as c:
        assert c.delete(f"/api/v1/items/{KEY_ENC}").status_code == 403


# ---------------------------------------------------------------------------
# 新功能的界面入口（只有后端、没有按钮 = 功能等于不存在）
# ---------------------------------------------------------------------------


def test_cancel_button_is_present_on_job_pages(client) -> None:
    """「取消」按钮要在总览的任务面板与「任务」页都出现，且接线在共用的 app.js 里。

    接线做成共用的一处（`data-act=cancel-job` + app.js 的委托监听）：两个页面各写一份
    onclick 就是两份会分叉的实现——这个坑本项目已经在 `act()` 上踩过一次。
    """
    from fissue.web import jobs as jobs_mod

    js = client.get("/static/app.js").text
    assert "function cancelJob" in js
    assert "data-act=cancel-job" in js, "委托监听应当在 app.js 里（两页共用）"

    # 按钮只在作业**真的在跑**时出现，所以先造一个在跑的（夹具里没有作业）。
    registry = client.app.state.jobs
    live = jobs_mod.Job(id="live1", kind="scan")
    live.status = jobs_mod.RUNNING
    registry._jobs["live1"] = live
    try:
        for path in ("/", "/jobs"):
            html = client.get(path).text
            assert 'data-act="cancel-job" data-job="live1"' in html, f"{path} 上没有取消按钮"
    finally:
        registry._jobs.pop("live1", None)

    # 反面：没有在跑的任务时不该出现——否则用户点下去只会拿到 409。
    assert 'data-act="cancel-job"' not in client.get("/jobs").text


def test_item_page_renders_the_title_as_the_heading(client) -> None:
    """详情页要把条目标题当标题渲染。

    重做模板时漏了 `{% block heading %}`，h1 于是一直回落到 base.html 的「Fissue」——
    只能从浏览器标签页里看出在看哪一条，页面正文里完全没有标题（相对旧模板的功能回归）。
    """
    import re

    html = client.get(f"/item/{KEY_ENC}").text
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    assert match is not None, "详情页没有 h1"
    assert "程序崩溃" in match.group(1), f"标题没进 h1：{match.group(1)[:60]!r}"


def test_delete_entry_points_are_present(client) -> None:
    """两个删除入口：条目页的「删除选中」与详情页的「删除本地数据」，都要二次确认。"""
    items = client.get("/items").text
    assert 'onclick="bulkDelete()"' in items
    assert "function bulkDelete" in items
    assert "Fissue.confirm(" in items, "破坏性操作必须二次确认"

    detail = client.get(f"/item/{KEY_ENC}").text
    assert 'onclick="deleteItem()"' in detail
    assert "function deleteItem" in detail
    assert "Fissue.confirm(" in detail
    # 措辞必须点明「只删本地」——否则用户会以为平台上的 Issue 也被删了
    assert "本地数据" in detail


# ---------------------------------------------------------------------------
# 「谁在写」要说清楚：角标不能只说一句「写操作进行中」
# ---------------------------------------------------------------------------
#
# 真机报告：角标显示「后台有写操作进行中」，但任务页看不到任何任务、也无法取消，而 token
# 一直在走。查下来是**常驻服务的定时轮次**在整轮持锁——它不在 JobRegistry 里，所以那张表是
# 空的，取消按钮也够不着它。下面这几条守的是「界面必须把这件事说明白」。


def _fake_writer(monkeypatch, **fields):
    """把写闸门的持有者换成一个受控替身（不必真的去抢锁）。"""
    from fissue import lockfile

    status = lockfile.WriterStatus(**{"busy": True, **fields})
    monkeypatch.setattr(lockfile.WRITE_GATE, "describe", lambda: status)
    return status


def test_status_reports_who_holds_the_write_gate(client, monkeypatch) -> None:
    """`/status` 要给出「谁在写、哪一类、跑了多久」，而不是只报一个 pid。"""
    from datetime import datetime, timezone

    _fake_writer(
        monkeypatch,
        who="常驻服务：扫描轮次",
        kind="round",
        since=datetime(2026, 10, 19, 12, 47, 41, tzinfo=timezone.utc),
        seconds=252.4,
    )

    writer = client.get("/api/v1/status").json()["writer"]

    assert writer["busy"] is True
    assert writer["who"] == "常驻服务：扫描轮次"
    assert writer["kind"] == "round"
    assert writer["kind_label"] == "常驻服务轮次", "页面直接用这个中文，别让它自己猜 kind"
    assert writer["seconds"] == 252.4
    assert writer["since"].startswith("2026-10-19T12:47:41")


def test_jobs_page_explains_a_writer_that_is_not_a_job(client, monkeypatch) -> None:
    """轮次在写时，任务页要明说「它不在这张表里」，并指出可以**就地**停掉它。"""
    _fake_writer(monkeypatch, who="常驻服务：扫描轮次", kind="round", seconds=252.4)

    html = client.get("/jobs").text

    assert "不在本表" in html
    assert "常驻服务轮次" in html and "常驻服务：扫描轮次" in html
    assert "252" in html, "已跑时长要显示出来"
    assert 'data-act="stop-round"' in html, "要给出停它的入口（原先只能停掉整个服务）"


def test_jobs_page_keeps_quiet_when_the_writer_is_a_job(client, monkeypatch) -> None:
    """作业在写时**不要**弹那条提示——那时表格里就是它自己。"""
    _fake_writer(monkeypatch, who="作业 scan job-1", kind="job", seconds=3.0)

    html = client.get("/jobs").text

    assert "不在本表" not in html


def test_frontend_shows_who_is_writing_and_for_how_long(client) -> None:
    """前端两处（状态页写闸门、全站角标）都要把种类与时长显示出来。

    它们是前端渲染的，所以只能守到「脚本里有这段接线」——真机那次的病根正是这段接线缺失：
    `who` 只有一个 pid、也没有时长，用户看不出是谁在写、更判断不出卡没卡。
    """
    status_html = client.get("/status").text
    assert "writerText" in status_html and "kind_label" in status_html

    app_js = client.get("/static/app.js").text
    assert "writerDur" in app_js and "kind_label" in app_js


# ---------------------------------------------------------------------------
# 停止当前轮次：常驻轮次停得掉，跨进程的停不了（不许给一个按下去必然失败的按钮）
# ---------------------------------------------------------------------------


def test_stop_round_reports_nothing_to_stop(client) -> None:
    """没有在跑的轮次 → 409，并说清「可能刚结束，或在另一个进程里」。"""
    res = client.post("/api/v1/round/stop")

    assert res.status_code == 409
    assert "没有在跑的轮次" in res.text


def test_stop_round_stops_the_current_round(client, monkeypatch) -> None:
    """有轮次在跑 → 200 + 告知停的是哪一轮、跑了多久、以及打断的代价。"""
    from datetime import datetime, timezone

    from fissue.service import control

    stopped = control.RoundInfo("扫描轮次", datetime.now(timezone.utc), 252.4)
    monkeypatch.setattr(control.ROUNDS, "request_stop", lambda: stopped)

    res = client.post("/api/v1/round/stop")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["stopped"] == "扫描轮次"
    assert body["seconds"] == 252.4
    assert "只完成一半" in body["note"], "打断的代价必须如实告知"


def test_stop_round_requires_an_identity(settings) -> None:
    """非回环来源没有身份就停不了——停轮次也是控制面操作。"""
    from fastapi.testclient import TestClient

    from fissue.web.app import create_app

    _seed_admin(settings)
    with TestClient(create_app(settings), client=("203.0.113.9", 4321)) as remote:
        assert remote.post("/api/v1/round/stop").status_code == 401


def test_jobs_page_offers_stopping_a_round(client, monkeypatch) -> None:
    """轮次在写时给出「停止这一轮」按钮（原先只能去终端 Ctrl-C 停掉整个服务）。"""
    _fake_writer(monkeypatch, who="常驻服务：扫描轮次", kind="round", seconds=252.4)

    html = client.get("/jobs").text

    assert 'data-act="stop-round"' in html
    assert "只完成一半" in html, "打断的代价要写在按钮旁边"


def test_jobs_page_has_no_stop_button_for_another_process(client, monkeypatch) -> None:
    """跨进程的写操作**不给**按钮：那份任务在别的进程的内存里，按下去只会 409。"""
    _fake_writer(
        monkeypatch,
        who="pid=123 开始于 20:47:41：抓取/评测/验证/修复",
        kind="cli",
        seconds=None,
    )

    html = client.get("/jobs").text

    assert "不在本表" in html, "仍要解释它是谁"
    assert 'data-act="stop-round"' not in html


# ---------------------------------------------------------------------------
# 聚合状态缓存（web/cache.py）
# ---------------------------------------------------------------------------


def test_queue_panel_does_not_issue_per_queue_queries(repo, settings, monkeypatch) -> None:
    """队列面板必须走单查询的 ``queue_panel_summary``，不再逐口径发查询。

    这是页面提速的根因之一：旧拼法每渲染一次面板就是 10 个数据库往返，
    远端 PostgreSQL 上仅面板一项就是秒级。
    """
    from fissue.models import QueueName
    from fissue.web.status import queue_panel

    calls: list[str] = []
    for name in ("queue_stats", "queue_pending_count", "queue_unflushed", "oldest_unflushed_age"):
        original = getattr(repo, name)

        def _make(_name: str, _orig) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                calls.append(_name)
                return _orig(*args, **kwargs)

            return wrapper

        monkeypatch.setattr(repo, name, _make(name, original))

    panel = queue_panel(repo, settings)

    assert calls == [], f"面板不该再逐口径查询，却调用了：{calls}"
    assert [p["name"] for p in panel] == [q.value for q in QueueName]


def test_stats_cache_refreshes_in_background_and_bump_wakes_it() -> None:
    """后台线程定期刷新；bump 能提前唤醒一轮刷新；前台读取永不阻塞。"""
    from fissue.web.cache import StatsCache

    builds = 0

    def build() -> dict[str, Any]:
        nonlocal builds
        builds += 1
        return {"n": builds}

    cache = StatsCache(build, interval=0.2)
    cache.start()
    try:
        # 首份快照由后台线程产出（冷启动的 get() 同步兜底也算数）
        deadline = time.monotonic() + 3
        snap = None
        while time.monotonic() < deadline and (snap := cache.get()) is None:
            time.sleep(0.02)
        assert snap is not None
        first = snap["n"]

        # bump 触发提前刷新：下一份快照的 n 必须变
        cache.bump()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and cache.get()["n"] == first:
            time.sleep(0.02)
        assert cache.get()["n"] > first
        assert cache.build_count >= 2
    finally:
        cache.stop()


def test_stats_cache_build_failure_keeps_stale_snapshot() -> None:
    """刷新失败（数据库抖动）时保留旧快照：看板宁要 2 秒前的数字，不要空白。"""
    from fissue.web.cache import StatsCache

    state = {"fail": False}
    attempts = 0

    def build() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if state["fail"]:
            raise RuntimeError("db down")
        return {"ok": True}

    cache = StatsCache(build, interval=30.0)  # 长周期：只靠 bump 驱动第二次刷新
    cache.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and cache.get() is None:
            time.sleep(0.02)
        assert cache.get() == {"ok": True}
        ok_count = cache.build_count

        state["fail"] = True
        cache.bump()
        before_attempts = attempts
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and attempts <= before_attempts:
            time.sleep(0.05)
        # 失败的那轮刷新跑过且未计入成功次数；前台仍拿到旧快照
        assert attempts > before_attempts
        assert cache.build_count == ok_count
        assert cache.get() == {"ok": True}
    finally:
        cache.stop()


def test_collect_with_cached_aggregates_matches_live(client) -> None:
    """``collect`` 传入缓存聚合与现算必须逐项一致（口径不能因缓存而变）。"""
    from fissue.web import status as st_mod

    app = client.app
    settings = app.state.settings
    repo = app.state.repo
    live = st_mod.collect(app, settings, repo)
    agg = st_mod.collect_aggregates(app, settings, repo)
    cached = st_mod.collect(app, settings, repo, aggregates=agg)

    for key in ("queues", "summary", "repos", "scans"):
        assert cached[key] == live[key], f"口径漂移：{key}"
    assert cached["llm"]["today"] == live["llm"]["today"]
    assert cached["database"]["items"] == live["database"]["items"]
    assert cached["database"]["repos"] == live["database"]["repos"]


def test_pages_are_served_from_background_cache(client) -> None:
    """页面请求不触发聚合的同步重算：背景数字全部命中后台刷新缓存。

    回归目标：远端数据库上「切换页面 = 十来个串行往返 = 几秒」。lifespan 已启动
    ``StatsCache``，首份快照就绪后，页面请求只应读缓存——刷新总次数不随页面
    请求增加。
    """
    cache = client.app.state.stats_cache
    assert cache is not None

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and cache.get() is None:
        time.sleep(0.02)
    assert cache.get() is not None, "首份快照应在后台就绪"

    # 用一次 bump 刷新把时间基线拉到现在，避免撞上周期刷新的边界
    cache.bump()
    pre = cache.build_count
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and cache.build_count == pre:
        time.sleep(0.02)
    baseline = cache.build_count

    assert client.get("/").status_code == 200
    assert client.get("/items").status_code == 200
    assert client.get("/queues").status_code == 200

    assert cache.build_count == baseline, "页面请求不应触发聚合的同步重算"
