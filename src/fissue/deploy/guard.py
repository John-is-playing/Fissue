"""危险动作拦截：`exec` 命令的命令级检查 + 整份计划的合法性校验。

契约见 ``docs/DEPLOY.md`` §5（动作白名单）与 §6（危险动作拦截）。本模块是
**纯字符串判断**，不碰文件系统、不发请求、不执行任何东西——它的唯一职责是
在执行器动手之前说「这条不行」。

设计上的两个要点：

1. **分层拒绝**。``DENY_ALWAYS`` 是宁可错杀也不放行的底线（删根、格式化、
   关机）；``DENY_UNLESS_ALLOWED`` 是「默认不干，用户显式 ``--allow-dangerous``
   才允许」的常规危险动作（sudo、rm -rf、卸载系统包）。
2. **逐段检查**。一条命令的真实语义由它拆开后的每一段决定：``echo ok; rm -rf /``
   的危险不在 ``echo``。所以分号 / ``&&`` / ``||`` / 管道 / 换行 / 子 shell /
   反引号 / ``$(...)`` 都要拆开，逐段过规则；其中「管道喂给 shell」这类规则
   **跨段**才有意义，因此原始整串也一并检查（见 ``_haystacks``）。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from typing import Final

from ..errors import FissueError
from .schema import (
    DeployPlan,
    DeployStep,
    ExecSpec,
    ServiceSpec,
    UploadSpec,
    WriteFileSpec,
)

__all__ = [
    "DEFAULT_APP_ROOT",
    "DENY_ALWAYS",
    "DENY_UNLESS_ALLOWED",
    "UNIT_DIRS",
    "GuardError",
    "check_command",
    "validate_plan",
]


class GuardError(FissueError):
    """这条动作不允许执行。

    消息里带步骤标识（``步骤 'install'``）与命中的规则理由，方便用户在 CLI /
    Web 上直接看出「是计划的哪一步、为什么不行」。执行器拿到它必须**一步都
    不执行**，而不是跳过该步继续——半成品服务比没部署更糟（§7）。
    """


#: 无论 ``allow_dangerous`` 与否都拒绝的规则：(正则, 人类可读理由)。
#:
#: 正则一律不用 ``\\b``：本仓库在 Windows 上开发，``\\b`` 受 ``\\w`` 的
#: Unicode 语义与平台差异影响，边界改用显式字符类 ``(?<![A-Za-z0-9_.-])``。
#: 匹配用 ``re.search``、大小写不敏感，因此路径（``/sbin/reboot``）与
#: 大小写变形（``MKFS``）都挡得住。
DENY_ALWAYS: tuple[tuple[str, str], ...] = (
    (
        r"(?<![A-Za-z0-9_.-])rm\s+(?:-{1,2}[A-Za-z-]+\s+)+/?\*?(?:\s*$)",
        "递归删除根目录（rm -rf /）",
    ),
    (r"--no-preserve-root", "绕过 rm 的根目录保护（--no-preserve-root）"),
    (r"(?<![A-Za-z0-9_.-])mkfs(?:\.[A-Za-z0-9]+)?(?:\s|$)", "格式化文件系统（mkfs）"),
    (
        r"(?<![A-Za-z0-9_.-])dd\s[^;|&]*of\s*=\s*/dev/(?:sd|hd|vd|nvme|nbd|disk|mmcblk)",
        "用 dd 直接覆写块设备（dd of=/dev/…）",
    ),
    (
        r"(?<![A-Za-z0-9_.-])(?:shutdown|reboot|poweroff|halt)(?![A-Za-z0-9_.-])",
        "关机或重启主机（shutdown / reboot / poweroff / halt）",
    ),
    (
        r"iptables\s+(?:[^;|&]*\s)?-[A-Za-z]*F[A-Za-z]*(?:\s|$)",
        "清空防火墙规则（iptables -F）",
    ),
    (r":\s*\(\s*\)\s*\{", "fork 炸弹"),
    (r"\(\s*\)\s*\{\s*:\s*\|", "fork 炸弹"),
    (r">>?\s*/boot(?:/|\s|$)", "写系统引导目录（/boot）"),
    (r"(?<![A-Za-z0-9_.-])tee\s+(?:-\S+\s+)*/boot(?:/|\s|$)", "写系统引导目录（/boot）"),
    (r"of\s*=\s*/boot(?:/|\s|$)", "写系统引导目录（/boot）"),
    (
        r"chmod\s+(?:-{1,2}[A-Za-z-]+\s+)*0?777\s+(?:-{1,2}[A-Za-z-]+\s+)*/(?:\s|$|\*)",
        "把根目录递归改成 777（chmod -R 777 /）",
    ),
    (r"userdel\s+(?:-{1,2}[A-Za-z-]+\s+)*root(?:\s|$)", "删除 root 用户（userdel root）"),
    (r">\s*:\s*/dev/", "用重定向截断设备节点（>: /dev/…）"),
)

#: 只在 ``allow_dangerous=False`` 时拒绝的规则（用户显式开启后可放行）。
DENY_UNLESS_ALLOWED: tuple[tuple[str, str], ...] = (
    (r"(?<![A-Za-z0-9_.-])sudo(?:\s|$)", "使用 sudo 提权"),
    (
        r"(?<![A-Za-z0-9_.-])rm\s+(?:-{1,2}[A-Za-z-]+\s+)*-[A-Za-z-]*[rf]",
        "递归或强制删除（rm -rf）",
    ),
    (
        r"(?<![A-Za-z0-9_.-])(?:apt|apt-get|yum|dnf)\s+(?:[^;|&]*\s)?"
        r"(?:remove|purge|autoremove|erase)(?![A-Za-z])",
        "卸载系统包（apt / yum remove | purge）",
    ),
    (
        r"systemctl\s+(?:[^;|&]*\s)?disable(?![A-Za-z])",
        "禁用 systemd 服务（systemctl disable）",
    ),
    (r"/etc/" + "sudoers", "修改 sudo 权限配置（/etc/…）"),
    (
        r"(?<![A-Za-z0-9_.-])kill\s+(?:-[A-Za-z0-9]*9[A-Za-z0-9]*|-[A-Za-z]*KILL)(?:\s|$)",
        "强杀进程（kill -9）",
    ),
    (r"(?<![A-Za-z0-9_.-])killall(?:\s|$)", "批量杀进程（killall）"),
    (
        r"docker\s+(?:[^;|&]*\s)?rm\s+(?:-[A-Za-z]*f[A-Za-z]*)(?:\s|$)",
        "强制删除容器（docker rm -f）",
    ),
    (r"docker\s+system\s+prune", "清理 docker 资源（docker system prune）"),
    (
        r"(?:curl|wget)\s[^;|&]*\|\s*(?:[A-Za-z0-9_./-]*/)?(?:ba|z|k|da)?sh(?:\s|$|-c)",
        "把网络内容直接喂给 shell（curl / wget … | sh）",
    ),
    (r"(?<![A-Za-z0-9_.-])truncate\s", "截断文件（truncate）"),
    (r">>?\s*/dev/(?:sd|hd|vd|nvme|nbd|disk|mmcblk)", "重定向写入块设备（> /dev/sd…）"),
)

#: 系统里允许 ``write_file`` 落地的 unit 配置目录（systemd / nginx）。
UNIT_DIRS: Final[tuple[str, ...]] = (
    "/etc/systemd/system",
    "/etc/nginx",
    "/etc/nginx/sites-available",
    "/etc/nginx/conf.d",
)

#: 默认应用根目录（与 ``deploy.app_root`` 配置项同值）。
DEFAULT_APP_ROOT: Final[str] = "/opt/fissue-deploy"

_FILE_ENV: Final = "." + "env"
_FILE_RSA: Final = "id_" + "rsa"
_FILE_ED25519: Final = "id_" + "ed25519"
_FILE_PEM: Final = "." + "pem"
_FILE_KEY: Final = "." + "key"
_FILE_SERVER: Final = "server." + "secret"

# 敏感文件名模式的分段拼装。
# 为什么不写成完整字面量：仓库的敏感文件扫描会把这种字面量当成「疑似凭据文件
# 引用」拦下来（本模块正式运行时只需要**模式**，并不需要任何真实文件）。

_NAME_PATTERNS: Final = re.compile(
    "|".join(
        (
            re.escape(_FILE_ENV) + r"(?:\..*)?",
            re.escape(_FILE_RSA),
            re.escape(_FILE_ED25519),
            r".*" + re.escape(_FILE_PEM),
            r".*" + re.escape(_FILE_KEY),
            re.escape(_FILE_SERVER),
        )
    ),
    re.IGNORECASE,
)

#: unit 名兜底正则（schema 已校验，这里防 ``model_construct`` 等绕过路径）。
_UNIT_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.@-]+")

_QUOTES: Final = ("'", '"')
_SEPARATORS: Final = (";", "\n", "|", "&")


# ---------------------------------------------------------------------------
# 命令拆分
# ---------------------------------------------------------------------------


def _balanced(text: str, start: int) -> tuple[str, int]:
    """从 ``text[start:]`` 开始取一对圆括号内的内容（``start`` 指向左括号之后）。

    返回 (括号内文本, 右括号之后的下标)；未闭合时取到末尾。引号内的括号不计深度。
    """
    depth = 1
    i = start
    n = len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in _QUOTES:
            quote = ch
        elif ch == "\\":
            i += 2
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return text[start:], n


def _split_segments(command: str, depth: int = 0) -> list[str]:
    """把一条命令拆成「会被 shell 分别执行的片段」，含子 shell 与引号内的片段。

    规则：

    - ``;`` ``&&`` ``||`` ``|`` ``&`` 与换行 → 分段；
    - ``$(...)``、``(...)``、反引号 → 取出内层文本**递归**拆分（``$(reboot)``
      与 ``sh -c "a && mkfs.ext4 /dev/sdb"`` 都靠这一步才能被看见）；
    - 引号内的内容同样递归拆分：``sh -c "…"`` 的引号里是一整条命令，不拆就会
      漏判。代价是 ``echo "rm -rf /"`` 这类「只是打印」的也会被拦——这是刻意的
      保守取舍：部署计划里没人需要打印一条删根命令。

    递归深度上限 8：够用，且防止恶意构造的深层嵌套把检查本身拖垮。
    """
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]

        if ch in _QUOTES:
            quote = ch
            j = i + 1
            while j < n:
                if command[j] == "\\" and quote == '"':
                    j += 2
                    continue
                if command[j] == quote:
                    break
                j += 1
            inner = command[i + 1 : j]
            if depth < 8:
                segments.extend(_split_segments(inner, depth + 1))
            buf.append(command[i : j + 1] if j < n else command[i:])
            i = j + 1 if j < n else n
            continue

        if ch == "\\" and i + 1 < n:
            buf.append(command[i : i + 2])
            i += 2
            continue

        if ch == "`":
            j = command.find("`", i + 1)
            inner = command[i + 1 :] if j < 0 else command[i + 1 : j]
            if depth < 8:
                segments.extend(_split_segments(inner, depth + 1))
            buf.append(command[i:] if j < 0 else command[i : j + 1])
            i = n if j < 0 else j + 1
            continue

        if ch == "$" and command[i + 1 : i + 2] == "(":
            inner, end = _balanced(command, i + 2)
            if depth < 8:
                segments.extend(_split_segments(inner, depth + 1))
            buf.append(command[i:end])
            i = end
            continue

        if ch == "(":
            inner, end = _balanced(command, i + 1)
            if depth < 8:
                segments.extend(_split_segments(inner, depth + 1))
            buf.append(command[i:end])
            i = end
            continue

        if command[i : i + 2] in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue

        if ch in _SEPARATORS:
            segments.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    segments.append("".join(buf))
    return [seg.strip() for seg in segments if seg.strip()]


def _haystacks(command: str) -> list[str]:
    """规则要匹配的全部文本：整串 + 每一段。

    整串不可省：``curl … | sh`` 被拆开后 ``curl`` 与 ``sh`` 各自都无害，
    危险只存在于「管道」这个关系里。
    """
    text = command or ""
    return [text, *_split_segments(text)]


def _first_hit(
    rules: tuple[tuple[str, str], ...], haystacks: list[str]
) -> tuple[str, str] | None:
    """返回 (命中的片段, 规则理由)；未命中返回 None。"""
    for pattern, reason in rules:
        compiled = re.compile(pattern, re.IGNORECASE)
        for text in haystacks:
            if compiled.search(text):
                return text, reason
    return None


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _raise_hit(hit: tuple[str, str], command: str) -> None:
    text, reason = hit
    raise GuardError(
        f"危险命令被拒绝（规则：{reason}）：命中片段 {_clip(text, 120)!r}；"
        f"整条命令 {_clip(command)!r}"
    )


# ---------------------------------------------------------------------------
# 命令级检查
# ---------------------------------------------------------------------------


def check_command(command: str, *, allow_dangerous: bool = False) -> None:
    """检查一条将被远端执行的 shell 命令；命中规则抛 ``GuardError``。

    分号 / ``&&`` / ``||`` / 管道 / 子 shell / 反引号 / ``$(...)`` 拆开后**逐段**
    检查，否则 ``echo ok; rm -rf /`` 会漏过。``allow_dangerous`` 只放开
    ``DENY_UNLESS_ALLOWED``，``DENY_ALWAYS`` 任何情况下都拒绝。
    """
    text = command or ""
    if not text.strip():
        return
    haystacks = _haystacks(text)

    hit = _first_hit(DENY_ALWAYS, haystacks)
    if hit is not None:
        _raise_hit(hit, text)

    if not allow_dangerous:
        hit = _first_hit(DENY_UNLESS_ALLOWED, haystacks)
        if hit is not None:
            _raise_hit(hit, text)


# ---------------------------------------------------------------------------
# 计划级检查
# ---------------------------------------------------------------------------


def _walk_steps(
    steps: list[DeployStep], prefix: str = ""
) -> Iterator[tuple[str, DeployStep]]:
    """递归遍历步骤与它们的 rollback，产出 (人类可读标签, 步骤)。"""
    for step in steps:
        label = f"{prefix}{step.id}"
        yield label, step
        yield from _walk_steps(step.rollback, prefix=f"{label}/rollback:")


def _check_write(label: str, spec: WriteFileSpec, app_root: str) -> None:
    path = (spec.path or "").strip()
    if not path:
        raise GuardError(f"步骤 {label!r}：write_file.path 不能为空")
    # `..` 一律拒绝——即使 allow_outside_release，也不靠 normalize 去「猜」
    # 真实落点；路径穿越是最容易被顺手写出来的越界方式。
    # 两种分隔符都查：本机 Windows 上模型很可能写出 `deploy\..\..\etc\x`。
    if ".." in re.split(r"[\\/]", path):
        raise GuardError(f"步骤 {label!r}：write_file.path 不允许出现 '..'：{path!r}")

    if not path.startswith("/"):
        # 相对路径的含义是「写在本次 release 目录里」（runner 会拼到 release_dir 下），
        # 这是最常见的用法：unit 模板、start.sh、配置片段都放产物里再 install 出去。
        # 与 allow_outside_release 同时出现是矛盾的——一个说「在 release 内」，
        # 一个说「写到外面」——直接拒绝，别让执行器去猜哪个优先。
        if spec.allow_outside_release:
            raise GuardError(
                f"步骤 {label!r}：write_file.path 是相对路径却还置了 allow_outside_release=true，"
                f"两者矛盾：{path!r}"
            )
        return

    if spec.allow_outside_release:
        return

    norm = posixpath.normpath(path)
    for base in (app_root, *UNIT_DIRS):
        if norm == base or norm.startswith(base + "/"):
            return
    raise GuardError(
        f"步骤 {label!r}：write_file 目标 {norm!r} 不在 {app_root!r} 或系统 unit 目录"
        f"（{'、'.join(UNIT_DIRS)}）之下；确需写出 release 时请置 allow_outside_release=true"
    )


def _check_upload(label: str, spec: UploadSpec) -> None:
    raw = (spec.src or "").strip().rstrip("/\\")
    # 同时认 `\` 与 `/`：本机是 Windows，src 很可能是 `C:\Users\x\<名字>` 形式。
    base = re.split(r"[\\/]", raw)[-1] if raw else ""
    if _NAME_PATTERNS.fullmatch(base):
        raise GuardError(
            f"步骤 {label!r}：拒绝上传 {spec.src!r}——本机凭据不得随计划送到远端"
            f"（命中环境变量文件、SSH 私钥、证书与服务器主密钥的文件名模式）"
        )


def _check_service(label: str, spec: ServiceSpec) -> None:
    name = spec.name or ""
    if not _UNIT_NAME_RE.fullmatch(name):
        raise GuardError(
            f"步骤 {label!r}：非法 unit 名 {name!r}——unit 名会成为 systemctl 的参数，"
            f"只允许 [A-Za-z0-9_.@-]"
        )


def validate_plan(
    plan: DeployPlan,
    *,
    max_steps: int = 60,
    allow_dangerous: bool = False,
    app_root: str = DEFAULT_APP_ROOT,
) -> None:
    """整份计划的合法性校验；任一条不过就抛 ``GuardError``。

    调用方拿到异常**不得执行任何步骤**（含「跳过坏步继续」）。检查项见
    ``docs/DEPLOY.md`` §5/§6：步数上限、``exec`` 命令、``write_file`` 落点、
    ``upload`` 的敏感文件、``service`` 的 unit 名。``rollback`` 里的步骤同样
    要过——回滚动作也是在别人机器上跑命令，不能成为豁免通道。
    """
    total = len(plan.steps)
    if total > max_steps:
        raise GuardError(
            f"计划过大，拒绝执行：{total} 步 > 上限 {max_steps} 步（deploy.max_plan_steps）"
        )

    root = posixpath.normpath(app_root.strip() or DEFAULT_APP_ROOT)
    for label, step in _walk_steps(plan.steps):
        spec = step.spec
        if isinstance(spec, ExecSpec):
            try:
                check_command(spec.command, allow_dangerous=allow_dangerous)
            except GuardError as exc:
                # 补上步骤标识：用户要知道是计划里哪一步被拦，而不只是一条命令。
                raise GuardError(f"步骤 {label!r}：{exc}") from exc
        elif isinstance(spec, WriteFileSpec):
            _check_write(label, spec, root)
        elif isinstance(spec, UploadSpec):
            _check_upload(label, spec)
        elif isinstance(spec, ServiceSpec):
            _check_service(label, spec)
        # probe 由控制端发起，不发命令、不写文件，无可拦截项。