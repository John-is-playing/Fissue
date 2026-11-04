"""全栈自检：一条命令说清「这台机器上的 Fissue 为什么跑不起来」。

为什么需要它
------------
``fissue platforms doctor`` 回答的是「**令牌与平台**这一层能做什么」：它默认要配置
能加载、要对着真实仓库发请求。可现实里「装不上、跑不起来」几乎都发生在**它之前**——
数据库连不上、LLM key 还是示例值、8000 端口被别的进程占着、Docker 装了却没启动、
数据目录不可写。这些问题的共同点是**报错离原因很远**：``Address already in use``
不会告诉用户该改哪个变量，``401`` 也不会说「你填的是文档里的示例值」。

本模块就是补上这一段：逐项探测，每项给 ``ok / warn / bad`` 与**一句能照着做的下一步**，
最后一行是 ``N 项通过 / M 项警告 / K 项失败``。

三条硬规矩
----------
* **不花额度**：模型那一项只做 DNS / TCP 连通判断，绝不发 chat 请求——体检不该产生账单。
* **不打印密钥**：需要展示时一律走 :func:`_mask_secret`（与 ``web/setup.py`` 同一规则）；
  驱动报错里回显的 DSN 也会被 :func:`_scrub` 换掉。
* **探测必须带超时**：DNS、TCP、``docker version``、socket 连接各有上限，超时就
  **跳过并明说跳过**（跳过 ≠ 通过，所以给 warn 而不是 ok）；任何一项内部异常都收敛成
  一条 bad，整体不许崩（见 :func:`_guarded`）。

这里只提供 :func:`collect_checks` 与 :func:`render` 两个纯函数 + 一个渲染入口，
命令注册（``--full`` 开关、``--json``）留在 ``cli/main.py``——它与 ``cli/doctor.py``
是同一套路数：子命令模块不自己建 Typer app。
"""

from __future__ import annotations

import contextlib
import locale
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from ..config import Settings
from .main import emit, print_table

_T = TypeVar("_T")

#: 状态等级，顺序即严重程度（``ok < warn < bad``）。多处结论合并时取最严重的那个。
OK, WARN, BAD = "ok", "warn", "bad"
_RANK = {OK: 0, WARN: 1, BAD: 2}
_MARK = {OK: "✅", WARN: "⚠️", BAD: "❌"}
_LABEL = {OK: "通过", WARN: "警告", BAD: "失败"}

#: 各项探测的超时（秒）。都是「超过就跳过并说出来」，不是「等到底」。
DNS_TIMEOUT_SECONDS = 2.0
TCP_TIMEOUT_SECONDS = 2.0
DOCKER_TIMEOUT_SECONDS = 3.0
SOCKET_TIMEOUT_SECONDS = 0.5
DATABASE_TIMEOUT_SECONDS = 15.0

#: 数据目录剩余空间低于这个值就给 warn（1 GiB）：沙盒镜像 + 导出很吃盘。
LOW_DISK_BYTES = 1024**3

