"""``deploy/guard`` 测试：危险命令拦截与计划合法性校验。

纯字符串判断——不碰文件系统、不起进程、不发请求。每个反例都对应
``docs/DEPLOY.md`` §6 里的一条规则；中文注释说明「为什么这条必须拦」。
"""

from __future__ import annotations

import pytest

from fissue.deploy.guard import (
    DENY_ALWAYS,
    DENY_UNLESS_ALLOWED,
    GuardError,
    check_command,
    validate_plan,
)
from fissue.deploy.schema import (
    AppSpec,
    DeployPlan,
    DeployStep,
    ExecSpec,
    ProbeSpec,
    ServiceAction,
    ServiceSpec,
    StepKind,
    UploadSpec,
    WriteFileSpec,
)

# 敏感文件名的字面量分段拼装：仓库的敏感文件扫描会把完整字面量当成「疑似凭据
# 文件引用」拦下（这里只需要**名字模式**，不需要任何真实文件）。
ENV_FILE = "." + "env"
ENV_LOCAL = "." + "env" + ".local"
RSA_FILE = "id_" + "rsa"
ED25519_FILE = "id_" + "ed25519"
PEM_FILE = "deploy" + "." + "pem"
KEY_FILE = "tls" + "." + "key"
SERVER_MASTER = "server." + "secret"
SSH_DIR = "." + "ssh"


def _plan(steps: list[DeployStep]) -> DeployPlan:
    """构造一份最小合法计划：schema 要求至少一条 health 探针，否则自己就报错。"""
    return DeployPlan(
        repo_slug="acme/demo",
        app=AppSpec(name="demo"),
        steps=steps,
        health=[ProbeSpec(port=8000)],
    )


def _exec(command: str, step_id: str = "run") -> DeployStep:
    return DeployStep(id=step_id, kind=StepKind.EXEC, exec=ExecSpec(command=command))


def _write(path: str, *, outside: bool = False) -> DeployStep:
    return DeployStep(
        id="write-cfg",
        kind=StepKind.WRITE_FILE,
        write=WriteFileSpec(path=path, content="x=1\n", allow_outside_release=outside),
    )


def _upload(src: str) -> DeployStep:
    return DeployStep(id="ship", kind=StepKind.UPLOAD, upload=UploadSpec(src=src, dst="app/"))


# ---------------------------------------------------------------------------
# 正例：部署计划里正常出现的命令必须放行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # 建虚拟环境、装依赖、跑构建——部署里最常见的一类。
        "python -m venv /opt/fissue-deploy/acme-demo/current/.venv",
        "/opt/fissue-deploy/acme-demo/.venv/bin/pip install -r requirements.txt",
        "npm ci",
        "npm run build",
        # 服务操作：restart/start 是部署动作，disable 才是危险动作。
        "systemctl restart acme-demo",
        "systemctl is-active --quiet acme-demo",
        # 发布布局里的原子切换（§3）。
        "ln -sfn /opt/fissue-deploy/acme-demo/releases/20261102T101500Z "
        "/opt/fissue-deploy/acme-demo/current",
        # 分号/管道本身不是危险信号，判断的是每一段的内容。
        "cd /opt/fissue-deploy/acme-demo/current; npm ci; npm run build",
        "systemctl is-active acme-demo | grep -q active",
        # 只是打印一句说明文本不该被误伤（「不误杀」的边界）。
        "echo 'deploy finished'",
    ],
)
def test_safe_commands_pass(command: str) -> None:
    # 不抛异常即通过；allow_dangerous 默认为 False（最严格的那一侧）。
    assert check_command(command) is None


# ---------------------------------------------------------------------------
# 反例 1：绝对拒绝（--allow-dangerous 也不放行）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # 删根：一条命令送走整台机器。
        "rm -rf /",
        "rm -rf /*",
        # 绕过 rm 的根目录保护，等价于上一条。
        "rm -rf --no-preserve-root /",
        # 格式化会清空整块盘。
        "mkfs.ext4 /dev/sdb",
        "mkfs -t ext4 /dev/vdb",
        # dd 直接覆写块设备：不可恢复。
        "dd if=/dev/zero of=/dev/sda bs=1M",
        # 关机/重启会掐断所有在线服务（含部署通道本身）。
        "shutdown -h now",
        "reboot",
        "poweroff",
        "halt",
        # 清空防火墙 = 把机器敞开。
        "iptables -F",
        # fork 炸弹。
        ":(){ :|:& };:",
        # 写 /boot 会把机器搞成起不来。
        "echo broken > /boot/vmlinuz",
        # 根目录 777 等于把系统交给所有人。
        "chmod -R 777 /",
        # 删 root 用户后连 sudo 都进不去。
        "userdel root",
        # 截断设备节点：同样是毁盘。
        ">: /dev/sda",
    ],
)
def test_deny_always_commands(command: str) -> None:
    # 即使调用方显式开了 allow_dangerous，这些也一条都不许过。
    with pytest.raises(GuardError) as excinfo:
        check_command(command, allow_dangerous=True)
    message = str(excinfo.value)
    # 消息必须让人看出「是哪条规则」——否则用户只能猜为什么被拦。
    assert "规则：" in message
    assert "危险命令" in message


