"""计划归一化：把「模型漏写但可由计划本身推断」的细节补正。

为什么要有这一层
----------------
提示词是**请求**，不是**保证**。真机验证时模型在同一份提示词下依然出现两类漏洞：

1. 计划自己 `sudo -n install` 了 `/etc/systemd/system/<unit>`，但 `service` 步骤
   却没置 `sudo`——于是 `daemon-reload` 立刻以 "Interactive authentication required"
   失败，前十几步全白跑；
2. `health` 探针写成 `http://rpi:5000/`——`rpi` 是 Fissue 里的**主机别名**，控制端
   根本解析不了，探针必然 ConnectError。

这些都能从计划/主机上下文里**确定地**推出来，不该靠模型每次都记得。归一化只做
「补齐」与「换名」，**不删除、不改写模型要求的动作语义**；每一处改动都会写进
`plan.notes`，审计时看得见。

刻意不做的事：不替模型补 unit 内容、不补装依赖、不猜端口。那些是计划该有的东西，
补不出来就该让部署失败——而不是由我们编一个能跑的计划。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from ..logging_setup import get_logger
from .schema import DeployPlan, ProbeSpec, ServiceSpec, WriteFileSpec

log = get_logger(__name__)

#: 系统级 unit 目录。计划一旦往这里装文件，`systemctl` 就必须提权。
SYSTEM_UNIT_DIR = "/etc/systemd/system"

#: 用户级 unit 目录：装到这里时**不需要** sudo，归一化必须区分开。
USER_UNIT_HINT = "/.config/systemd/user"


def normalize_plan(
    plan: DeployPlan,
    *,
    host_name: str = "",
    host_address: str = "",
    host_user: str = "",
) -> DeployPlan:
    """就地补正计划，返回同一个对象（便于调用方继续使用）。

    幂等：对已经正确的计划再跑一次不会有任何变化。
    """
    changes: list[str] = []

    if _installs_system_unit(plan) and plan.app.service_name:
        if not plan.app.service_sudo:
            plan.app.service_sudo = True
            changes.append("app.service_sudo=true（计划要把 unit 装进 /etc/systemd/system）")
        for step in plan.steps:
            spec = step.spec
            if isinstance(spec, ServiceSpec) and not spec.sudo:
                # pydantic 的赋值校验开着（validate_assignment），这里改的是嵌套模型字段。
                spec.sudo = True
                changes.append(f"步骤 {step.id}: service.sudo=true")
        if host_user:
            injected = _ensure_unit_user(plan, host_user)
            if injected:
                changes.append(f"unit 补 User={host_user}（缺了会以 root 运行）")

    if host_name and host_address:
        # `health` 与**内联 probe 步骤**都要改：真机验证时只改了 health，
        # 而计划里那条 `probe: http://rpi:5000/` 步骤仍然带着别名，执行到它就
        # getaddrinfo 失败——一步之差，整次部署白跑。
        for index, spec in enumerate(plan.health):
            rewritten = _rewrite_alias(spec.url, host_name, host_address)
            if rewritten and rewritten != spec.url:
                changes.append(f"health[{index}] {spec.url} → {rewritten}（别名换成真实地址）")
                spec.url = rewritten
        for step in plan.steps:
            spec = step.spec
            if not isinstance(spec, ProbeSpec):
                continue
            url = spec.url
            rewritten = _rewrite_alias(url, host_name, host_address)
            if rewritten and rewritten != url:
                changes.append(f"步骤 {step.id}: {url} → {rewritten}（别名换成真实地址）")
                spec.url = rewritten

    if changes and changes != ["__noop__"]:
        note = "；".join(changes)
        plan.notes = (plan.notes + "\n" if plan.notes else "") + f"[归一化] {note}"
        log.info("部署计划已归一化：%s", note)
    return plan


def _ensure_unit_user(plan: DeployPlan, user: str) -> bool:
    """给要装进系统的 unit 补一行 ``User=<部署用户>``。

    缺了它 systemd 会以 **root** 跑应用——真机验证时的实际后果：
    `__pycache__` / SQLite 全归 root，之后普通用户连 release 都删不掉
    （`destroy` 的 `rm -rf` 一报权限错误），而且一个公网可达的 Flask 应用
    以 root 运行本身就是安全事故。

    只改真正的 unit 文件（`write_file` 且路径以 ``.service`` 结尾）；
    已经写了 ``User=`` 的不动。
    """
    changed = False
    for step in plan.steps:
        spec = step.spec
        if not isinstance(spec, WriteFileSpec):
            continue
        content = spec.content or ""
        if not spec.path.endswith(".service") or "User=" in content:
            continue
        if "[Service]" not in content:
            continue
        spec.content = content.replace("[Service]", f"[Service]\nUser={user}", 1)
        changed = True
    return changed


def _installs_system_unit(plan: DeployPlan) -> bool:
    """计划里是否存在「把 unit 装到系统级目录」的动作。

    同时看 `exec` 命令与 `write_file` 落点：模型既可能 `sudo install`，也可能
    直接写绝对路径（虽然写不进去，但那说明它的意图是系统级）。
    """
    for step in plan.steps:
        spec = step.spec
        command = getattr(spec, "command", "") or ""
        path = getattr(spec, "path", "") or ""
        if systemd_target(command) or systemd_target(path):
            return True
    return False


def systemd_target(text: str) -> bool:
    """这段文本是否指向系统级 unit 目录（且不是用户级目录）。"""
    if not text:
        return False
    if USER_UNIT_HINT in text:
        return False
    if SYSTEM_UNIT_DIR in text:
        return True
    # `systemctl --user ...` 是用户级作用域，不需要 root。
    return bool(re.search(r"systemctl\s+(?!--user)", text)) and "sudo" in text


def _rewrite_alias(url: str | None, host_name: str, host_address: str) -> str | None:
    """把探针 URL 里的主机别名换成真实地址。

    只换**主机名完全相同**的情况：`rpi` → `192.168.2.10`。别的情况下（用户就写了
    真实域名、或写了别的机器）一律不动——那可能是故意指向 CDN 或反代。
    """
    if not url:
        return None
    parts = urlsplit(url)
    if parts.hostname != host_name:
        return None
    netloc = host_address
    if parts.port:
        netloc = f"{host_address}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))