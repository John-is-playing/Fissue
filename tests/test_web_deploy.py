"""部署运维 Web 层测试（TestClient + 临时 SQLite，**不连真机、不连真模型**）。

这里刻意**自己注册路由**（``register_deploy_api`` / ``register_deploy_pages``）：
本文件只验证 ``web/deploy_api.py`` 与 ``web/templates/ops.html`` 的行为，
注册点（``routes_api.py`` / ``routes_pages.py``）由应用侧负责——测里再去依赖那两处
会让「路由没挂上」和「实现有 bug」变成同一个红灯。

凭据一律用桩：``CredentialBox.from_settings`` 被换成假盒子，因此测试既不读
``data/server.secret``、也不会在断言里碰到任何真凭据。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fissue.deploy.runner import DeployError
from fissue.deploy.service import DeployService
from fissue.store.db import build_database
from fissue.store.deploy_repo import DeployRepository
from fissue.web.app import _auth_dependency, create_app
from fissue.web.deploy_api import register_deploy_api, register_deploy_pages

PLAIN = "-----BEGIN OPENSSH PRIVATE KEY-----超级机密-----END OPENSSH PRIVATE KEY-----"
SEALED = "fissuebox:v1:TEST-STUB-CIPHERTEXT"


class _StubBox:
    """假的凭据盒子：加密结果可预测，解密不做任何校验。

    用真 ``CredentialBox`` 也能跑，但那要读/建 ``data/server.secret``——
    单测不该在用户目录里留东西，也不该让「密钥没生成」这种环境问题变成红灯。
    """

    def __init__(self) -> None:
        self.sealed_inputs: list[str] = []

    def seal(self, plaintext: str) -> str:
        self.sealed_inputs.append(plaintext)
        return SEALED

    def open(self, sealed: str) -> str:  # pragma: no cover - 测试不在 Web 层解密
        return PLAIN


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> _StubBox:
    """把 ``CredentialBox.from_settings`` 换成假盒子（唯一接触明文的入口）。"""
    from fissue.deploy import secretbox

    stub = _StubBox()
    monkeypatch.setattr(secretbox.CredentialBox, "from_settings", classmethod(lambda cls, settings: stub))
    return stub


def _client(settings: Any, *, enabled: bool) -> TestClient:
    """构造一个挂了部署路由的 TestClient。"""
    settings.deploy.enabled = enabled
    app = create_app(settings)
    register_deploy_api(app, settings.api.prefix, _auth_dependency())
    register_deploy_pages(app, settings)
    return TestClient(app)


@pytest.fixture
def deploy_client(settings, box):
    """部署能力**已开启**的客户端。"""
    with _client(settings, enabled=True) as c:
        yield c


@pytest.fixture
def disabled_client(settings, box):
    """部署能力**关闭**（默认值）的客户端。"""
    with _client(settings, enabled=False) as c:
        yield c


def _deploy_repo_of(settings: Any) -> DeployRepository:
    """打开同一个库的部署仓储（断言「库里存的是什么」用）。"""
    db = build_database(settings)
    db.create_all()
    return DeployRepository(db)


def _add_host(client: TestClient, name: str = "pi5") -> dict[str, Any]:
    r = client.post(
        "/api/v1/hosts",
        json={
            "name": name,
            "address": "192.168.1.3",
            "port": 22,
            "user": "john",
            "auth_method": "key",
            "credential": PLAIN,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. 能力关闭：读照常，写一律 409
# ---------------------------------------------------------------------------


def test_disabled_reads_still_work_but_writes_are_409(disabled_client) -> None:
    """``deploy.enabled=false`` 时历史要看得到，但一次也不许改远端机器。"""
    r = disabled_client.get("/api/v1/hosts")
    assert r.status_code == 200, r.text
    assert r.json() == []

    r = disabled_client.post(
        "/api/v1/hosts",
        json={"name": "pi5", "address": "192.168.1.3", "credential": PLAIN},
    )
    assert r.status_code == 409, r.text
    assert "部署能力未开启" in r.json()["detail"]

    # 其余写端点同样 409（不是 500、也不是悄悄成功）
    assert disabled_client.post("/api/v1/hosts/pi5/ping").status_code == 409
    assert disabled_client.delete("/api/v1/hosts/pi5").status_code == 409
    assert (
        disabled_client.post(
            "/api/v1/deployments", json={"repo_slug": "psf/requests", "host": "pi5"}
        ).status_code
        == 409
    )

    # 部署记录是只读的，能力关着也照常返回
    assert disabled_client.get("/api/v1/deployments").status_code == 200


# ---------------------------------------------------------------------------
# 2. 新增主机：库里是密文，响应里没有明文
# ---------------------------------------------------------------------------


def test_create_host_seals_credential_and_never_echoes_it(deploy_client, settings, box) -> None:
    body = _add_host(deploy_client)

    # 响应只有 id/name，**没有** credential（明文与密文都不回显）
    assert set(body) == {"id", "name"}, body
    assert body["name"] == "pi5"

    # 桩盒子确实拿到了明文（说明凭据走到了加密这一步，而不是被静默丢弃）
    assert box.sealed_inputs == [PLAIN]

    # 库里存的是密文：不是明文、且前缀表明它是 secretbox 的产物
    row = _deploy_repo_of(settings).get_host("pi5")
    assert row is not None
    assert row.credential == SEALED
    assert PLAIN not in row.credential


def test_list_hosts_never_leaks_credential_field(deploy_client, settings) -> None:
    _add_host(deploy_client)
    r = deploy_client.get("/api/v1/hosts")
    assert r.status_code == 200, r.text
    hosts = r.json()
    assert len(hosts) == 1
    host = hosts[0]

    # 字段清单按契约来——多一个 credential 就等于把凭据面开到浏览器上
    assert "credential" not in host
    assert set(host) == {
        "name",
        "address",
        "user",
        "port",
        "auth_method",
        "fingerprint",
        "enabled",
        "app_root",
        "last_seen_at",
    }
    # 明文与密文都不该出现在响应正文里（密文能离线爆破，等于泄漏）
    assert PLAIN not in r.text
    assert SEALED not in r.text
    assert host["fingerprint"] is None  # 还没连过，TOFU 未建立
    assert host["address"] == "192.168.1.3"


# ---------------------------------------------------------------------------
# 3. 触发部署：立刻返回 accepted，不在请求里跑完
# ---------------------------------------------------------------------------


def test_create_deployment_returns_accepted_without_waiting(
    deploy_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次部署可能几分钟：端点必须**受理完就返回**，把执行丢给后台任务。"""
    seen: dict[str, Any] = {}
    finished = {"done": False}

    async def fake_apply(self, **kwargs):  # noqa: ANN001, ANN003 - 与真实签名对齐
        seen.update(kwargs)
        await asyncio.sleep(1.0)  # 模拟「跑得很久」——但请求不该等它
        finished["done"] = True

    monkeypatch.setattr(DeployService, "apply", fake_apply)

    started = time.monotonic()
    r = deploy_client.post(
        "/api/v1/deployments",
        json={"repo_slug": "psf/requests", "host": "pi5", "ref": "main"},
    )
    elapsed = time.monotonic() - started

    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": True, "host": "pi5", "repo_slug": "psf/requests"}
    # 请求返回时后台**还没**跑完：这一条不依赖计时，比断 elapsed 更硬。
    assert finished["done"] is False
    # 若这里同步等 apply 跑完，elapsed 至少 1.0s
    assert elapsed < 0.8, f"触发部署阻塞了请求：{elapsed:.2f}s"

    # 后台任务真的跑起来了（不是「返回 accepted 然后什么都没做」）
    time.sleep(1.2)
    assert seen.get("slug") == "psf/requests"
    assert seen.get("host") == "pi5"
    assert seen.get("ref") == "main"


