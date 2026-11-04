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
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import tomllib

from ..logging_setup import get_logger
from .schema import (
    DeployPlan,
    DeployStep,
    ProbeSpec,
    ServiceSpec,
    StepKind,
    UploadSpec,
    WriteFileSpec,
)

log = get_logger(__name__)

#: 系统级 unit 目录。计划一旦往这里装文件，`systemctl` 就必须提权。
SYSTEM_UNIT_DIR = "/etc/systemd/system"

#: 用户级 unit 目录：装到这里时**不需要** sudo，归一化必须区分开。
USER_UNIT_HINT = "/.config/systemd/user"

#: 只在本机可达的监听地址。服务绑这些地址时，**控制端**的探针一定连不上。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})

#: 承载「监听地址」的环境变量名（按名字判断，不猜语言/框架）。
BIND_KEY_HINT = re.compile(r"(HOST|ADDR|ADDRESS|BIND|LISTEN)", re.IGNORECASE)


def normalize_plan(
    plan: DeployPlan,
    *,
    host_name: str = "",
    host_address: str = "",
    host_user: str = "",
    source_root: Path | str | None = None,
) -> DeployPlan:
    """就地补正计划，返回同一个对象（便于调用方继续使用）。

    幂等：对已经正确的计划再跑一次不会有任何变化。

    ``source_root`` 是**本地源码目录**（计划里的相对路径都相对它）。有它才能检查
    「pyproject 里 `readme = "README.md"`，计划却只上传了 pyproject」这类缺口；
    没有它时这条规则整条跳过，不做任何猜测。
    """
    changes: list[str] = []

    # 打包元数据引用到的文件必须一起上传：真机验证时 AI 只传了 pyproject.toml，
    # hatchling 在生成元数据时 `readme` 直接 OSError，装包那一步才失败。
    changes.extend(_ensure_packaging_files(plan, source_root))

    # 服务绑回环地址时，**控制端**的探针永远连不上：要么让服务监听外部地址，
    # 要么这次部署的「独立证据」就永远拿不到。真机上 AI 侥幸写对了，不该靠运气。
    changes.extend(_ensure_reachable_bind(plan))

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
                changes.append(
                    f"unit 运行身份改为 {host_user}（缺 User= 或以 root 运行都会被改写）"
                )
        # 需要 docker 时按组授予权限：短路写法保证「不需要 docker」时不去改 unit。
        if _unit_needs_docker(plan) and _ensure_docker_group(plan):
            changes.append(
                "unit 补 SupplementaryGroups=docker（需要 docker 权限时按组授予，"
                "而不是整进程 root）"
            )

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


def _ensure_packaging_files(plan: DeployPlan, source_root: Path | str | None) -> list[str]:
    """把打包元数据**引用到**的文件补进上传步骤。

    真机验证时 AI 只上传了 ``pyproject.toml`` / ``src`` / ``config.example.yaml``，
    而 pyproject 里有 ``readme = "README.md"``——hatchling 生成元数据时直接
    ``OSError``，前面十几步全白跑，报错还是埋在 pip 的构建日志里。

    只补**确实存在**的文件：源码目录里没有的（比如计划本来就只带了一部分），
    补一个上传步骤只会在执行时以"文件不存在"失败，不如不补。
    """
    if not source_root:
        return []
    root = Path(source_root)
    if not root.is_dir():
        return []

    referenced = _packaging_referenced_files(root)
    if not referenced:
        return []

    covered = _uploaded_paths(plan)
    missing = [rel for rel in referenced if rel not in covered]
    if not missing:
        return []

    # 插在最后一个上传步骤之后：保持「先传文件、再写配置、再执行」的自然顺序，
    # 也避免把上传步骤混进后面的 exec 序列里。
    insert_at = 0
    for index, step in enumerate(plan.steps):
        if isinstance(step.spec, UploadSpec):
            insert_at = index + 1

    new_steps: list[DeployStep] = []
    for rel in missing:
        handle = re.sub(r"[^A-Za-z0-9]+", "-", rel).strip("-").lower()[:40] or "file"
        step_id = f"upload-{handle}"
        taken = {s.id for s in plan.steps} | {s.id for s in new_steps}
        suffix = 2
        base = step_id
        while step_id in taken:
            step_id = f"{base}-{suffix}"
            suffix += 1
        new_steps.append(
            DeployStep(
                id=step_id,
                kind=StepKind.UPLOAD,
                title=f"上传 {rel}（打包元数据引用它，缺了构建会失败）",
                upload=UploadSpec(src=rel, dst=rel),
            )
        )
    plan.steps[insert_at:insert_at] = new_steps
    return [f"补上传步骤：{'、'.join(missing)}（pyproject/setup 引用，之前漏了）"]


