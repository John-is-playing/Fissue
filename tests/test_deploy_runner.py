"""执行器的集成测试：假 SSH 通道 + 假探针，验证四条关键路径。

不连真机、不连真模型。这里要证明的是执行器的**判定逻辑**，而不是 SSH 本身：
1. 计划非法时**一步都不执行**（连机器都不连）；
2. 全部成功 + 探针绿 → `success`，且步骤与审计流水都落了库；
3. 某步失败 + 有上一版 → 切回上一版并**重新探针**，状态 `rolled_back`；
4. 某步失败 + 没有上一版 → `failed`，现场保留。

第 3、4 条是这块最容易被写歪的地方：很多实现会「回滚了就当成功」，而这一步是否
真的恢复了，只有重新跑一遍健康判定才算数。
"""

from __future__ import annotations

import pytest

from fissue.config import Settings
from fissue.deploy.runner import DeployError, DeploymentRunner
from fissue.deploy.schema import (
    AppSpec,
    DeployPlan,
    DeployStatus,
    DeployStep,
    ExecSpec,
    ProbeSpec,
    ServiceSpec,
    StepKind,
    StepStatus,
)
from fissue.deploy.secretbox import CredentialBox
from fissue.store.db import build_database_from_config
from fissue.store.deploy_repo import DeployRepository


class FakeResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration = 0.01

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class FakeChannel:
    """按命令内容回话的假通道；记录所有命令，便于断言「该跑/不该跑」。"""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.commands: list[str] = []
        self.fail_on = fail_on
        self.host_fingerprint = "SHA256:fake"
        self.closed = False

    def run(self, command: str, *, timeout=None, cwd=None) -> FakeResult:
        full = f"{command}"
        self.commands.append(full)
        if "systemctl is-active" in full:
            return FakeResult(stdout="active\n")
        if "test -d" in full:
            # 回滚前会校验目标 release 是否还在；假通道一律回答「在」，
            # 专门验证「回滚点已删除」的用例自己覆盖这一条。
            return FakeResult(stdout="yes\n")
        if "grep -c" in full:
            return FakeResult(stdout="1\n")
        if "readlink -f" in full:
            return FakeResult(stdout="/opt/x/current\n")
        if self.fail_on and self.fail_on in full:
            return FakeResult(exit_code=1, stdout="", stderr="boom")
        return FakeResult(stdout="ok\n")

    def put_text(self, text: str, dst: str, mode: str = "0644") -> None:
        self.commands.append(f"put_text {dst}")

    def put_file(self, local, dst: str, mode: str = "0644") -> None:
        self.commands.append(f"put_file {dst}")

    def read_text(self, path: str, *, max_bytes: int = 65536) -> str:
        return ""

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@pytest.fixture()
def env(tmp_path):
    """一套临时配置 + 库 + 已登记主机。"""
    settings = Settings()
    settings.app.data_dir = str(tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    settings.database.url = f"sqlite:///{(tmp_path / 'deploy.db').as_posix()}"
    settings.deploy.enabled = True
    # 健康判定会按 health_timeout 轮询（真实场景要等应用 bind 端口）。测试里把它压到
    # 很小，否则「故意不健康」那两条用例每条都要实打实等 60 秒。
    settings.deploy.health_timeout = 0.6

    db = build_database_from_config(settings.database)
    db.create_all()
    repo = DeployRepository(db)
    box = CredentialBox.from_settings(settings)
    repo.upsert_host(
        name="pi",
        address="192.168.1.3",
        credential=box.seal("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n"),
        user="john",
        app_root="/opt/fissue-deploy",
    )
    return settings, repo, box


def simple_plan(*, dangerous: bool = False) -> DeployPlan:
    command = "rm -rf /" if dangerous else "python -m pip install -r requirements.txt"
    return DeployPlan(
        repo_slug="acme/web",
        ref="main",
        app=AppSpec(name="acme-web", runtime="python", service_name="acme-web", port=8000),
        steps=[
            DeployStep(id="install", kind=StepKind.EXEC, exec=ExecSpec(command=command)),
            DeployStep(
                id="restart",
                kind=StepKind.SERVICE,
                service=ServiceSpec(action="restart", name="acme-web"),
            ),
        ],
        health=[ProbeSpec(url="http://192.168.1.3:8000/healthz")],
    )


def install_runner(monkeypatch, channel: FakeChannel, *, probe_ok: bool = True):
    """把 connect / probe 换成假的；返回 connect 是否被调用过的标记。"""
    calls: dict[str, int] = {"connect": 0}

    def fake_connect(target, *, accept_new_host_key=True):
        calls["connect"] += 1
        return channel

    async def fake_probe(spec):
        # 回滚把 current 指回上一版之后，服务应当恢复——否则「回滚成功」本身就是假的。
        rolled_back = any("20260101T000000Z" in c for c in channel.commands)
        ok = probe_ok or rolled_back
        return {
            "target": spec.url or f"tcp:{spec.port}",
            "ok": ok,
            "detail": "伪造探针" if ok else "连接被拒绝",
            "elapsed": 0.0,
        }

    monkeypatch.setattr("fissue.deploy.runner.connect", fake_connect)
    monkeypatch.setattr("fissue.deploy.runner.probe", fake_probe)
    return calls


@pytest.mark.asyncio
async def test_dangerous_plan_never_connects(env, monkeypatch):
    """计划里有 `rm -rf /`：连接都不该建立（校验在连机器之前）。"""
    settings, repo, _box = env
    channel = FakeChannel()
    calls = install_runner(monkeypatch, channel)
    runner = DeploymentRunner(settings, repo)

    with pytest.raises(DeployError) as exc:
        await runner.run(host_name="pi", plan=simple_plan(dangerous=True), source_dir=settings.data_dir)

    assert "计划未通过校验" in str(exc.value)
    assert calls["connect"] == 0, "计划非法时不允许建立连接"
    assert channel.commands == []


@pytest.mark.asyncio
async def test_success_path_records_steps_and_events(env, monkeypatch):
    settings, repo, _box = env
    channel = FakeChannel()
    install_runner(monkeypatch, channel, probe_ok=True)
    runner = DeploymentRunner(settings, repo)

    report = await runner.run(host_name="pi", plan=simple_plan(), source_dir=settings.data_dir)

    assert report.status is DeployStatus.SUCCESS
    assert [s.status for s in report.steps] == [StepStatus.OK, StepStatus.OK]
    assert report.health["ok"] is True

    steps = repo.steps_of(report.deployment_id)
    assert [s.step_id for s in steps] == ["install", "restart"]
    assert all(s.status == StepStatus.OK.value for s in steps)

    events = [e.message for e in repo.events_of(report.deployment_id)]
    assert any("部署成功" in m for m in events)
    assert any("current ->" in m for m in events)
    # 证据齐全：systemctl 与端口监听都被查过
    assert any("systemctl is-active" in c for c in channel.commands)
    assert any("ss -ltnH" in c for c in channel.commands)
    assert channel.closed is True


@pytest.mark.asyncio
async def test_step_failure_without_previous_marks_failed(env, monkeypatch):
    settings, repo, _box = env
    channel = FakeChannel(fail_on="pip install")
    install_runner(monkeypatch, channel, probe_ok=True)
    runner = DeploymentRunner(settings, repo)

    report = await runner.run(host_name="pi", plan=simple_plan(), source_dir=settings.data_dir)

    assert report.status is DeployStatus.FAILED
    assert report.rolled_back is False
    assert "install" in (report.error or "")
    # 失败后不再执行后续步骤（半成品服务比没部署更糟）
    assert not any("systemctl restart" in c for c in channel.commands)
    events = [e.message for e in repo.events_of(report.deployment_id)]
    assert any("无可用回滚点" in m for m in events)


@pytest.mark.asyncio
async def test_step_failure_rolls_back_to_previous_release(env, monkeypatch):
    settings, repo, _box = env
    # 先造一条「上一次成功部署」，它才有回滚点
    host = repo.get_host("pi")
    previous_id = repo.create_deployment(
        host_id=int(host.id),
        repo_slug="acme/web",
        ref="main",
        commit_sha="deadbeef",
        plan=simple_plan().dump(),
        app_root="/opt/fissue-deploy",
    )
    repo.set_release(previous_id, release="20260101T000000Z", previous=None)
    repo.set_status(previous_id, DeployStatus.SUCCESS)

    channel = FakeChannel(fail_on="pip install")
    install_runner(monkeypatch, channel, probe_ok=True)
    runner = DeploymentRunner(settings, repo)

    report = await runner.run(host_name="pi", plan=simple_plan(), source_dir=settings.data_dir)

    assert report.status is DeployStatus.ROLLED_BACK
    assert report.rolled_back is True
    assert report.previous_release == "20260101T000000Z"
    # 回滚就是把 current 指回上一版 + 重启服务
    assert any("20260101T000000Z" in c and "ln -sfn" in c for c in channel.commands)
    assert any("systemctl restart" in c for c in channel.commands)


@pytest.mark.asyncio
async def test_unhealthy_deploy_rolls_back(env, monkeypatch):
    """步骤全绿但探针不通：依然算失败——退出码 0 不是「服务起来了」的证据。"""
    settings, repo, _box = env
    host = repo.get_host("pi")
    previous_id = repo.create_deployment(
        host_id=int(host.id),
        repo_slug="acme/web",
        ref="main",
        commit_sha="deadbeef",
        plan=simple_plan().dump(),
        app_root="/opt/fissue-deploy",
    )
    repo.set_release(previous_id, release="20260101T000000Z", previous=None)
    repo.set_status(previous_id, DeployStatus.SUCCESS)

    channel = FakeChannel()
    install_runner(monkeypatch, channel, probe_ok=False)
    runner = DeploymentRunner(settings, repo)

    report = await runner.run(host_name="pi", plan=simple_plan(), source_dir=settings.data_dir)

    assert report.status is DeployStatus.ROLLED_BACK
    # report.health 记录的是**回滚之后**重新探针的结果：只有它绿了才敢说「服务恢复了」
    assert report.health["ok"] is True
    events = [e.message for e in repo.events_of(report.deployment_id)]
    assert any("健康检查未通过" in m for m in events)
    assert any("回滚完成" in m for m in events)


@pytest.mark.asyncio
async def test_disabled_swallows_nothing(env, monkeypatch):
    """能力关闭时明确报错，而不是悄悄什么都不做。"""
    settings, repo, _box = env
    settings.deploy.enabled = False
    channel = FakeChannel()
    calls = install_runner(monkeypatch, channel)
    runner = DeploymentRunner(settings, repo)

    with pytest.raises(DeployError) as exc:
        await runner.run(host_name="pi", plan=simple_plan(), source_dir=settings.data_dir)
    assert "未开启" in str(exc.value)
    assert calls["connect"] == 0

@pytest.mark.asyncio
async def test_destroy_removes_unit_and_disables(env, monkeypatch):
    """下线必须把 unit 收掉，不能只停服务。

    只停服务时 `enabled` 还在，机器重启后 systemd 会去启动一个指向**已删除目录**的
    服务。真机验证时实测到了这一点：服务「下线」了，`systemctl is-enabled` 仍是 enabled。
    """
    settings, repo, _box = env
    host = repo.get_host("pi")
    deployment_id = repo.create_deployment(
        host_id=int(host.id),
        repo_slug="acme/web",
        ref="main",
        commit_sha="deadbeef",
        plan=simple_plan().dump(),
        app_root="/opt/fissue-deploy",
    )
    repo.set_release(deployment_id, release="20260101T000000Z", previous=None)
    # destroy 的服务分支靠 deployment.service_name 判断，这里必须写上（真实部署由
    # runner 在开头 set_service_info 写入）。
    repo.set_service_info(deployment_id, service_name="school-bbs", url=None)
    repo.set_status(deployment_id, DeployStatus.SUCCESS)

    channel = FakeChannel()
    install_runner(monkeypatch, channel)
    runner = DeploymentRunner(settings, repo)

    report = await runner.destroy(deployment_id)

    assert report.status is DeployStatus.DESTROYED
    joined = "\n".join(channel.commands)
    assert "systemctl stop school-bbs" in joined
    assert "systemctl disable school-bbs" in joined
    assert "rm -f /etc/systemd/system/school-bbs.service" in joined
    assert "systemctl daemon-reload" in joined
    # shared 目录不能被删掉（里面有数据库与 .env）
    assert "shared" not in joined.split("rm -rf")[-1].split("&&")[0]