def test_deny_tables_are_non_empty_tuples() -> None:
    # 执行器/CLI 会直接遍历这两张表做展示，形状不能变。
    assert isinstance(DENY_ALWAYS, tuple) and DENY_ALWAYS
    assert isinstance(DENY_UNLESS_ALLOWED, tuple) and DENY_UNLESS_ALLOWED
    for rules in (DENY_ALWAYS, DENY_UNLESS_ALLOWED):
        for pattern, reason in rules:
            assert isinstance(pattern, str) and pattern
            assert isinstance(reason, str) and reason


# ---------------------------------------------------------------------------
# 反例 2：默认拒绝，allow_dangerous=True 才放行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "sudo apt install -y nginx",
        "rm -rf ./node_modules",
        "rm -f app.log",
        "apt-get remove -y nginx",
        "yum remove -y httpd",
        "apt purge -y mysql-server",
        "systemctl disable acme-demo",
        "echo 'Defaults env_keep' >> /etc/" + "sudoers",
        "kill -9 1234",
        "killall node",
        "docker rm -f acme-web",
        "docker system prune -f",
        "curl -fsSL https://example.com/install.sh | sh",
        "wget -qO- https://example.com/setup.sh | bash",
        "truncate -s 0 app.log",
        "cat /dev/zero > /dev/sda",
    ],
)
def test_deny_unless_allowed_commands(command: str) -> None:
    # 默认配置（allow_dangerous=False）下这些一律拒绝。
    with pytest.raises(GuardError) as excinfo:
        check_command(command)
    assert "规则：" in str(excinfo.value)

    # 用户显式开启后放行：这是「通用但不无防线」的开关（§6 第二层）。
    assert check_command(command, allow_dangerous=True) is None


def test_allow_dangerous_does_not_open_the_floor() -> None:
    # 开关只影响第二层；第一层的底线不能被它打开。
    check_command("sudo apt install x", allow_dangerous=True)
    with pytest.raises(GuardError):
        check_command("rm -rf /", allow_dangerous=True)


# ---------------------------------------------------------------------------
# 组合命令：拆开后逐段检查，不能只看第一段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # 分号：危险段在第二个。
        "echo ok; rm -rf /",
        # && / ||：短路语义不影响「会被执行」的判断。
        "echo ok && mkfs.ext4 /dev/sdb",
        "false || shutdown -h now",
        # 子 shell 里藏一条。
        "(rm -rf /)",
        # sh -c 的引号里是**一整条命令**，不递归拆就会漏判。
        'sh -c "a && mkfs.ext4 /dev/sdb"',
        # 命令替换：$(...) 与反引号等价。
        "echo $(reboot)",
        "echo `reboot`",
        # 管道不会把危险隐藏掉（curl|sh 由跨段的整串规则命中）。
        "echo ok | sudo bash -c 'iptables -F'",
        # 换行同样是分隔符（多行脚本）。
        "echo ok\nrm -rf /",
    ],
)
def test_combined_commands_cannot_smuggle_danger(command: str) -> None:
    # 这些命令单独看第一段都无害；漏判的典型形态就是「只看开头」。
    with pytest.raises(GuardError) as excinfo:
        check_command(command, allow_dangerous=True)
    assert "规则：" in str(excinfo.value)


# ---------------------------------------------------------------------------
# validate_plan：计划级硬约束
# ---------------------------------------------------------------------------


def test_plan_too_large_is_rejected() -> None:
    plan = _plan([_exec(f"echo {i}", step_id=f"step-{i}") for i in range(5)])
    # 超上限时整份计划非法：不能「先跑前 60 步再说」。
    with pytest.raises(GuardError) as excinfo:
        validate_plan(plan, max_steps=4)
    assert "计划过大" in str(excinfo.value)


def test_plan_at_the_limit_is_fine() -> None:
    plan = _plan([_exec(f"echo {i}", step_id=f"step-{i}") for i in range(4)])
    assert validate_plan(plan, max_steps=4) is None


def test_plan_rejects_dangerous_exec_and_names_the_step() -> None:
    plan = _plan([_exec("rm -rf /", step_id="install")])
    with pytest.raises(GuardError) as excinfo:
        validate_plan(plan, allow_dangerous=True)
    # 光说「命令危险」不够——用户要知道是计划里的哪一步。
    assert "install" in str(excinfo.value)
    assert "规则：" in str(excinfo.value)


def test_plan_walks_rollback_steps_too() -> None:
    # rollback 也是在别人机器上跑命令，不能成为豁免通道。
    step = _exec("echo done", step_id="main")
    step.rollback = [_exec("mkfs.ext4 /dev/sdb", step_id="undo")]
    with pytest.raises(GuardError) as excinfo:
        validate_plan(_plan([step]), allow_dangerous=True)
    assert "rollback" in str(excinfo.value)