def _packaging_referenced_files(root: Path) -> list[str]:
    """``pyproject.toml`` 里被引用的文件（readme / license / 动态版本文件）。"""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):  # 坏文件交给构建去报错，这里不猜
        return []

    referenced: list[str] = []
    project = data.get("project") or {}
    readme = project.get("readme")
    if isinstance(readme, str):
        referenced.append(readme)
    elif isinstance(readme, dict) and readme.get("file"):
        referenced.append(str(readme["file"]))
    license_spec = project.get("license")
    if isinstance(license_spec, dict) and license_spec.get("file"):
        referenced.append(str(license_spec["file"]))

    tool = data.get("tool") or {}
    hatch_version = (tool.get("hatch") or {}).get("version") or {}
    if isinstance(hatch_version, dict) and hatch_version.get("path"):
        referenced.append(str(hatch_version["path"]))
    setuptools_dynamic = ((tool.get("setuptools") or {}).get("dynamic")) or {}
    if isinstance(setuptools_dynamic, dict):
        version = setuptools_dynamic.get("version")
        if isinstance(version, dict) and version.get("file"):
            referenced.append(str(version["file"]))

    out: list[str] = []
    for rel in dict.fromkeys(referenced):
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        if (root / candidate).is_file():
            out.append(rel)
    return out


def _uploaded_paths(plan: DeployPlan) -> set[str]:
    """计划里已经覆盖的上传路径（相对 release 目录）。"""
    covered: set[str] = set()
    for step in plan.steps:
        spec = step.spec
        if not isinstance(spec, UploadSpec):
            continue
        for value in (spec.src, spec.dst):
            if not value:
                continue
            normalised = value.strip("./")
            covered.add(normalised)
    return covered


def _ensure_reachable_bind(plan: DeployPlan) -> list[str]:
    """服务不能只听回环：控制端的探针要连得上。

    判据是"计划里存在指向**非回环**主机的探针"——那说明这次部署要的是"外部可访问"。
    只有在那种情况下才改写监听地址；纯本机服务不动，免得替用户改了语义。
    """
    if not _has_remote_probe(plan):
        return []

    changes: list[str] = []
    for key, value in list(plan.app.env.items()):
        if not BIND_KEY_HINT.search(key):
            continue
        if str(value).strip().strip("\"'").lower() in LOOPBACK_HOSTS:
            plan.app.env[key] = "0.0.0.0"
            changes.append(
                f"app.env {key}: {value} → 0.0.0.0（探针从控制端发起，回环地址不可达）"
            )
    return changes


def _has_remote_probe(plan: DeployPlan) -> bool:
    """计划里是否存在指向非回环地址的探针。"""
    urls = [spec.url for spec in plan.health if spec.url]
    urls.extend(
        step.spec.url
        for step in plan.steps
        if isinstance(step.spec, ProbeSpec) and step.spec.url
    )
    for url in urls:
        host = urlsplit(url).hostname
        if host and host.lower() not in LOOPBACK_HOSTS:
            return True
    return False


