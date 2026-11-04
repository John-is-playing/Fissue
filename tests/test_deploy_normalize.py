"""计划归一化的测试：只做「补齐」与「换名」，不替模型编计划。

这些用例都来自真机验证里真实发生的失败，不是设想的边界：
模型自己 `sudo install` 了系统 unit，却把 `service` 步骤的 `sudo` 留空；
探针写成主机**别名**（`rpi`），而控制端只认地址。
"""

from __future__ import annotations

from fissue.deploy.normalize import normalize_plan, systemd_target
from fissue.deploy.schema import (
    AppSpec,
    DeployPlan,
    DeployStep,
    ExecSpec,
    ProbeSpec,
    ServiceSpec,
    StepKind,
    WriteFileSpec,
)


def _plan(*, install_unit: bool = True, probe_url: str | None = None, service_sudo: bool = False) -> DeployPlan:
    steps = [
        DeployStep(
            id="install-unit",
            kind=StepKind.EXEC,
            exec=ExecSpec(
                command=(
                    "sudo -n install -m 644 deploy/app.service /etc/systemd/system/app.service"
                    if install_unit
                    else "cp deploy/app.service ~/app.service"
                )
            ),
        ),
        DeployStep(
            id="restart",
            kind=StepKind.SERVICE,
            service=ServiceSpec(action="restart", name="app", sudo=service_sudo),
        ),
    ]
    if probe_url:
        steps.append(
            DeployStep(id="probe-http", kind=StepKind.PROBE, probe=ProbeSpec(url=probe_url))
        )
    return DeployPlan(
        repo_slug="acme/web",
        app=AppSpec(name="app", service_name="app", service_sudo=service_sudo),
        steps=steps,
        health=[ProbeSpec(url=probe_url or "http://192.168.2.10:5000/")],
    )


def test_system_unit_install_implies_sudo_for_service_steps():
    """装系统 unit 的计划，service 步骤必须自动补提权（否则 daemon-reload 必失败）。"""
    plan = normalize_plan(_plan(), host_name="rpi", host_address="192.168.2.10")
    assert plan.app.service_sudo is True
    assert plan.steps[1].service.sudo is True
    assert "归一化" in plan.notes
    assert "service.sudo=true" in plan.notes


def test_idempotent_when_already_correct():
    """已经正确的计划再跑一次不该产生任何改动（否则 notes 会被刷屏）。"""
    plan = normalize_plan(
        _plan(service_sudo=True), host_name="rpi", host_address="192.168.2.10"
    )
    first_notes = plan.notes
    normalize_plan(plan, host_name="rpi", host_address="192.168.2.10")
    assert plan.notes == first_notes


def test_user_unit_does_not_get_sudo():
    """用户级 unit 不需要提权，归一化不能顺手把 sudo 加进去。"""
    plan = _plan(install_unit=False)
    plan.steps[0].exec.command = "cp deploy/app.service ~/.config/systemd/user/app.service"
    normalize_plan(plan, host_name="rpi", host_address="192.168.2.10")
    assert plan.app.service_sudo is False
    assert plan.steps[1].service.sudo is False


def test_alias_rewritten_in_health_and_inline_probe_step():
    """别名 → 真实地址：health 与内联 probe 步骤都要改。"""
    plan = normalize_plan(
        _plan(probe_url="http://rpi:5000/"), host_name="rpi", host_address="192.168.2.10"
    )
    assert plan.health[0].url == "http://192.168.2.10:5000/"
    probe_step = next(s for s in plan.steps if s.id == "probe-http")
    assert probe_step.probe.url == "http://192.168.2.10:5000/"


def test_other_hosts_are_left_alone():
    """不是别名的地址一律不动——那可能是故意指向反代或 CDN。"""
    plan = normalize_plan(
        _plan(probe_url="https://example.com/healthz"),
        host_name="rpi",
        host_address="192.168.2.10",
    )
    assert plan.health[0].url == "https://example.com/healthz"


def test_systemd_target_detection():
    assert systemd_target("sudo -n install -m 644 x /etc/systemd/system/x.service")
    assert not systemd_target("cp x ~/.config/systemd/user/x.service")
    assert not systemd_target("systemctl --user restart app")
    assert not systemd_target("")

def test_unit_gets_user_line_when_missing():
    """unit 缺 `User=` 会以 root 运行：既造成 root 属主的残留，也是安全事故。

    真机验证时的实际后果：服务以 root 跑过一次，`__pycache__` 归 root，之后
    `destroy` 的 `rm -rf` 在普通用户下直接报权限错误。
    """
    plan = _plan()
    plan.steps.insert(
        1,
        DeployStep(
            id="write-unit",
            kind=StepKind.WRITE_FILE,
            write=WriteFileSpec(
                path="deploy/app.service",
                content="[Unit]\nDescription=x\n\n[Service]\nExecStart=/bin/true\n",
            ),
        ),
    )
    normalize_plan(plan, host_name="rpi", host_address="192.168.2.10", host_user="john")
    unit = next(s for s in plan.steps if s.id == "write-unit")
    assert "User=john" in unit.write.content
    assert "运行身份改为 john" in plan.notes


def test_unit_user_not_duplicated():
    """已经写了 User= 的 unit 不能被重复注入。"""
    plan = _plan()
    plan.steps.insert(
        1,
        DeployStep(
            id="write-unit",
            kind=StepKind.WRITE_FILE,
            write=WriteFileSpec(
                path="deploy/app.service",
                content="[Service]\nUser=deploy\nExecStart=/bin/true\n",
            ),
        ),
    )
    normalize_plan(plan, host_name="rpi", host_address="192.168.2.10", host_user="john")
    unit = next(s for s in plan.steps if s.id == "write-unit")
    assert unit.write.content.count("User=") == 1
