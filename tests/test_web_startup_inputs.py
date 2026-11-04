"""前置项在 Web 上的呈现：/ops 列表标记 + 详情接口字段。

为什么单独测：`required_inputs` 是「部署前就该知道的事」，如果只存在于计划 JSON 里，
普通用户根本看不到——他们会一路点到 apply，然后在第 20 步探针那里收到 ConnectError。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from fissue.deploy.schema import (
    AppSpec,
    DeployPlan,
    DeployStep,
    ProbeSpec,
    RequiredInput,
    StepKind,
    UploadSpec,
)
from fissue.store.db import build_database
from fissue.store.deploy_repo import DeployRepository
from fissue.web.app import _auth_dependency, create_app
from fissue.web.deploy_api import register_deploy_api, register_deploy_pages


def _plan(*, env: dict[str, str]) -> DeployPlan:
    return DeployPlan(
        repo_slug="acme/web",
        app=AppSpec(name="web", env=env),
        steps=[
            DeployStep(
                id="upload-src", kind=StepKind.UPLOAD, upload=UploadSpec(src="src", dst="src")
            )
        ],
        health=[ProbeSpec(url="http://192.168.2.10:8000/")],
        required_inputs=[
            RequiredInput(key="FISSUE_LLM_API_KEY", reason="LLM 密钥", required=True),
            RequiredInput(key="FISSUE_GITHUB_TOKEN", reason="平台令牌", required=False),
        ],
    )


def _repo_of(settings: Any) -> DeployRepository:
    db = build_database(settings)
    db.create_all()
    return DeployRepository(db)


def _seed(settings: Any, plan: DeployPlan) -> int:
    repo = _repo_of(settings)
    host_id = repo.upsert_host(name="pi5", address="192.168.2.10", credential="fissuebox:v1:X")
    return int(
        repo.create_deployment(
            host_id=host_id,
            repo_slug=plan.repo_slug,
            ref="main",
            commit_sha=None,
            plan=plan.dump(),
            app_root="/opt/fissue-deploy",
        )
    )


@pytest.fixture
def client(settings):
    app = create_app(settings)
    register_deploy_api(app, settings.api.prefix, _auth_dependency())
    register_deploy_pages(app, settings)
    with TestClient(app) as c:
        yield c


def test_ops_page_flags_missing_required_inputs(settings, client):
    """缺必需前置项时，列表里要有可见标记——不能只在计划 JSON 里。"""
    _seed(settings, _plan(env={}))
    html = client.get("/ops").text
    assert "缺 1 项前置" in html


def test_ops_page_flags_placeholder_values(settings, client):
    """占位符比"缺值"更危险：apply 会被直接拒绝，页面必须提前说。"""
    _seed(settings, _plan(env={"FISSUE_LLM_API_KEY": "sk-REPLACE-ME"}))
    html = client.get("/ops").text
    assert "占位符待填" in html


def test_ops_page_has_no_flag_when_inputs_are_provided(settings, client):
    _seed(settings, _plan(env={"FISSUE_LLM_API_KEY": "sk-real-123456"}))
    html = client.get("/ops").text
    assert "缺 1 项前置" not in html
    assert "占位符待填" not in html


def test_deployment_detail_exposes_startup_inputs(settings, client):
    plan = _plan(env={})
    dep_id = _seed(settings, plan)
    data = client.get(f"/api/v1/deployments/{dep_id}").json()
    startup = data["startup_inputs"]
    assert startup["missing_required"] == ["FISSUE_LLM_API_KEY"]
    keys = {item["key"]: item for item in startup["items"]}
    assert keys["FISSUE_LLM_API_KEY"]["required"] is True
    assert keys["FISSUE_LLM_API_KEY"]["provided"] is False
    assert keys["FISSUE_GITHUB_TOKEN"]["required"] is False
    assert startup["blocking"] is False


def test_detail_marks_blocking_and_masks_placeholder(settings, client):
    """占位符只回显掩码：半填的密钥不该原样出现在接口响应里。"""
    dep_id = _seed(settings, _plan(env={"FISSUE_LLM_API_KEY": "sk-REPLACE-ME"}))
    startup = client.get(f"/api/v1/deployments/{dep_id}").json()["startup_inputs"]
    assert startup["blocking"] is True
    assert startup["placeholders"], "应当列出占位符"
    hint = startup["placeholders"][0]["hint"]
    assert "REPLACE" not in hint and "sk-REP" in hint


def test_old_plan_without_required_inputs_still_renders(settings, client):
    """历史记录（没有这个字段）不能让页面 500——契约只加字段，旧数据照样能读。"""
    repo = _repo_of(settings)
    host_id = repo.upsert_host(name="old", address="10.0.0.9", credential="fissuebox:v1:X")
    dep_id = repo.create_deployment(
        host_id=host_id,
        repo_slug="acme/old",
        ref="main",
        commit_sha=None,
        plan={"version": 1, "repo_slug": "acme/old", "app": {"name": "old"}, "steps": []},
        app_root="/opt/fissue-deploy",
    )
    assert client.get("/ops").status_code == 200
    data = client.get(f"/api/v1/deployments/{dep_id}").json()
    assert data["startup_inputs"]["items"] == []
    assert data["startup_missing"] == 0