def _ensure_unit_user(plan: DeployPlan, user: str) -> bool:
    """unit 必须跑在**部署用户**下：缺 ``User=`` 要补，写了 ``User=root`` 要改。

    缺了它 systemd 会以 root 跑应用——真机验证时的实际后果：`__pycache__` /
    SQLite 全归 root，之后普通用户连 release 都删不掉（`destroy` 的 `rm -rf`
    一报权限错误），而且一个公网可达的服务以 root 运行本身就是安全事故。

    **写 `User=root` 也要改**：2026-11-04 真机部署 Fissue 自己时，模型主动写了
    ``User=root`` + ``Group=root``，还在 notes 里解释"沙盒 runtime=docker 需要
    docker 权限"。旧实现只在缺 ``User=`` 时注入，于是 root 被原样保留、部署判定
    却是 success——证据链里没有"运行身份"这一项，没人发现。

    真要 docker 权限的正确做法是把用户加进 docker 组（``usermod -aG docker`` 或
    unit 里 ``SupplementaryGroups=docker``），而不是让整个进程以 root 跑。
    这里只改运行身份，权限需求交给使用者在 notes 里看到后决定。
    """
    changed = False
    for step in plan.steps:
        spec = step.spec
        if not isinstance(spec, WriteFileSpec):
            continue
        content = spec.content or ""
        if not spec.path.endswith(".service") or "[Service]" not in content:
            continue

        updated = re.sub(r"(?im)^User\s*=\s*(?:root|0)\s*$", f"User={user}", content)
        # `Group=root` 直接删掉：systemd 会回落到该用户的**主组**，
        # 不假设"存在一个和用户名同名的组"（那是常见情况，但不是保证）。
        updated = re.sub(r"(?im)^Group\s*=\s*(?:root|0)\s*\n?", "", updated)
        if not re.search(r"(?im)^User\s*=", updated):
            updated = updated.replace("[Service]", f"[Service]\nUser={user}", 1)

        if updated != content:
            spec.content = updated
            changed = True
    return changed


#: 需要 docker 的线索。只看**命令与 unit 内容**，不看路径：
#: 计划里上传一个叫 `docker/` 的目录不代表要连 docker daemon。
_DOCKER_HINT = re.compile(r"(?i)\bdocker\b")


def _unit_needs_docker(plan: DeployPlan) -> bool:
    """这份计划是否要用 docker（沙盒、compose、`docker run` 等）。"""
    for step in plan.steps:
        spec = step.spec
        for text in (
            getattr(spec, "command", "") or "",
            getattr(spec, "content", "") or "",
        ):
            if _DOCKER_HINT.search(text):
                return True
    return bool(_DOCKER_HINT.search(plan.notes or ""))


def _ensure_docker_group(plan: DeployPlan) -> bool:
    """需要 docker 时，给 unit 补 ``SupplementaryGroups=docker``。

    为什么是这条而不是 ``usermod -aG docker``：两者都能让进程拿到 docker 权限，
    但侧重点不同——

    * ``usermod`` 改的是**系统账户**，会影响该用户的每个会话与进程，
      而且"改了组、正在跑的进程仍用旧组"这件事很容易被忽略（要重新登录/重启）；
    * ``SupplementaryGroups=`` 只作用**这一个 unit**，systemd 以 root 身份设置附加组，
      不需要用户事先被加进组。权限范围就是"这个服务"，回滚也只需改 unit 文件。

    所以标准做法是：unit 级授予 + 只授予需要的那个组。真要执行 docker 命令的服务
    仍然**不以 root 运行**（User 仍是部署用户）。

    已经有 ``SupplementaryGroups=`` 的不动——模型可能已经写了别的组，覆盖会削权限。
    """
    changed = False
    for step in plan.steps:
        spec = step.spec
        if not isinstance(spec, WriteFileSpec):
            continue
        content = spec.content or ""
        if not spec.path.endswith(".service") or "[Service]" not in content:
            continue
        if re.search(r"(?im)^SupplementaryGroups\s*=", content):
            continue
        if not re.search(r"(?im)^User\s*=", content):
            continue  # 没有 User= 的 unit 归 _ensure_unit_user 管，这里不越界
        spec.content = content.replace(
            "[Service]", "[Service]\nSupplementaryGroups=docker", 1
        )
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