#: 「填了等于没填」的示例值。只用来认占位符，不是密钥校验。
_PLACEHOLDER_RE = re.compile(
    r"(replace[-_ ]?me|your[-_ ]?(api[-_ ]?)?key|changeme|xxxx+|sk-x{3,}|<[^>]*>|\.\.\.$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CheckResult:
    """单项检查的结论。

    :param name: 检查项名字（中文，直接显示给用户）
    :param level: ``ok`` / ``warn`` / ``bad``；``warn`` 不挡流程，``bad`` 让退出码变 1
    :param detail: 看到了什么（路径、版本号、原始报错摘要）——**绝不放密钥**
    :param next_step: 用户下一步能照着做的一句话（具体的命令或变量名）
    """

    name: str
    level: str
    detail: str
    next_step: str


# ---------------------------------------------------------------------------
# 小工具：等级合并、脱敏、超时
# ---------------------------------------------------------------------------


def _worst(*levels: str) -> str:
    """取最严重的等级（``ok < warn < bad``）。"""
    return max(levels, key=lambda level: _RANK.get(level, 0), default=OK)


def _mask_secret(value: str | None) -> str:
    """密钥只回长度与首尾，够用户确认「填的是哪一把」即可。

    与 ``web/setup.py::_mask_secret`` 同一规则。这里刻意**复制**而不是 import：
    体检不该为了 6 行掩码把 FastAPI 那套拉进进程。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


def _is_placeholder(value: str) -> bool:
    """像示例值（``sk-REPLACE-ME`` / ``<你的key>``）而不是真密钥。"""
    text = (value or "").strip()
    return bool(text) and bool(_PLACEHOLDER_RE.search(text))


def _mask_dsn(url: str) -> str:
    """连接串脱敏：口令换成 ``***``，其余（主机/库名/文件路径）原样保留。"""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url or "")


def _password_of(url: str) -> str:
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).password or ""
    except Exception:
        return ""


def _scrub(text: str, url: str, masked: str) -> str:
    """把报错文本里的连接串与口令换成脱敏形式。

    驱动（psycopg / pymysql）的报错经常把整条 DSN 原样回显，而这条 detail 是要
    直接打到终端上给用户看的——不洗一遍，等于自检自己泄露口令。
    """
    out = text or ""
    if url:
        out = out.replace(url, masked)
    secret = _password_of(url)
    if secret:
        out = out.replace(secret, "***")
    return out


def _bounded(fn: Callable[[], _T], seconds: float, *, timeout_note: str) -> tuple[_T | None, str]:
    """在守护线程里跑 ``fn``，最多等 ``seconds`` 秒；超时与异常都收敛成一句文本。

    返回 ``(值, "")`` 或 ``(None, 原因)``。

    为什么用 ``threading.Thread(daemon=True)`` 而不是 ``ThreadPoolExecutor``：后者会在
    解释器退出时 join 自己的工作线程，而一个卡在 DNS 上的探测足以把整个进程（以及
    pytest）挂住——本仓库踩过「pytest 假死」的亏（见 ``pyproject.toml`` 的 timeout 说明）。
    守护线程不会拦着退出。
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # 探测里的任何异常都不许穿出去
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, name="fissue-fullcheck-probe", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return None, timeout_note
    if "error" in box:
        return None, str(box["error"])
    return box.get("value"), ""


def _tcp_probe(base_url: str) -> tuple[bool | None, str]:
    """只做 DNS 解析 + 一次 TCP 握手。``True`` 可达 / ``False`` 不可达 / ``None`` 没法判断。

    **绝不在这一层发 HTTP 请求**：判断「你机器能不能连上」不该消耗调用额度，也不该
    因为一次真实调用把自检变成账单；真要验 key 有效性请用 ``fissue eval --limit 1``。
    """
    text = (base_url or "").strip()
    if not text:
        return None, "base_url 为空"
    split = urlsplit(text if "//" in text else f"//{text}")
    host = split.hostname or ""
    scheme = (split.scheme or "").lower()
    port = split.port or (443 if scheme == "https" else 80)
    if not host:
        return None, "解析不出主机名"
    resolved, error = _bounded(
        lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        DNS_TIMEOUT_SECONDS,
        timeout_note=f"DNS 解析超过 {DNS_TIMEOUT_SECONDS:.0f}s（已跳过）",
    )
    if resolved is None:
        return False, f"解析不了 {host}：{error}"
    if not resolved:
        return False, f"{host} 没有可用的地址"

    def connect() -> bool:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT_SECONDS):
            return True

    reachable, error = _bounded(
        connect,
        TCP_TIMEOUT_SECONDS + 1.0,
        timeout_note=f"TCP {host}:{port} 超过 {TCP_TIMEOUT_SECONDS:.0f}s（已跳过）",
    )
    if reachable is True:
        return True, f"TCP {host}:{port} 可达（只握手，未发请求、不耗额度）"
    if reachable is None and "超过" in error:
        return None, error
    return False, f"连不上 {host}:{port}：{error}"


