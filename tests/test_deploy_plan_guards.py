"""归一化的两条新规则 + 启动前置项校验。

全部来自 2026-11-04 的真机部署（让 AI 部署 Fissue 自己到树莓派）：

* AI 只上传了 ``pyproject.toml`` / ``src`` / ``config.example.yaml``，而 pyproject
  里有 ``readme = "README.md"`` —— hatchling 生成元数据时 ``OSError``，
  ``install-app`` 那一步才失败，报错埋在 pip 构建日志里。
* AI 在环境模板里写了 ``FISSUE_LLM_API_KEY=sk-REPLACE-ME``。``fissue serve``
  启动即 ``ConfigError`` 退出，而失败点报在第 20 步的探针上（``ConnectError``），
  离真正的原因隔了 20 步。
* 服务监听 ``127.0.0.1`` 时，**控制端**的探针永远连不上（这次 AI 偶然写对了）。

这些都不是"设想的边界"，所以规则写成确定性的补正 + 执行前检查，而不是再指望提示词。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fissue.deploy.normalize import normalize_plan
from fissue.deploy.runner import DeployError
from fissue.deploy.schema import (
    AppSpec,
    DeployPlan,
    DeployStep,
    ProbeSpec,
    RequiredInput,
    StepKind,
    UploadSpec,
)
from fissue.deploy.service import _check_startup_inputs

# ---------------------------------------------------------------------------
# 打包元数据：pyproject 引用的文件必须一起上传
# ---------------------------------------------------------------------------


def _project(root: Path, *, with_readme: bool = True) -> None:
    """造一个最小的 Python 项目：pyproject 引用 readme / license / 动态版本文件。"""
    (root / "pyproject.toml").write_text(
        """
[project]
name = "demo"
readme = "README.md"
license = { file = "LICENSE" }