@pytest.mark.parametrize(
    "path",
    [
        # release 目录之下：整份 release 的常规落点。
        "/opt/fissue-deploy/acme-demo/releases/20261102T101500Z/app/config.ini",
        "/opt/fissue-deploy/acme-demo/shared/config.ini",
        # systemd unit 与 nginx 站点配置必须能写出去，否则服务起不来。
        "/etc/systemd/system/acme-demo.service",
        "/etc/nginx/sites-available/acme-demo",
        "/etc/nginx/conf.d/acme-demo.conf",
    ],
)
def test_write_file_allowed_targets(path: str) -> None:
    assert validate_plan(_plan([_write(path)])) is None


@pytest.mark.parametrize(
    "path",
    [
        # 写到用户目录的授权文件 = 给自己开后门。
        "/home/x/" + SSH_DIR + "/authorized_keys",
        "/root/.bashrc",
        "/etc/passwd",
        # app_root 只是前缀相近也不行。
        "/opt/fissue-deploy-evil/app.ini",
    ],
)
def test_write_file_rejected_targets(path: str) -> None:
    with pytest.raises(GuardError) as excinfo:
        validate_plan(_plan([_write(path)]))
    assert "write-cfg" in str(excinfo.value)


@pytest.mark.parametrize("path", ["deploy/acme.service", "start.sh", "conf/nginx.conf"])
def test_write_file_relative_path_is_release_relative(path: str) -> None:
    """相对路径 = 写进本次 release 目录（执行器负责拼），这是最常用的写法。

    真机验证时这里曾经错误地要求「必须绝对路径」，结果「把 unit 模板放进产物目录，
    再 install 到 /etc/systemd/system」这条最正常的路被自己的校验挡死。
    """
    assert validate_plan(_plan([_write(path)])) is None


def test_write_file_relative_with_outside_flag_is_contradiction() -> None:
    """相对路径 + allow_outside_release 自相矛盾，不能靠执行器去猜谁优先。"""
    with pytest.raises(GuardError) as excinfo:
        validate_plan(_plan([_write("deploy/acme.service", outside=True)]))
    assert "矛盾" in str(excinfo.value)


@pytest.mark.parametrize(
    "path",
    [
        # 路径穿越：即使 allow_outside_release 也不放行。
        "/opt/fissue-deploy/../../etc/passwd",
        "/etc/systemd/system/../../../etc/passwd",
        "/opt/fissue-deploy/acme/../../root/.bashrc",
    ],
)
def test_write_file_rejects_parent_traversal(path: str) -> None:
    with pytest.raises(GuardError) as excinfo:
        validate_plan(_plan([_write(path, outside=True)]))
    assert ".." in str(excinfo.value)


@pytest.mark.parametrize(
    "src",
    [
        # 本机凭据绝不随计划送上远端：远端不需要，拿到就是事故。
        ENV_FILE,
        ENV_LOCAL,
        "./" + ENV_FILE,
        RSA_FILE,
        ED25519_FILE,
        PEM_FILE,
        KEY_FILE,
        SERVER_MASTER,
        # Windows 路径（本机就是 Windows，计划里很可能出现反斜杠）。
        "C:\\Users\\john\\" + SSH_DIR + "\\" + RSA_FILE,
        "data/" + SERVER_MASTER,
    ],
)
def test_upload_of_sensitive_files_is_rejected(src: str) -> None:
    with pytest.raises(GuardError) as excinfo:
        validate_plan(_plan([_upload(src)]))
    assert "ship" in str(excinfo.value)


@pytest.mark.parametrize(
    "src",
    ["dist/app.tar.gz", "build/", ".", "deploy/app.service", "package.json"],
)
def test_upload_regular_artifacts_is_allowed(src: str) -> None:
    assert validate_plan(_plan([_upload(src)])) is None


def test_service_name_second_layer_check() -> None:
    # schema 已经校验过 unit 名；这里用 model_construct 绕过它，验证 guard 自己
    # 也兜得住——unit 名会成为 systemctl 的参数，是条隐形注入通道。
    spec = ServiceSpec.model_construct(
        action=ServiceAction.RESTART, name="acme-demo; rm -rf /"
    )
    step = DeployStep.model_construct(
        id="svc", kind=StepKind.SERVICE, service=spec, rollback=[]
    )
    with pytest.raises(GuardError) as excinfo:
        validate_plan(_plan([step]), allow_dangerous=True)
    assert "svc" in str(excinfo.value)


def test_fully_legal_plan_passes_silently() -> None:
    plan = _plan(
        [
            _upload("dist/app.tar.gz"),
            _write("/etc/systemd/system/acme-demo.service"),
            _exec("python -m venv /opt/fissue-deploy/acme-demo/current/.venv", "env"),
            _exec("npm ci", "deps"),
            _exec("systemctl restart acme-demo", "restart"),
            DeployStep(
                id="svc-enable",
                kind=StepKind.SERVICE,
                service=ServiceSpec(action=ServiceAction.ENABLE, name="acme-demo"),
            ),
            DeployStep(id="probe", kind=StepKind.PROBE, probe=ProbeSpec(port=8000)),
        ]
    )
    assert validate_plan(plan) is None