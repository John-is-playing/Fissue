"""引导接口测试：动作白名单、读接口与「没有引导时」的行为。

页面与前端行为在 ``tests/test_web_guide.py``；这里只锁服务端契约。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from fissue.store.db import build_database
from fissue.store.deploy_repo import DeployRepository
from fissue.web.app import _auth_dependency, create_app
from fissue.web.guide_api import GUIDE_ACTIONS, register_guide_api


def _client(settings: Any) -> TestClient:
    app = create_app(settings)
    register_guide_api(app, settings.api.prefix, _auth_dependency())
    return TestClient(app)


@pytest.fixture
def client(settings):
    with _client(settings) as c:
        yield c


def _repo_of(settings: Any) -> DeployRepository:
    db = build_database(settings)
    db.create_all()
    return DeployRepository(db)


def test_missing_guide_returns_404(client):
    r = client.get("/api/v1/guides", params={"repo_slug": "acme/web"})
    assert r.status_code == 404
    assert "引导" in r.json()["detail"]


def test_unknown_action_rejected_with_whitelist(client):
    r = client.post(
        "/api/v1/guides/actions",
        json={"action": "rm-rf", "repo_slug": "acme/web", "host": "pi5"},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    # 错误信息里要列出白名单，否则前端/人只能去翻源码
    for action in GUIDE_ACTIONS:
        assert action in detail


def test_guide_roundtrip_through_repository(settings, client):
    """仓储写入后，读接口应原样返回阶段与步骤（页面按这个结构渲染）。"""
    repo = _repo_of(settings)
    repo.upsert_guide(
        repo_slug="acme/web",
        host="pi5",
        mode="half",
        stages=[
            {
                "id": "deploy",
                "title": "部署",
                "goal": "跑起来",
                "steps": [
                    {"id": "s1", "title": "装依赖", "why": "要依赖", "command": "pip install -r requirements.txt"}
                ],
            }
        ],
        notes="来自测试",
        source="ai",
    )
    r = client.get("/api/v1/guides", params={"repo_slug": "acme/web", "host": "pi5"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "ai"
    assert body["mode"] == "half"
    assert body["stages"][0]["steps"][0]["command"] == "pip install -r requirements.txt"
    # 生成时间之类的时间戳要能序列化（页面直接展示）
    assert body["updated_at"] is not None


def test_add_host_action_seals_credential(settings, client, monkeypatch):
    """add-host 动作必须把凭据加密后再落库，且响应不回显凭据。"""
    from fissue.deploy import secretbox

    sealed_inputs: list[str] = []

    class _Box:
        def seal(self, plaintext: str) -> str:
            sealed_inputs.append(plaintext)
            return "fissuebox:v1:STUB"

    monkeypatch.setattr(secretbox.CredentialBox, "from_settings", classmethod(lambda cls, st: _Box()))

    r = client.post(
        "/api/v1/guides/actions",
        json={
            "action": "add-host",
            "host": "pi5",
            "host_spec": {
                "name": "pi5",
                "address": "192.168.2.10",
                "user": "john",
                "credential": "-----BEGIN OPENSSH PRIVATE KEY-----秘密",
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "pi5"
    assert "凭据" not in r.text and "PRIVATE KEY" not in r.text
    row = _repo_of(settings).get_host("pi5")
    assert row is not None
    assert row.credential == "fissuebox:v1:STUB"
    assert sealed_inputs and "PRIVATE KEY" in sealed_inputs[0]


def test_check_action_without_deployment_returns_null(settings, client):
    repo = _repo_of(settings)
    repo.upsert_host(name="pi5", address="192.168.2.10", credential="fissuebox:v1:STUB")
    r = client.post(
        "/api/v1/guides/actions",
        json={"action": "check", "repo_slug": "acme/web", "host": "pi5"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deployment"] is None


def test_check_action_returns_steps(settings, client):
    repo = _repo_of(settings)
    host_id = repo.upsert_host(name="pi5", address="192.168.2.10", credential="fissuebox:v1:STUB")
    dep_id = repo.create_deployment(
        host_id=host_id,
        repo_slug="acme/web",
        ref="main",
        commit_sha=None,
        plan={"version": 1},
        app_root="/opt/fissue-deploy",
    )
    repo.start_step(
        deployment_id=dep_id, seq=1, step_id="install", kind="exec", command="pip install x"
    )
    r = client.post(
        "/api/v1/guides/actions",
        json={"action": "check", "repo_slug": "acme/web", "host": "pi5", "deployment_id": dep_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deployment"]["id"] == dep_id
    assert body["steps"][0]["step_id"] == "install"


def test_generate_requires_repo_slug(client):
    r = client.post("/api/v1/guides", json={"host": "pi5"})
    assert r.status_code == 422  # pydantic 校验：repo_slug 必填