[tool.hatch.version]
path = "src/demo/__init__.py"
""",
        encoding="utf-8",
    )
    if with_readme:
        (root / "README.md").write_text("# demo", encoding="utf-8")
    (root / "LICENSE").write_text("MIT", encoding="utf-8")
    (root / "src/demo").mkdir(parents=True, exist_ok=True)
    (root / "src/demo/__init__.py").write_text('__version__ = "0.1"', encoding="utf-8")


def _upload_plan() -> DeployPlan:
    return DeployPlan(
        repo_slug="acme/demo",
        app=AppSpec(name="demo"),
        steps=[
            DeployStep(
                id="upload-pyproject",
                kind=StepKind.UPLOAD,
                upload=UploadSpec(src="pyproject.toml", dst="pyproject.toml"),
            ),
            DeployStep(
                id="probe",
                kind=StepKind.PROBE,
                probe=ProbeSpec(url="http://192.168.2.10:8000/"),
            ),
        ],
        health=[ProbeSpec(url="http://192.168.2.10:8000/")],
    )


def test_packaging_referenced_files_are_added(tmp_path: Path) -> None:
    _project(tmp_path)
    plan = _upload_plan()
    normalize_plan(plan, source_root=tmp_path)

    uploads = [step.upload for step in plan.steps if step.kind == StepKind.UPLOAD]
    dests = {item.dst for item in uploads if item}
    assert {"README.md", "LICENSE", "src/demo/__init__.py"} <= dests
    # 补出来的步骤要排在原上传步骤之后、执行步骤之前
    assert [step.id for step in plan.steps][:2] == ["upload-pyproject", "upload-readme-md"]


def test_packaging_rules_are_idempotent(tmp_path: Path) -> None:
    _project(tmp_path)
    plan = _upload_plan()
    normalize_plan(plan, source_root=tmp_path)
    count = len(plan.steps)
    normalize_plan(plan, source_root=tmp_path)
    assert len(plan.steps) == count, "第二次归一化不该重复补上传"


def test_missing_referenced_file_is_not_invented(tmp_path: Path) -> None:
    """pyproject 引用但源码里不存在的文件：补一个上传步骤只会在执行时失败。"""
    _project(tmp_path, with_readme=False)
    plan = _upload_plan()
    normalize_plan(plan, source_root=tmp_path)

    dests = {step.upload.dst for step in plan.steps if step.upload}
    assert "README.md" not in dests
    assert "LICENSE" in dests


def test_without_source_root_nothing_is_added() -> None:
    """没有源码目录就不猜——这是 `--plan-file` 场景（不克隆仓库）。"""
    plan = _upload_plan()
    before = len(plan.steps)
    normalize_plan(plan)
    assert len(plan.steps) == before


# ---------------------------------------------------------------------------
# 监听地址：不能只听回环，否则控制端探针拿不到证据
# ---------------------------------------------------------------------------


def _bind_plan(env: dict[str, str], probe: str) -> DeployPlan:
    return DeployPlan(
        repo_slug="acme/demo",
        app=AppSpec(name="demo", env=env),
        steps=[],
        health=[ProbeSpec(url=probe)],
    )


def test_loopback_bind_rewritten_when_probe_is_remote() -> None:
    plan = _bind_plan({"FISSUE_WEB_HOST": "127.0.0.1"}, "http://192.168.2.10:8000/")
    normalize_plan(plan)
    assert plan.app.env["FISSUE_WEB_HOST"] == "0.0.0.0"
    assert "0.0.0.0" in plan.notes


@pytest.mark.parametrize("value", ["127.0.0.1", "localhost", "::1"])
def test_all_loopback_spellings_are_rewritten(value: str) -> None:
    plan = _bind_plan({"APP_HOST": value}, "http://10.0.0.5:9000/")
    normalize_plan(plan)
    assert plan.app.env["APP_HOST"] == "0.0.0.0"


def test_local_only_service_keeps_loopback() -> None:
    """探针自己也指向回环时，说明就是本机自用：不改写用户的语义。"""
    plan = _bind_plan({"FISSUE_WEB_HOST": "127.0.0.1"}, "http://127.0.0.1:8000/")
    normalize_plan(plan)
    assert plan.app.env["FISSUE_WEB_HOST"] == "127.0.0.1"


def test_non_bind_env_is_untouched() -> None:
    plan = _bind_plan(
        {"FISSUE_LLM_BASE_URL": "http://127.0.0.1:11434/v1"}, "http://192.168.2.10:8000/"
    )
    normalize_plan(plan)
    assert plan.app.env["FISSUE_LLM_BASE_URL"] == "http://127.0.0.1:11434/v1"


# ---------------------------------------------------------------------------
# 启动前置项：执行前检查，而不是等第 20 步探针报 ConnectError
# ---------------------------------------------------------------------------


def _input_plan(env: dict[str, str], *, required: bool = True) -> DeployPlan:
    return DeployPlan(
        repo_slug="acme/demo",
        app=AppSpec(name="demo", env=env),
        steps=[],
        health=[ProbeSpec(url="http://192.168.2.10:8000/")],
        required_inputs=[
            RequiredInput(key="FISSUE_LLM_API_KEY", reason="LLM 密钥", required=required)
        ],
    )


@pytest.mark.parametrize(
    "value", ["sk-REPLACE-ME", "<your-key>", "changeme", "change_me", "YOUR_API_KEY"]
)
def test_placeholder_values_block_before_connecting(value: str) -> None:
    """占位符 = 计划根本没拿到真实值，继续跑只会制造一次注定失败的部署。"""
    plan = _input_plan({"FISSUE_LLM_API_KEY": value})
    with pytest.raises(DeployError) as excinfo:
        _check_startup_inputs(plan)
    message = str(excinfo.value)
    assert "FISSUE_LLM_API_KEY" in message
    assert "LLM 密钥" in message  # 原因要一起带出来，别让人再去翻计划


@pytest.mark.parametrize("value", ["sk-9f3ac2b1d4e5", "abcxdef1", "https://api.deepseek.com/v1"])
def test_real_values_pass(value: str) -> None:
    _check_startup_inputs(_input_plan({"FISSUE_LLM_API_KEY": value}))


def test_missing_required_input_only_warns() -> None:
    """计划没给、但远端 shared/.env 可能长期存着——不能当错误，误伤增量部署。"""
    plan = _input_plan({})
    _check_startup_inputs(plan)
    assert "需要人工提供" in plan.notes
    assert "FISSUE_LLM_API_KEY" in plan.notes


def test_missing_optional_input_is_silent() -> None:
    plan = _input_plan({}, required=False)
    _check_startup_inputs(plan)
    assert "FISSUE_LLM_API_KEY" not in plan.notes


def test_required_inputs_default_is_empty() -> None:
    """老计划（没有这个字段）要继续能读——契约只能加字段，不能改必填。"""
    plan = DeployPlan(
        repo_slug="a/b", app=AppSpec(name="x"), health=[ProbeSpec(url="http://h:1/")]
    )
    assert plan.required_inputs == []


# ---------------------------------------------------------------------------
# unit 运行身份：缺 User= 要补，写了 User=root 也要改
# ---------------------------------------------------------------------------


def _unit_plan(content: str) -> DeployPlan:
    from fissue.deploy.schema import WriteFileSpec

    return DeployPlan(
        repo_slug="acme/demo",
        app=AppSpec(name="demo", service_name="demo", service_sudo=True),
        steps=[
            DeployStep(
                id="write-unit",
                kind=StepKind.WRITE_FILE,
                write=WriteFileSpec(path="deploy/demo.service", content=content),
            ),
            DeployStep(
                id="install-unit",
                kind=StepKind.EXEC,
                exec=__import__("fissue.deploy.schema", fromlist=["ExecSpec"]).ExecSpec(
                    command="sudo -n install -m 644 deploy/demo.service /etc/systemd/system/demo.service"
                ),
            ),
        ],
        health=[ProbeSpec(url="http://192.168.2.10:8000/")],
    )


def _unit_content(plan: DeployPlan) -> str:
    return next(
        step.write.content
        for step in plan.steps
        if step.write and step.write.path.endswith(".service")
    )


def test_unit_user_injected_when_missing() -> None:
    plan = _unit_plan("[Service]\nExecStart=/bin/true\n")
    normalize_plan(plan, host_user="john", host_address="192.168.2.10")
    assert "User=john" in _unit_content(plan)


def test_unit_user_root_is_rewritten() -> None:
    """真机事故：模型主动写 `User=root` + `Group=root`，旧实现看到 User= 就不管了，
    结果一个公网可达的服务以 root 运行，而部署判定是 success。"""
    plan = _unit_plan("[Service]\nUser=root\nGroup=root\nExecStart=/bin/true\n")
    normalize_plan(plan, host_user="john", host_address="192.168.2.10")
    content = _unit_content(plan)
    assert "User=john" in content
    assert "User=root" not in content
    assert "Group=root" not in content, "Group=root 必须去掉，否则仍以 root 组运行"


def test_unit_user_zero_is_rewritten() -> None:
    plan = _unit_plan("[Service]\nUser=0\nExecStart=/bin/true\n")
    normalize_plan(plan, host_user="john", host_address="192.168.2.10")
    assert "User=john" in _unit_content(plan)


def test_existing_non_root_user_is_kept() -> None:
    """已经写成具体普通用户就别动：那可能是使用者有意的安排。"""
    plan = _unit_plan("[Service]\nUser=deploy\nExecStart=/bin/true\n")
    normalize_plan(plan, host_user="john", host_address="192.168.2.10")
    assert "User=deploy" in _unit_content(plan)
    assert "User=john" not in _unit_content(plan)


# ---------------------------------------------------------------------------
# docker 权限：按组授予，不是整进程 root
# ---------------------------------------------------------------------------


def _docker_plan(*, unit: str, needs_docker: bool) -> DeployPlan:
    from fissue.deploy.schema import ExecSpec, WriteFileSpec

    steps = [
        DeployStep(
            id="write-unit",
            kind=StepKind.WRITE_FILE,
            write=WriteFileSpec(path="deploy/demo.service", content=unit),
        ),
        DeployStep(
            id="install-unit",
            kind=StepKind.EXEC,
            exec=ExecSpec(
                command="sudo -n install -m 644 deploy/demo.service /etc/systemd/system/demo.service"
            ),
        ),
    ]
    if needs_docker:
        steps.append(
            DeployStep(
                id="docker-check", kind=StepKind.EXEC, exec=ExecSpec(command="docker version")
            )
        )
    return DeployPlan(
        repo_slug="acme/demo",
        app=AppSpec(name="demo", service_name="demo", service_sudo=True),
        steps=steps,
        health=[ProbeSpec(url="http://192.168.2.10:8000/")],
    )


def test_docker_group_added_when_plan_uses_docker() -> None:
    """需要 docker 时用 SupplementaryGroups 授予，**并把 User=root 改成部署用户**。

    用户确认过的结论：docker 确实要 root（或 docker 组）权限，但标准做法是
    只给这个服务一个附加组，不是让整个进程以 root 跑。
    """
    plan = _docker_plan(unit="[Service]\nUser=root\nExecStart=/bin/true\n", needs_docker=True)
    normalize_plan(plan, host_user="john")
    content = _unit_content(plan)
    assert "User=john" in content and "User=root" not in content
    assert "SupplementaryGroups=docker" in content


def test_docker_group_not_added_without_docker() -> None:
    plan = _docker_plan(unit="[Service]\nUser=john\nExecStart=/bin/true\n", needs_docker=False)
    normalize_plan(plan, host_user="john")
    assert "SupplementaryGroups" not in _unit_content(plan)


def test_existing_supplementary_groups_are_not_overwritten() -> None:
    """模型可能已经写了别的组，覆盖等于削权限。"""
    plan = _docker_plan(
        unit="[Service]\nUser=john\nSupplementaryGroups=video\nExecStart=/bin/true\n",
        needs_docker=True,
    )
    normalize_plan(plan, host_user="john")
    content = _unit_content(plan)
    assert "SupplementaryGroups=video" in content
    assert "docker" not in content.split("ExecStart")[0]


def test_uploading_a_docker_directory_does_not_trigger_group_grant() -> None:
    """上传 `docker/` 目录不代表要连 docker daemon——只看命令与 unit 内容。"""
    from fissue.deploy.schema import UploadSpec

    plan = _docker_plan(unit="[Service]\nUser=john\nExecStart=/bin/true\n", needs_docker=False)
    plan.steps.append(
        DeployStep(id="upload-docker", kind=StepKind.UPLOAD, upload=UploadSpec(src="docker", dst="docker"))
    )
    normalize_plan(plan, host_user="john")
    assert "SupplementaryGroups" not in _unit_content(plan)