# ---------------------------------------------------------------------------
# 4. 回滚 / 下线：部署链路的错误翻成 400 + 人话
# ---------------------------------------------------------------------------


def test_rollback_error_becomes_400_with_readable_detail(
    deploy_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_rollback(self, deployment_id):  # noqa: ANN001
        raise DeployError("这次部署没有上一份 release 可以回滚（previous_release 为空）")

    monkeypatch.setattr(DeployService, "rollback", fake_rollback)

    r = deploy_client.post("/api/v1/deployments/7/rollback")
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "previous_release" in detail
    # 不许把 traceback / 异常类名漏给客户端
    assert "Traceback" not in detail and "DeployError" not in detail


def test_destroy_error_becomes_400_with_readable_detail(
    deploy_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_destroy(self, deployment_id):  # noqa: ANN001
        raise DeployError(f"没有这次部署：{deployment_id}")

    monkeypatch.setattr(DeployService, "destroy", fake_destroy)

    r = deploy_client.delete("/api/v1/deployments/9")
    assert r.status_code == 400, r.text
    assert "没有这次部署" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 5. 页面
# ---------------------------------------------------------------------------


def test_ops_page_renders_hosts_and_deployments_sections(deploy_client) -> None:
    r = deploy_client.get("/ops")
    assert r.status_code == 200, r.text
    html = r.text
    assert "主机" in html
    assert "部署记录" in html
    # 四个操作区块
    for word in ("新增主机", "回滚", "下线"):
        assert word in html, word
    # 页面上不该出现任何凭据内容（模板不内联凭据，也不回显密文）
    assert PLAIN not in html
    assert "fissuebox:" not in html


def test_ops_page_shows_registered_host_name(deploy_client) -> None:
    _add_host(deploy_client, name="rpi-home")
    r = deploy_client.get("/ops")
    assert r.status_code == 200, r.text
    assert "rpi-home" in r.text


def test_ops_page_renders_a_deployment_row(deploy_client, settings) -> None:
    """部署表那一整段模板必须真的被执行一次（状态标签、发布目录、时间格式化）。

    没有这一条时，页面测试只覆盖了「空表」分支：模板语法能被 Jinja 编出来，
    但 ``d.created_at[:19]`` / 状态判断这些行一行都没跑过。
    """
    _add_host(deploy_client, name="pi5")
    repo = _deploy_repo_of(settings)
    host_row = repo.get_host("pi5")
    assert host_row is not None
    repo.create_deployment(
        host_id=int(host_row.id),
        repo_slug="psf/requests",
        ref="main",
        commit_sha="abc1234",
        plan={"version": 1},
        app_root="/opt/fissue-deploy",
    )

    r = deploy_client.get("/ops")
    assert r.status_code == 200, r.text
    assert "psf/requests" in r.text
    # 新建的部署是 planning 状态，页面按状态分支渲染
    assert "planning" in r.text


def test_ops_page_is_behind_the_page_guard() -> None:
    """/ops 必须和其他页面一样受身份守卫保护。

    运维页上有主机名、部署历史和「下线」按钮，是**能改远端机器**的入口。页面内部
    那道自查只是第二层；真正的口径在 ``web/auth.py::PAGE_PREFIXES``——漏掉它时，
    未登录的人能直接打开这一页。这条断言存在的意义就是：下次有人整理前缀清单，
    不会顺手把 ``/ops`` 删掉而没有任何测试变红。
    """
    from fissue.web.auth import _needs_identity

    assert _needs_identity("/ops") is True
    assert _needs_identity("/ops/") is True

