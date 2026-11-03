"""新手引导页（``/ops/guide``）与引导接口的页面级测试。

全部离线：不克隆仓库、不调模型、不连 SSH。
- 页面：直接渲染兜底模板（未生成过引导时的路径）；
- 接口：读/写动作都走真实路由，但数据来自临时 SQLite；
- 生成与部署的「后台动作」不在这里触发（它们要网络/模型），只验证页面与固定动作的契约。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fissue.store.deploy_repo import DeployRepository
from fissue.web.app import create_app

REPO = "acme/web"
HOST = "pi5"


def _client(settings: Any) -> TestClient:
    """真实装配的客户端：引导路由由 ``create_app`` 经 routes_api/routes_pages 挂上。"""
    settings.deploy.enabled = True
    return TestClient(create_app(settings))


@pytest.fixture
def guide_client(settings):
    with _client(settings) as c:
        yield c


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def test_guide_page_renders_three_modes_and_a_step(guide_client) -> None:
    """页面三档都要出现，且至少渲染出一个步骤标题（兜底模板的第三阶段）。"""
    r = guide_client.get("/ops/guide", params={"repo_slug": REPO})
    assert r.status_code == 200, r.text
    html = r.text
    for word in ("只读", "半自动", "一键"):
        assert word in html, word
    # 兜底第三阶段的第一步：命令与「为什么」都要在页面上。
    assert "先只要计划，不执行" in html
    assert "fissue deploy plan" in html


def test_guide_page_falls_back_when_never_generated(guide_client) -> None:
    """没生成过引导时用兜底模板渲染，并明确告诉用户这不是按仓库生成的。"""
    r = guide_client.get("/ops/guide", params={"repo_slug": REPO})
    assert r.status_code == 200, r.text
    html = r.text
    assert "通用" in html
    assert "尚未按本仓库" in html


def test_guide_page_never_mentions_credential_field(guide_client) -> None:
    """页面正文不得出现凭据字段名（更不得回显值）。

    ``credential`` 是请求体字段名，只应存在于 guide.js；模板一旦把它写进
    data-* 或表单 name，就等于把「凭据在页面里」这件事变成常态。
    """
    r = guide_client.get("/ops/guide", params={"repo_slug": REPO})
    assert r.status_code == 200, r.text
    assert "credential" not in r.text.lower()


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


def test_get_guide_without_a_record_is_404(guide_client, settings) -> None:
    r = guide_client.get(f"{settings.api.prefix}/guides", params={"repo_slug": REPO})
    assert r.status_code == 404, r.text


def test_unknown_action_is_rejected_with_allowed_list(guide_client, settings) -> None:
    """动作名不在 GUIDE_ACTIONS 里必须 400，并告诉调用方支持哪些动作。

    引导页能点的动作是写死的白名单；模型只声明 action key，不能被拿来当 URL。
    """
    r = guide_client.post(
        f"{settings.api.prefix}/guides/actions",
        json={"action": "rm-rf", "repo_slug": REPO, "host": HOST},
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "rm-rf" in detail
    # 后端当前措辞是「只支持：...」（不是「白名单」字样），断言允许动作被列出。
    assert "只支持" in detail
    for action in ("add-host", "generate-plan", "apply", "check", "rollback"):
        assert action in detail, action


def test_check_action_returns_deployment_and_steps(
    guide_client, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check 的响应必须同时带 ``deployment.status`` 与 ``steps``。

    页面靠这两个字段决定「成功/失败」和「失败在哪一步」——只给一个状态
    没有步骤，降级提示就写不出失败原因。这里把仓储查询打桩，不落库。
    """
    deployment = SimpleNamespace(
        id=17,
        status="success",
        release="20261102-120000",
        url="https://example.invalid/",
        health_json={"systemd": "active"},
        error=None,
    )
    step = SimpleNamespace(
        seq=1,
        step_id="install-deps",
        status="ok",
        exit_code=0,
        error=None,
    )
    monkeypatch.setattr(DeployRepository, "get_deployment", lambda self, deployment_id: deployment)
    monkeypatch.setattr(DeployRepository, "steps_of", lambda self, deployment_id: [step])

    r = guide_client.post(
        f"{settings.api.prefix}/guides/actions",
        json={
            "action": "check",
            "repo_slug": REPO,
            "host": HOST,
            "deployment_id": 17,
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["action"] == "check"
    assert payload["deployment"]["status"] == "success"
    assert payload["steps"], payload
    assert payload["steps"][0]["step_id"] == "install-deps"