def _docker_probe() -> tuple[bool | None, str]:
    """``shutil.which`` + 一次 ``docker version``。``None`` = 超时，放弃判断。"""
    binary = shutil.which("docker")
    if binary is None:
        return False, "未找到 docker 命令"
    try:
        proc = subprocess.run(
            [binary, "version", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"`docker version` 超过 {DOCKER_TIMEOUT_SECONDS:.0f}s 没返回（已跳过）"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker 命令执行失败：{exc}"
    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout or "").strip().splitlines()
        lines = [line.strip() for line in raw if line.strip()]
        tail = lines[-1][:160] if lines else f"退出码 {proc.returncode}"
        return False, f"Docker 守护进程未运行：{tail}"
    versions = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not versions:
        # Windows 上引擎不可达时 docker 客户端会 **exit 0 但无输出**（见 sandbox/runtime.py）。
        return False, "Docker 守护进程不可达（exit 0 但无输出）"
    return True, f"Docker {versions[-1]}"


def _port_in_use(host: str, port: int) -> bool:
    """能不能在 ``host:port`` 上 bind——bind 得动就说明没人占着。

    用「试着 bind」而不是「试着 connect」：uvicorn 起服务走的就是 bind，结论与用户真正
    会撞上的那句 ``Address already in use`` 完全一致；connect 则漏掉「占着但没在 accept」
    这类形态。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def _probe_unix_socket(path: str) -> tuple[bool, str]:
    """转发组件 socket 能不能连上（只连不写）。"""
    if getattr(socket, "AF_UNIX", None) is None:
        return False, "本平台不支持 AF_UNIX"

    def connect() -> bool:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(SOCKET_TIMEOUT_SECONDS)
            sock.connect(path)
            return True
        finally:
            sock.close()

    ok, error = _bounded(
        connect,
        SOCKET_TIMEOUT_SECONDS + 1.0,
        timeout_note=f"连接超过 {SOCKET_TIMEOUT_SECONDS:.0f}s（已跳过）",
    )
    return ok is True, error


def _human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover - 循环里已经收口


# ---------------------------------------------------------------------------
# 七项检查
# ---------------------------------------------------------------------------


def _secret_fields(settings: Settings) -> list[tuple[str, str]]:
    """「填了就不该再是示例值」的密钥字段 → ``(环境变量名, 值)``。

    ``llm.api_key`` 不在这里：它由「模型」那一项单独判（没填是 warn、填了示例值才是
    bad），同一件事不该在两张表里各说一遍。
    """
    tokens = settings.tokens
    hooks = settings.webhooks.secrets
    return [
        ("FISSUE_GITHUB_TOKEN", tokens.github),
        ("FISSUE_GITEE_TOKEN", tokens.gitee),
        ("FISSUE_ATOMGIT_TOKEN", tokens.atomgit),
        ("FISSUE_GITLAB_TOKEN", tokens.gitlab),
        ("FISSUE_WEBHOOK_GITHUB_SECRET", hooks.github),
        ("FISSUE_WEBHOOK_GITEE_SECRET", hooks.gitee),
        ("FISSUE_WEBHOOK_ATOMGIT_SECRET", hooks.atomgit),
        ("FISSUE_WEBHOOK_GITLAB_SECRET", hooks.gitlab),
        ("FISSUE_EDGEJEV_API_KEY", settings.edgejev.api_key),
    ]


def _check_config(settings: Settings) -> CheckResult:
    """1. 配置加载：文件在哪、覆盖层有没有、密钥是不是还留着示例值。

    路径都从 ``Settings`` 读（``config_source`` / ``env_source`` / ``overlay_path``），
    这里只负责把它们说清楚。
    """
    notes: list[str] = []
    level = OK
    if settings.config_path:
        notes.append(f"config.yaml：{settings.config_path}")
    else:
        level = _worst(level, WARN)
        notes.append(f"没有 config.yaml（按 {settings.config_source or './config.yaml'} 找过，用内置默认值）")
    if settings.env_path:
        notes.append(f"dotenv 文件：{settings.env_path}")
    else:
        notes.append(f"没有找到 dotenv 文件（按 {settings.env_source} 找过，默认 ./.env）")
    if settings.overlay_path:
        notes.append(f"覆盖层：{settings.overlay_path}（WebUI 改过的值在这里，优先级高于 config.yaml）")
    else:
        notes.append("覆盖层 config.local.yaml 不存在（正常：只有 WebUI / onboard 改过配置才生成）")

    fake = [(name, value) for name, value in _secret_fields(settings) if _is_placeholder(value)]
    if fake:
        level = _worst(level, BAD)
        names = "、".join(sorted({name for name, _ in fake}))
        notes.append(f"占位符没换成真值：{names}")
        next_step = f"把这几项的示例值换成真实值：{names}（写法见 docs/ENV.md §6；键名两侧不能有空格）"
    elif settings.config_path:
        next_step = "配置能加载，无需处理"
    else:
        next_step = (
            "cp config.example.yaml config.yaml（或用 fissue onboard 生成一份），"
            "否则仓库列表/阈值/策略全是内置默认值"
        )
    return CheckResult("配置加载", level, "；".join(notes), next_step)


def _check_database(settings: Settings) -> tuple[CheckResult, Any]:
    """2. 数据库：能连上吗、schema 版本对得上吗、表齐不齐。

    返回 ``(结论, Database 或 None)``——Web 那一项要复用它查「有没有管理员账号」，
    不为了一条查询再连一次库。
    """
    from sqlalchemy import inspect, text

    from ..store.db import SCHEMA_VERSION, build_database

    dsn = settings.database.url
    masked = _mask_dsn(dsn)
    bad_step = (
        "检查 FISSUE_DATABASE_URL 与文件/目录权限：SQLite 要写成四个斜杠的绝对路径 "
        "sqlite:////绝对路径/文件.db；然后跑 `fissue db check` 看原始报错（docs/ENV.md §3.2）"
    )

    db, error = _bounded(
        lambda: build_database(settings),
        DATABASE_TIMEOUT_SECONDS,
        timeout_note=f"建数据库引擎超过 {DATABASE_TIMEOUT_SECONDS:.0f}s（已跳过）",
    )
    if db is None:
        return CheckResult("数据库", BAD, f"连不上 {masked}：{_scrub(error, dsn, masked)}", bad_step), None

    reachable, error = _bounded(
        db.ping,
        DATABASE_TIMEOUT_SECONDS,
        timeout_note=f"ping 超过 {DATABASE_TIMEOUT_SECONDS:.0f}s（已跳过）",
    )
    if not reachable:
        detail = f"连不上 {masked}：{_scrub(error or 'ping 失败', dsn, masked)}"
        return CheckResult("数据库", BAD, detail, bad_step), None

    notes = [f"{masked} 可连接"]
    level = OK
    next_step = "数据库正常，无需处理"
    try:
        names = set(inspect(db.engine).get_table_names())
    except Exception as exc:
        names = set()
        level = _worst(level, WARN)
        notes.append(f"读表清单失败：{exc}")

    missing = [name for name in ("items", "llm_usage", "auth_users") if name not in names]
    if missing:
        level = _worst(level, WARN)
        notes.append(f"缺表：{'、'.join(missing)}（共 {len(names)} 张）")
        next_step = "跑 `fissue db init` 建表（幂等，不会动已有数据）"
    elif names:
        notes.append(f"表齐全（{len(names)} 张）")

    if "fissue_meta" not in names:
        level = _worst(level, WARN)
        notes.append("没有 fissue_meta 表（这个库从没初始化过）")
        next_step = "跑 `fissue db init` 初始化 schema"
    else:
        version: int | None = None
        try:
            with db.engine.connect() as conn:
                row = conn.execute(text("SELECT v FROM fissue_meta WHERE k = 'schema_version'")).first()
            version = int(row[0]) if row is not None else 0
        except Exception as exc:
            level = _worst(level, WARN)
            notes.append(f"读 schema 版本失败：{exc}")
        if version is not None:
            notes.append(f"schema v{version}（程序 {SCHEMA_VERSION}）")
            if version < SCHEMA_VERSION:
                level = _worst(level, WARN)
                next_step = "库比程序旧：跑 `fissue db init`（或启动一次 fissue web）把补列迁移跑完"
            elif version > SCHEMA_VERSION:
                level = _worst(level, WARN)
                next_step = "库比程序新（多半刚降级过版本）：升级 fissue，或另指一个库档"
    return CheckResult("数据库", level, "；".join(notes), next_step), db


def _check_llm(settings: Settings) -> CheckResult:
    """3. 模型：key 配没配、是不是示例值、模型名填没填、端点通不通（只握手）。"""
    llm = settings.llm
    notes: list[str] = []
    level = OK
    next_step = "模型侧正常，无需处理"

    key = (llm.api_key or "").strip()
    if not key:
        level = _worst(level, WARN)
        notes.append("FISSUE_LLM_API_KEY 没配（fetch/status/report 不需要它；eval/verify/fix 必须要）")
        next_step = (
            "把 FISSUE_LLM_API_KEY 配上一个 sk- 开头的真实 key"
            "（见 docs/ENV.md §3.4、§6 模板）"
        )
    elif _is_placeholder(key):
        level = _worst(level, BAD)
        notes.append(f"FISSUE_LLM_API_KEY 还是示例值（{_mask_secret(key)}）")
        next_step = "把 FISSUE_LLM_API_KEY 换成真实 key——示例值会让每次 eval 都 401"
    else:
        notes.append(f"FISSUE_LLM_API_KEY 已配置（{_mask_secret(key)}）")

    model = (llm.model or "").strip()
    if model:
        notes.append("模型 " + model + (f"（回退 {llm.fallback_model}）" if llm.fallback_model else ""))
    else:
        level = _worst(level, BAD)
        notes.append("llm.model 为空")
        next_step = "在 config.yaml 的 llm.model 填模型名（如 deepseek-chat），或设 FISSUE_LLM_MODEL"

    base = (llm.base_url or "").strip()
    if not base:
        level = _worst(level, BAD)
        notes.append("llm.base_url 为空")
        next_step = (
            "在 config.yaml 的 llm.base_url 填端点（默认 https://api.deepseek.com/v1），"
            "或设 FISSUE_LLM_BASE_URL"
        )
    else:
        reachable, detail = _tcp_probe(base)
        notes.append(f"端点 {base} {detail}")
        if reachable is not True:
            level = _worst(level, WARN)
            next_step = (
                "核对 FISSUE_LLM_BASE_URL（默认 https://api.deepseek.com/v1）：公司代理/内网需设 "
                "HTTPS_PROXY 或 FISSUE_CA_BUNDLE（docs/ENV.md §3.4）；本项只做连通判断，没发请求"
            )
    return CheckResult("模型", level, "；".join(notes), next_step)


def _check_sandbox(settings: Settings) -> CheckResult:
    """4. 沙盒：runtime 是什么、docker 可不可用、转发组件 socket 通不通。"""
    sandbox = settings.sandbox
    notes: list[str] = [f"runtime={sandbox.runtime}"]
    level = OK
    steps: list[str] = []

    if not sandbox.enabled:
        level = _worst(level, WARN)
        notes.append("sandbox.enabled=false：验证与自动修复都不会跑")
        steps.append("要跑 verify/fix：把 config.yaml 的 sandbox.enabled 设回 true")
    elif sandbox.runtime == "local":
        level = _worst(level, WARN)
        notes.append("本机执行：没有容器隔离、不禁网，只适合本机开发")
        steps.append(
            "想真隔离：装 Docker 后把 sandbox.runtime 设成 docker（见 docs/SECURITY.md）；"
            "本机开发可保持 local"
        )
    else:
        notes.append(f"image={sandbox.image}")
        ready, detail = _docker_probe()
        notes.append(detail)
        if ready is not True:
            level = _worst(level, WARN)
            steps.append(
                "装/启动 Docker：Windows 用 Docker Desktop，Linux 装 docker-ce 后 `systemctl start docker`；"
                "确实不打算用容器就把 sandbox.runtime 设成 local，并显式打开 "
                "sandbox.allow_local_fallback（否则验证会失败）"
            )
        elif not sandbox.allow_local_fallback:
            notes.append("allow_local_fallback=false：Docker 不在就直说要装，不会偷偷降级")

    forwarder = sandbox.forwarder
    if not forwarder.enabled:
        notes.append("转发组件已关闭（inline 直调 Docker）")
    elif os.name == "nt":
        # forwarder 在 Windows 上只认命名管道，SandboxManager 也固定走 inline——这里别报警。
        notes.append("Windows 上固定 inline（转发组件只支持 POSIX socket / 命名管道）")
    elif not forwarder.socket:
        notes.append("没配转发组件 socket（inline 直调 Docker）")
    elif not Path(forwarder.socket).exists():
        # socket 不存在 = 自动 inline，是正常形态而不是故障（见 forwarder._decide_inline）。
        notes.append(f"转发组件 socket 不存在（{forwarder.socket}）→ 自动走 inline，正常")
    else:
        ok, error = _probe_unix_socket(forwarder.socket)
        if ok:
            notes.append(f"转发组件 socket 可达：{forwarder.socket}")
        else:
            level = _worst(level, WARN)
            notes.append(f"转发组件 socket 连不上：{forwarder.socket}（{error}）")
            steps.append("跑 `fissue sandbox serve` 起转发组件；不想用就把 sandbox.forwarder.socket 清空走 inline")

    return CheckResult("沙盒", level, "；".join(notes), "；".join(steps) if steps else "无需处理")


def _admin_count(db: Any) -> int | None:
    """库里已有几个账号；查不到（没库 / 没表 / 没权限）返回 ``None``——那是「不知道」，不是 0。"""
    if db is None:
        return None
    try:
        from ..auth.store import AuthStore

        return int(AuthStore(db).user_count())
    except Exception:
        return None


def _check_web(settings: Settings, *, host: str, db: Any) -> CheckResult:
    """5. Web：端口占没占、只读没只读、鉴权开了但还没管理员账号。

    ``host`` 是**试探端口**用的绑定地址（默认回环）：uvicorn 起服务绑的就是它，
    拿 ``web.host`` 去 bind（尤其 ``0.0.0.0``）会把「别的网卡上有东西」也算进来。
    """
    web = settings.web
    notes: list[str] = []
    level = OK
    steps: list[str] = []

    port = int(web.port or 0)
    if port:
        try:
            busy = _port_in_use(host, port)
        except Exception as exc:  # 端口非法 / 地址不可绑：说成 warn，别让整项变「检查本身出错」
            busy = False
            level = _worst(level, WARN)
            notes.append(f"没法在 {host}:{port} 上试探：{exc}")
        if busy:
            level = _worst(level, BAD)
            notes.append(f"{host}:{port} 已被占用（起页面会直接报 Address already in use）")
            steps.append(
                f"换端口或腾出 {port}：把 FISSUE_WEB_PORT 设成 8001（或改 config.yaml 的 web.port）；"
                f"或者找出占用者并停掉——Windows `netstat -ano | findstr :{port}` 再 "
                f"`taskkill /PID <pid> /F`，Linux/macOS `ss -ltnp | grep :{port}` 或 `lsof -i :{port}`"
            )
        else:
            notes.append(f"{host}:{port} 空闲")
    else:
        notes.append("web.port=0（随机端口，起服务后看日志）")

    web_host = (web.host or "").strip().lower()
    if web_host and web_host not in {"127.0.0.1", "localhost", "::1"} and not web_host.startswith("127."):
        notes.append(f"监听地址 {web.host}（本机之外可达）")

    if web.read_only:
        level = _worst(level, WARN)
        notes.append("web.read_only=true：页面只读，不会往平台打标签/提 PR")
        steps.append("要开自动修复的写操作：把 config.yaml 的 web.read_only 设成 false（或在设置页关掉只读）")
    else:
        notes.append("只读模式关闭（可写）")

    if not web.auth.enabled:
        level = _worst(level, WARN)
        notes.append("web.auth.enabled=false：会话鉴权关了，只靠回环地址 / 可信网段放行")
        steps.append("重新打开 web.auth.enabled（默认就是 true），或声明 web.trusted_networks 圈定内网网段")
    else:
        count = _admin_count(db)
        if count is None:
            notes.append("鉴权已启用；数据库不可用，查不了有没有管理员账号")
        elif count == 0:
            level = _worst(level, WARN)
            notes.append("鉴权已启用，但库里还没有管理员账号（第一次打开页面要走引导）")
            steps.append(
                "启动 `fissue web`，按终端打印的一次性引导口令完成初始化（docs/AUTH.md §3.4）；"
                "口令丢了就 `fissue auth reset`"
            )
        else:
            notes.append(f"鉴权已启用，管理员账号 {count} 个")

    return CheckResult("Web", level, "；".join(notes), "；".join(steps) if steps else "无需处理")


def _check_storage(settings: Settings) -> CheckResult:
    """6. 磁盘与数据目录：建得出来、写得进去、还剩多少。"""
    raw = settings.app.data_dir or "./data"
    path = Path(raw).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            "磁盘与数据目录",
            BAD,
            f"数据目录建不出来：{path}（{exc}）",
            "把 config.yaml 的 app.data_dir 指到一个可写目录（例如 ./data）；"
            "容器里要挂卷，别写在只读镜像层上",
        )

    probe = path / ".fissue-fullcheck.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            "磁盘与数据目录",
            BAD,
            f"数据目录不可写：{path}（{exc}）",
            "给这个目录写权限（Linux: chown/chmod 给当前用户；Windows: 换一个不在 Program Files 下的目录），"
            "或把 app.data_dir 指到可写位置",
        )
    else:
        with contextlib.suppress(OSError):  # 删不掉不影响结论
            probe.unlink()

    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        return CheckResult(
            "磁盘与数据目录",
            WARN,
            f"数据目录可写（{path}），但读不到剩余空间：{exc}",
            "忽略即可；要确认空间自己看 df -h（Linux）或资源管理器（Windows）",
        )
    notes = [f"数据目录可写：{path}", f"剩余 {_human_bytes(free)}"]
    if free < LOW_DISK_BYTES:
        return CheckResult(
            "磁盘与数据目录",
            WARN,
            "；".join(notes),
            "盘快满了：清掉 data/exports 与日志，或把 app.data_dir 换到更大的分区"
            "（沙盒镜像与导出都很吃盘）",
        )
    return CheckResult("磁盘与数据目录", OK, "；".join(notes), "无需处理")


def _stdout_encoding() -> str:
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if encoding:
            return str(encoding)
    try:  # pragma: no cover - 走不到（stdout 总有编码）
        return locale.getpreferredencoding(False) or ""
    except Exception:  # pragma: no cover
        return ""


def _check_locale() -> CheckResult:
    """7. 时区与语言环境：都不致命，但会让页面上的时间「差 8 小时」、中文变问号。"""
    notes: list[str] = []
    level = OK
    steps: list[str] = []

    tz_env = os.environ.get("TZ")
    # ``+ 0.0`` 只是为了把 ``-0.0`` 归一成 ``0.0``：UTC 机器上会显示成「UTC-0」。
    offset = -time.timezone / 3600.0 + 0.0
    tz_name = time.tzname[0] if time.tzname else "?"
    notes.append(f"时区 {tz_name}（UTC{offset:+g}）" + (f"，TZ={tz_env}" if tz_env else "，TZ 未设置"))
    if tz_env is None and abs(offset) < 1e-9:
        level = _worst(level, WARN)
        steps.append(
            "时间若显示成 UTC：先确认系统时区（Windows：设置 → 时间和语言 → 时区；"
            "Linux：`timedatectl set-timezone Asia/Shanghai`），必要时启动前把 TZ 设成 Asia/Shanghai"
        )

    encoding = _stdout_encoding()
    notes.append(f"控制台编码 {encoding or '未知'}")
    if encoding and "utf" not in encoding.lower():
        level = _worst(level, WARN)
        steps.append(
            f"中文可能乱码（当前 {encoding}）：Windows 先跑 `chcp 65001`，"
            "Linux/macOS 用 `export LANG=C.UTF-8`；也可以把 PYTHONIOENCODING 设成 utf-8"
        )

    next_step = "；".join(steps) if steps else "无需处理"
    return CheckResult("时区与语言环境", level, "；".join(notes), next_step)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _guarded(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """单项检查的兜底：探测代码自己炸了，也只是这一项 bad，整体照常出结论。"""
    try:
        return fn()
    except Exception as exc:
        return CheckResult(
            name,
            BAD,
            f"检查本身出错：{type(exc).__name__}: {exc}",
            "这一条是本命令的缺陷、不是你的配置问题：把上面这行（去掉任何密钥）报给维护者",
        )


def collect_checks(settings: Settings, *, host: str = "127.0.0.1") -> list[CheckResult]:
    """跑完七项探测，**保证返回七条**（顺序固定，便于对照输出与日志）。

    :param settings: 已加载的配置（本函数不再读文件、不再改配置）
    :param host: 试探 Web 端口的绑定地址，默认回环
    """
    results = [_guarded("配置加载", lambda: _check_config(settings))]

    # 数据库对象只建一次：Web 那一项要用它查管理员账号。
    holder: dict[str, Any] = {}

    def database_check() -> CheckResult:
        result, database = _check_database(settings)
        holder["db"] = database
        return result

    results.append(_guarded("数据库", database_check))
    results.append(_guarded("模型", lambda: _check_llm(settings)))
    results.append(_guarded("沙盒", lambda: _check_sandbox(settings)))
    results.append(_guarded("Web", lambda: _check_web(settings, host=host, db=holder.get("db"))))
    results.append(_guarded("磁盘与数据目录", lambda: _check_storage(settings)))
    results.append(_guarded("时区与语言环境", _check_locale))
    return results


def _ordered_steps(results: list[CheckResult]) -> list[str]:
    """按检查顺序去重收集 ``next_step``（同一句提示不必让人读两遍）。"""
    steps: list[str] = []
    for item in results:
        step = (item.next_step or "").strip()
        if step and step not in steps:
            steps.append(step)
    return steps


def render(settings: Settings, results: list[CheckResult]) -> int:
    """打印体检表与总结，返回退出码：有 bad → 1，否则 0（warn 不算失败）。

    为什么 warn 不计入退出码：``sandbox.runtime=local``、没配 LLM key 都是**合法**配置
    （``fetch`` / ``status`` / ``report`` 不需要它们）。把它们算成失败，等于让自检在
    一台配置良好的机器上恒红——下一个人只会学会无视退出码。
    """
    source = settings.config_source or settings.config_path or "./config.yaml"
    emit(f"Fissue 全栈自检 —— 配置来源：{source}")
    if not results:
        emit("（没有可显示的检查项）")
        return 0

    rows = [
        {
            "检查项": f"{_MARK.get(item.level, '?')} {item.name}",
            "状态": _LABEL.get(item.level, item.level),
            "详情": item.detail,
            "下一步": item.next_step or "—",
        }
        for item in results
    ]
    print_table(rows, ["检查项", "状态", "详情", "下一步"], title="全栈自检")

    passed = sum(1 for item in results if item.level == OK)
    warned = sum(1 for item in results if item.level == WARN)
    failed = [item for item in results if item.level == BAD]
    emit(f"\n{passed} 项通过 / {warned} 项警告 / {len(failed)} 项失败")
    if failed:
        emit("按顺序执行下面几步，再跑一次本检查：")
        for index, step in enumerate(_ordered_steps(failed), start=1):
            emit(f"  {index}. {step}")
        return 1
    if warned:
        emit("警告不挡流程，但上表「下一步」里那几条值得看一眼。")
    return 0
