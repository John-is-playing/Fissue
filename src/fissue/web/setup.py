"""配置写入内核：初始化向导 / 仓库管理 / 设置页共用。

安全边界（先读这段）
====================
这个模块能改**本机的配置文件与 .env**（含平台 token、LLM Key、访问口令）——比
``/actions/*`` 那种「去平台上打个标签」敏感得多，所以单独一道闸门 :func:`setup_guard`：

======================  ==========================================================
请求来源                 结果
======================  ==========================================================
本机回环                 放行。默认装机就是这种：小白打开 localhost 就能初始化
带反向代理转发头          拒绝。同机 nginx 一转，控制面就对外了（同 app.py 的判断）
已开鉴权且带着正确口令     放行。前端鉴权已经挡过一道，这里再确认一次
非回环且未开鉴权          拒绝——除非显式打开 ``web.allow_remote_config_write``
======================  ==========================================================

写什么、写到哪
==============
* **密钥与环境项** → ``.env``（逐行改写，注释与顺序原样保留，见 ``configio``）；
* **业务配置**（仓库列表等）→ 覆盖层 ``config.local.yaml``；
* ``config.yaml``（人写的那份）**永不触碰**。

**白名单**：界面能写的环境变量必须在 :data:`ENV_WRITABLE` 里登记，能写的覆盖层路径必须在
:data:`OVERLAY_WRITABLE` 里登记，没登记的一律拒绝——否则「设置页」就等于一个任意文件写入器。

改完之后为什么必须热重载
========================
进程内存里那份 ``Settings`` 是启动时读进来的。写完文件不重载，界面上立刻显示旧值、用户会以为
「保存没生效」；重载后 ``app.state`` 整份换新（含数据库引擎），请求路径无需重启即可用上新配置。
**例外**：``web.host`` / ``web.port`` 是 uvicorn 已经绑好的 socket，改它必须重启进程——接口会
如实回一条 :data:`RESTART_REQUIRED` 说明，而不是假装生效。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..config import (
    DEFAULT_ENV_FILE,
    RepoConfig,
    Settings,
    _read_env_file,
    load_settings,
)
from ..configio import (
    config_sources,
    read_yaml_mapping,
    repo_entry,
    seed_repos,
    update_env_file,
    update_overlay,
)
from ..logging_setup import get_logger
from ..models import Platform
from .status import mask_url

log = get_logger(__name__)

#: 允许通过界面写进 ``.env`` 的键 → 界面上的说明文字。**白名单**，不是「什么都能写」。
#:
#: 这里曾有 ``FISSUE_API_TOKEN``（单一访问口令）。它已被会话 + API Key 整体替换，
#: 所以从这个白名单里去掉了：登录口令**不经通用配置写入器**，它只以 PBKDF2 哈希
#: 存在 ``auth_users`` 表里。留一个「往 .env 写访问口令」的入口，等于给「口令是配置项」
#: 这个错觉续命。
ENV_WRITABLE: dict[str, str] = {
    "FISSUE_LLM_API_KEY": "LLM API Key",
    "FISSUE_LLM_BASE_URL": "LLM 端点（OpenAI 兼容）",
    "FISSUE_LLM_MODEL": "主模型",
    "FISSUE_LLM_FALLBACK_MODEL": "备用模型（主模型失败时降级，可留空）",
    "FISSUE_GITHUB_TOKEN": "GitHub Token",
    "FISSUE_GITEE_TOKEN": "Gitee Token",
    "FISSUE_ATOMGIT_TOKEN": "AtomGit Token",
    "FISSUE_GITLAB_TOKEN": "GitLab Token",
    "FISSUE_DATABASE_URL": "数据库连接串",
    "FISSUE_WEB_HOST": "监听地址",
    "FISSUE_WEB_PORT": "监听端口",
}

#: 属于密钥的键：读取时只回「有没有配」与掩码，**绝不回原值**。
#: 页面把 token 打回浏览器，等于把它写进了浏览器历史、代理日志和截图。
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "FISSUE_LLM_API_KEY",
        "FISSUE_GITHUB_TOKEN",
        "FISSUE_GITEE_TOKEN",
        "FISSUE_ATOMGIT_TOKEN",
        "FISSUE_GITLAB_TOKEN",
        # 连接串也算密钥：`postgresql+psycopg://user:pw@host/db` 的 pw 明文在里面。
        # 同一批代码在 status.py 里专门写了 `mask_url` 去遮它，而设置页曾经把它**原样回显**
        # 给浏览器（含口令）——等于一边遮一边漏。
        "FISSUE_DATABASE_URL",
    }
)

#: 覆盖层里允许写的顶层路径（点分）。``repos`` 由 :func:`save_repos` 专门负责，
#: 它带种子导入，不能走通用的深合并。
#:
#: ``web.auth.*`` 与 ``web.trusted_networks`` 是鉴权那次新增的（docs/AUTH.md §6）：
#: 鉴权开关、会话时长、安全 cookie、失败次数与可信网段**都要能在界面上改**。
#:
#: ``llm.*`` 这一组是**计费与预算**（以及影响花费的生成参数）：价格填错会让
#: ``hard_stop`` 提前掐断或形同虚设，而这些数只有用户知道（跟着模型/渠道走），原先却只能
#: 改 config.yaml。它们的边界由 pydantic 的约束兜（见 ``BudgetConfig`` / ``PricingConfig``），
#: 写盘前的 ``_validate_candidate`` 会先跑一遍。
OVERLAY_WRITABLE: frozenset[str] = frozenset(
    {
        "repos",
        "web.allow_remote_config_write",
        "web.auth.enabled",
        "web.auth.session_ttl_minutes",
        "web.auth.secure_cookie",
        "web.auth.login_max_failures",
        "web.trusted_networks",
        # -- LLM 计费与预算 --
        "llm.pricing.input_per_1k",
        "llm.pricing.output_per_1k",
        "llm.budget.daily_tokens_per_repo",
        "llm.budget.daily_usd_per_repo",
        "llm.budget.hard_stop",
        # -- 影响花费的生成参数 --
        "llm.temperature",
        "llm.max_tokens",
        "llm.max_tokens_ceiling",
        "llm.timeout_seconds",
        "llm.max_retries",
        "llm.concurrency",
    }
)

#: 改了必须重启进程才生效的键（uvicorn 已经绑好 socket 了）。
RESTART_REQUIRED: frozenset[str] = frozenset({"FISSUE_WEB_HOST", "FISSUE_WEB_PORT"})

#: 「请求来自反向代理」的判据。**必须与 `app.py` 里那份一致**——两处判据不同，会出现
#: 「一边认为是从代理来的、另一边不认为」，于是同一件事在两处得到相反结论。
#: 原先这里漏了 `forwarded`（RFC 7239 的标准写法），而 app.py 有它。
FORWARD_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
)


# ---------------------------------------------------------------------------
# 安全边界
# ---------------------------------------------------------------------------


def _peer_is_loopback(request: Request) -> bool:
    """来源是不是本机回环。拿不到可解析地址（测试客户端 / UDS）时算 True。

    「不知道对端是谁」不等于「可疑」——与 app.py 的 ``_reject_remote_write`` 同一口径，
    否则本机正常用法会被一起挡掉。
    """
    client = request.client
    host = (client.host if client else "") or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def setup_guard(request: Request, settings: Settings) -> None:
    """配置写入的边界。不通过就 403（并把「怎么才能通过」写清楚）。

    判据按 docs/AUTH.md §9 更新为「身份」而不是「有没有设过口令」：

    * 来源在回环/可信网段内 → 放行（本机与内网自用，这是主要用法）；
    * 已通过会话或 API Key 认证 → 放行（`Depends(auth)` 已核对过，这里不重复）；
    * 其余 → 拒绝，除非显式打开 ``web.allow_remote_config_write`` 那个逃生开关。

    转发头的补刀保留：挂了反向代理时，请求在 HTTP 层与「本机直连」无法区分，
    见到转发头就拒，把「静默开放」变成「响亮拒绝」。**这是补刀，不是安全边界**——
    真正的边界是身份本身。
    """
    from .auth import forwarded_without_declaration, resolve_identity

    identity = resolve_identity(request)
    hit = forwarded_without_declaration(request, settings, identity)
    if hit:
        raise HTTPException(
            status_code=403,
            detail=(
                f"拒绝改写配置：请求带有反向代理转发头（{'、'.join(hit)}），"
                "但既没有通过认证，也没有声明可信网段。"
                "在反向代理后面暴露这个控制面等于把「改密钥」的权限一并开放——"
                "请在 /login 登录（或用 API Key），"
                "若确实挂在反向代理后面，请把代理/内网网段写进 web.trusted_networks。"
            ),
        )
    if identity.trusted_source or identity.authenticated:
        return
    if settings.web.allow_remote_config_write:
        log.warning(
            "允许未认证的非本机来源改写配置（web.allow_remote_config_write=true），来源：%s",
            request.client.host if request.client else "未知",
        )
        return
    peer = request.client.host if request.client else "未知"
    hint = (
        "请在该机器上访问 /login 完成初始化（尚无管理员账号）。"
        if settings.web.auth.enabled
        else "当前 web.auth.enabled=false：请改为 true，或把来源网段写进 web.trusted_networks。"
    )
    raise HTTPException(
        status_code=403,
        detail=(
            f"拒绝改写配置：请求来自 {peer}，既不是本机回环/可信网段，也没有通过认证。"
            f"{hint} 若确定这台机器只有可信网络可达，也可在设置页打开"
            "「强制允许非本机来源改写配置」（web.allow_remote_config_write）。"
        ),
    )


# ---------------------------------------------------------------------------
# 热重载
# ---------------------------------------------------------------------------


def reload_state(app: FastAPI) -> Settings:
    """按**当前配置文件**重建 ``app.state``，让改动立刻生效（不用重启）。

    换掉的不只是 ``settings``：数据库连接串可能也改了，所以引擎/仓储一起重建，旧引擎
    ``dispose()`` 掉，别把连接池留在那儿。

    在跑的作业不受影响：它启动时就 `_ctx_of(app)` 拿到了自己的 context（含旧引擎），
    照旧跑完；新来的请求走新引擎。这是有意的——中途把正在写库的作业的引擎抽掉才是灾难。
    """
    from ..store.db import build_database
    from ..store.repository import Repository

    old = getattr(app.state, "settings", None)
    # 用**同一组输入路径**重新加载。必须用 config_source / env_source（解析结果，文件不存在时
    # 也有值），而不是 config_path / env_path（文件不存在时是 None）——拿 None 再走一遍
    # load_settings 会回落到 cwd 下的默认 config.yaml / .env，于是「保存一次仓库配置」就悄悄
    # 换了数据库与配置来源（实测过：/tmp/t.db → ./fissue.db）。
    settings = load_settings(
        getattr(old, "config_source", None) or getattr(old, "config_path", None),
        getattr(old, "env_source", None) or getattr(old, "env_path", None),
    )
    database = build_database(settings)
    database.create_all()
    previous_db = getattr(app.state, "db", None)
    app.state.settings = settings
    app.state.db = database
    app.state.repo = Repository(database)
    # 作业表里也留着一份 settings，而它正是作业抢锁的依据（`single_flight` 按 database.url
    # 定位锁文件）。不同步的话，在设置页改掉 DATABASE_URL 之后，新提交的作业仍去抢**旧库**的
    # flock——两处写操作于是能同时进行：重复调 LLM、重复往平台打标签。
    jobs = getattr(app.state, "jobs", None)
    if jobs is not None:
        jobs.settings = settings
    if previous_db is not None and previous_db is not database:
        with contextlib.suppress(Exception):  # 旧引擎关不掉不该让保存失败
            previous_db.engine.dispose()
    log.info("配置已重载：%s", settings.config_path or "（内置默认值）")
    return settings


def _reload_or_report(app: FastAPI) -> Settings:
    """热重载；失败时给一条**可照做**的报错，而不是把裸异常丢出去变成 500。

    为什么单独包一层：走到这一步时**文件已经写下去了**——校验只能保证「这个值解析得通」，
    保证不了「那台数据库此刻连得上」。报错必须让用户知道两件事：已经写盘了、下一步敲什么。
    只丢一个 500 的话，他既不知道写没写、也不知道怎么复核；而**重载不出来就没有界面**，
    等于把人直接逼回命令行（真机实测踩到过，见 `DatabaseConfig._check_url` 的说明）。
    """
    try:
        return reload_state(app)
    except Exception as exc:
        log.exception("配置已写盘，但热重载失败")
        raise HTTPException(
            status_code=500,
            detail=(
                f"配置已写盘，但热重载失败：{type(exc).__name__}: {exc}\n"
                "请用 `fissue db check --config <config.yaml> --env <.env>` 复核数据库连接串；"
                "若连接串写错了，直接编辑 .env 里的 FISSUE_DATABASE_URL"
                "（此刻界面可能已经打不开——重载不出来就没有界面，只能从命令行自救）。"
            ),
        ) from exc


# ---------------------------------------------------------------------------
# 写 .env / 覆盖层
# ---------------------------------------------------------------------------


def _validate_candidate(
    settings: Settings, env_updates: dict[str, str], overlay_updates: dict[str, Any]
) -> None:
    """在落盘**之前**校验「改完之后的配置」能不能加载。

    为什么必须前置：原先的顺序是「先写 .env / 覆盖层 → 再热重载」。于是把
    `FISSUE_WEB_PORT=8e3`、或一个写错的 PG 地址写进 .env 时，**文件已经坏了**——而重启会
    直接起不来，用户连界面都没有（服务起不来就没有界面），从界面上**无法自救**。
    这里把「改完之后」的配置先在内存里构造一遍：不合法就 400，文件一字不动。
    """
    from ..config import _apply_env_overrides, _deep_merge

    try:
        merged: dict[str, Any] = {}
        if settings.config_source:
            merged = read_yaml_mapping(settings.config_source)
        merged = _deep_merge(merged, read_yaml_mapping(overlay_file(settings)))
        if overlay_updates:
            merged = _deep_merge(merged, dict(overlay_updates))

        target_env = env_file(settings)
        env_values = _read_env_file(target_env) if target_env.exists() else {}
        env_values.update({k: str(v) for k, v in env_updates.items()})
        merged = _apply_env_overrides(merged, env_values)

        Settings(**merged)
    except Exception as exc:
        # **整段都包进来**：非法值最早是在「环境变量覆盖」那一步被拒的（那里按字段类型解析，
        # 例如 `FISSUE_WEB_PORT=8e3` 直接抛 ConfigError）。只包住 `Settings(...)` 的话，
        # 它会以 500 冒出去——而用户要的是「这条值不合法、文件没动」。
        raise HTTPException(
            status_code=400,
            detail=f"配置校验失败，未写入任何文件：{exc}",
        ) from exc


def apply_env(env_path: str | Path | None, values: dict[str, str]) -> list[str]:
    """把白名单内的键写进 .env，返回**实际写了**的键名。

    未登记的键直接 400：不这样，这个接口就是一个「往任意路径写任意内容」的洞。
    """
    if not values:
        return []
    unknown = sorted(set(values) - set(ENV_WRITABLE))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不允许改写这些环境变量：{'、'.join(unknown)}；"
                f"可改的有：{'、'.join(sorted(ENV_WRITABLE))}"
            ),
        )
    path = Path(env_path) if env_path else Path(DEFAULT_ENV_FILE)
    update_env_file(path, {k: str(v) for k, v in values.items()})
    log.info("已写入 .env：%s（%s）", path, "、".join(sorted(values)))
    return sorted(values)


def _flatten_paths(updates: dict[str, Any], prefix: str = "") -> set[str]:
    """把待写字典摊成**叶子路径**的集合，用于和 :data:`OVERLAY_WRITABLE` 比对。

    必须**递归**：``{"web": {"auth": {"enabled": true}}}`` 的叶子是 ``web.auth.enabled``。
    原先只摊一层，于是 `web.auth` 这个中间节点被当成待写路径去比对白名单，直接 400
    「不允许改写这些配置项：web.auth」——而白名单里写的明明是那四个叶子。
    """
    flat: set[str] = set()
    for key, value in updates.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            flat.update(_flatten_paths(value, path))
        else:
            flat.add(path)
    return flat


def apply_overlay(
    overlay_path: str | Path,
    settings: Settings,
    updates: dict[str, Any],
    *,
    seed_repo_list: bool = True,
) -> list[str]:
    """把白名单内的路径写进覆盖层，返回写了哪些路径。

    ``seed_repo_list``：要动 ``repos`` 时**先做种子导入**（把当前生效的仓库列表搬进覆盖层）。
    没有这一步，覆盖层里只写新加的那一条会把 config.yaml 里原有的整张列表替掉——静默丢失，
    详见 ``configio.seed_repos``。
    """
    unknown = sorted(p for p in _flatten_paths(updates) if p not in OVERLAY_WRITABLE)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不允许改写这些配置项：{'、'.join(unknown)}；"
                f"可改的有：{'、'.join(sorted(OVERLAY_WRITABLE))}"
            ),
        )
    seed = seed_repos(settings.repos) if (seed_repo_list and "repos" in updates) else None
    update_overlay(overlay_path, updates, seed=seed)
    return sorted(_flatten_paths(updates))


def save_repos(overlay_path: str | Path, settings: Settings, repos: list[RepoConfig]) -> None:
    """把**完整**的仓库列表写进覆盖层（种子导入 + 整体替换）。

    覆盖层一旦有 ``repos``，它就是这个列表的唯一来源（合并时列表整体替换），所以每次都必须写
    完整的一份；只写改动的那一条就会把其余的全删掉。
    """
    update_overlay(overlay_path, {"repos": [repo_entry(r) for r in repos]}, seed=seed_repos(settings.repos))


def env_file(settings: Settings) -> Path:
    """要写入的 .env 路径。

    **必须优先用 `env_source`（解析结果，文件不存在时也有值）**，而不是 `env_path`
    （文件不存在时是 None）。拿 None 去 `Path(DEFAULT_ENV_FILE)` 会落到 **cwd 下的 .env**——
    实测过一次真实事故：用一份临时的 `--env <tmp>/missing.env` 起服务，向导写配置时把
    `FISSUE_LLM_API_KEY` 写进了项目根目录那份**真实的 .env**，覆盖了里面原有的密钥。
    「往我没打算写的那份文件里写密钥」是最不该发生的一类错误。
    """
    for candidate in (settings.env_source, settings.env_path):
        if candidate:
            return Path(candidate)
    return Path(DEFAULT_ENV_FILE)


def overlay_file(settings: Settings) -> Path:
    """覆盖层路径：优先用配置里解析出来的那份（``FISSUE_CONFIG_LOCAL`` 可能改过它）。

    与 :func:`env_file` 同理：用 ``config_source``（解析结果）而不是 ``config_path``
    （config.yaml 不存在时是 None，会回落成 cwd 下的 ``config.local.yaml``）。
    """
    if settings.overlay_path:
        return Path(settings.overlay_path)
    from ..config import overlay_path_for

    return overlay_path_for(settings.config_source or settings.config_path)


# ---------------------------------------------------------------------------
# 描述当前配置（设置页用）
# ---------------------------------------------------------------------------


def _mask_secret(value: str | None) -> str:
    """密钥只回长度与首尾，够用户确认「填的是哪一把」即可。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


def describe_settings(settings: Settings) -> dict[str, Any]:
    """设置页要显示的：每个可写项的值（密钥掩码）、来源、是否需重启。"""
    import os

    sources = config_sources(
        config_path=settings.config_path,
        overlay_path=settings.overlay_path,
        env_path=settings.env_path,
    )
    # 读的是**将要写入的那份** .env（env_file），否则「页面显示未配置、其实是写到了别处」
    # 这种自相矛盾的状态就会一直存在——真实事故里正是这个现象暴露了写入路径错了。
    target_env = env_file(settings)
    env_file_values = _read_env_file(target_env) if target_env.exists() else {}

    items: list[dict[str, Any]] = []
    for key, label in ENV_WRITABLE.items():
        # 生效值优先看**进程环境**（它能盖住 .env），其次 .env 文件。
        effective = os.environ.get(key) or env_file_values.get(key) or ""
        items.append(
            {
                "key": key,
                "label": label,
                "secret": key in SECRET_KEYS,
                "set": bool(effective),
                "value": _mask_secret(effective) if key in SECRET_KEYS else effective,
                "restart_required": key in RESTART_REQUIRED,
            }
        )

    return {
        # 键名别叫 `items`：模板里 `view.items` 会解析成 **dict 的方法** `items` 而报
        # 「not iterable」，得写 `view['items']` 才行——这种坑不值得留给下一个人。
        "env_items": items,
        "paths": {
            "config": settings.config_path,
            "overlay": settings.overlay_path,
            "overlay_expected": str(overlay_file(settings)),
            "env": settings.env_path,
        },
        "effective": {
            "llm_model": settings.llm.model,
            "llm_base_url": settings.llm.base_url,
            "llm_api_key_set": bool(settings.llm.api_key),
            # 脱敏后再给页面：这一行（「当前生效」）原先直接把 DSN 原样铺出来，含口令。
            "database": mask_url(str(settings.database.url)),
            "web_host": settings.web.host,
            "web_port": settings.web.port,
            "read_only": settings.web.read_only,
            "allow_remote_config_write": settings.web.allow_remote_config_write,
            # 鉴权（docs/AUTH.md §6）：全部经覆盖层可写。
            "auth_enabled": settings.web.auth.enabled,
            "session_ttl_minutes": settings.web.auth.session_ttl_minutes,
            "secure_cookie": settings.web.auth.secure_cookie,
            "login_max_failures": settings.web.auth.login_max_failures,
            # 覆盖层里没写就显示空列表（回落到 config.yaml / 默认值的那份仍由
            # settings.web.trusted_networks 给出，页面上的输入框回填的是**生效值**）。
            "trusted_networks": list(settings.web.trusted_networks),
            "trusted_networks_text": "\n".join(settings.web.trusted_networks),
            "repos": [r.slug for r in settings.repos],
            # LLM 计费 / 预算 / 生成参数的**生效值**：页面上的输入框按它回填。
            # 为什么必须回填生效值而不是覆盖层里的值：这三处可能来自 config.yaml 或默认值，
            # 只显示覆盖层那份会让用户以为「我填的数没生效」——真正生效的是合并后的这一份。
            "llm_tunables": {
                "pricing_input_per_1k": settings.llm.pricing.input_per_1k,
                "pricing_output_per_1k": settings.llm.pricing.output_per_1k,
                "budget_tokens": settings.llm.budget.daily_tokens_per_repo,
                "budget_usd": settings.llm.budget.daily_usd_per_repo,
                "budget_hard_stop": settings.llm.budget.hard_stop,
                "temperature": settings.llm.temperature,
                "max_tokens": settings.llm.max_tokens,
                "max_tokens_ceiling": settings.llm.max_tokens_ceiling,
                "timeout_seconds": settings.llm.timeout_seconds,
                "max_retries": settings.llm.max_retries,
                "concurrency": settings.llm.concurrency,
            },
        },
        "sources": sources,
        "overlay_content": read_yaml_mapping(settings.overlay_path) if settings.overlay_path else {},
    }


# ---------------------------------------------------------------------------
# 探活
# ---------------------------------------------------------------------------

#: 探活的上限（秒）。平台请求默认 30s 超时 + 4 次重试，最坏两分钟——而探活是**用户在页面上
#: 按了按钮正等着**。实测过一次：本机到 github.com 被中间设备阻断，`探测` 按钮转了两分钟才
#: 报错，用户只会以为页面死了。这里统一封顶，超时就明确说「超时」。
PROBE_TIMEOUT_SECONDS = 20.0


async def _bounded(coro: Any, *, what: str) -> Any:
    """给探活套一个超时上限（不重试由调用方通过 ``max_retries=0`` 保证）。"""
    try:
        return await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{what}超时（>{PROBE_TIMEOUT_SECONDS:.0f}s）") from exc


def _probe_settings(settings: Settings) -> Settings:
    """探活专用的一份配置：**只尝试一次，不重试**。

    ``max_retries`` 在平台适配器里同样是**总尝试次数**（见
    ``PlatformAdapter._request`` 的 ``range(1, max_retries + 1)`` 与
    ``attempt >= max_retries`` 判定）。所以这里必须填 **1**，不能填 0：

    * 填 0 → 循环全空、**一次请求都不发**，然后抛「请求最终失败：（None）」——
      ``last_exc`` 是空的，报错把真正的原因（连不上 / 401 / 404）全丢了。
      实测就踩过：GitHub 的「校验」按钮一直报 `请求最终失败：GET https://api.github.com/user（None）`，
      而同一把 token 直连是 200。
    * 填 1 → 尝试一次、失败立刻返回真实原因，这才是「探活要快」的本意。

    重试是给流水线用的（一次网络抖动不该让整批失败），探活要的是「快点给个答案」。
    """
    cand = settings.model_copy(deep=True)
    cand.platforms.max_retries = 1
    return cand


async def probe_llm(
    *,
    base_url: str,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    """真的打一次 LLM：端点 / Key / 模型三样有一样不对，这里就会失败。

    三个刻意的选择，都来自踩过的坑：

    * **只测被测的那一个模型，不降级**。带上备用模型时，主模型失败会静默降级到备用，最后那条
      报错指向的是**备用模型**——用户看到「模型调用失败（qwen3.6-plus）」会以为自己的配置被
      环境变量覆盖了（真实发生过）。连通性测试要回答的就是「这个端点 + 这个 key + 这个模型行不行」。
    * **``max_retries=1``，不是 0**。这个数是**总尝试次数**，0 会让循环全空、一次请求都不发出，
      然后报「重试耗尽」——探活于是永远失败，连 401 都问不出来。
    * **超时 30 秒**：探活是「用户按了按钮在等」，不该陪着退避策略耗两分钟。
    """
    from ..ai.client import LLMClient
    from ..config import LLMConfig

    started = datetime.now(timezone.utc)
    cfg = LLMConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        fallback_model=None,
        timeout_seconds=30,
        max_retries=1,
    )
    client = LLMClient(cfg)
    try:
        data, usage = await client.chat_json(
            [
                {"role": "system", "content": "你是连通性探针，只回 JSON。"},
                {"role": "user", "content": '回复 {"ok": true} 这个 JSON，不要别的内容。'},
            ],
            purpose="probe:llm",
        )
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return {
            "ok": True,
            "model": model,
            "seconds": round(elapsed, 2),
            "tokens": {
                "prompt": usage.prompt_tokens,
                "completion": usage.completion_tokens,
            },
            "reply_keys": sorted(data)[:8] if isinstance(data, dict) else str(type(data).__name__),
        }
    except Exception as exc:  # noqa: BLE001 - 探活失败的原因就是要展示给用户的东西
        return {
            "ok": False,
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        }
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def probe_database(url: str) -> dict[str, Any]:
    """连一次库并建表。

    **异步执行**：连接是一种不可控的等待（不可路由的地址会一直等到超时）。原先是同步实现、
    却在 `async def` 端点里直接调用，于是「测试连接」会把整个事件循环按住——同一时刻的
    状态轮询、别的请求全都停住。放到线程里跑就只是这一个请求在等。
    """
    return await asyncio.to_thread(_probe_database_sync, url)


def _probe_database_sync(url: str) -> dict[str, Any]:
    """`probe_database` 的同步实体（放在线程里跑）。"""
    from ..config import DatabaseConfig
    from ..store.db import build_database_from_config

    started = datetime.now(timezone.utc)
    try:
        cfg = DatabaseConfig(url=url)
        database = build_database_from_config(cfg)
        try:
            database.create_all()
            ok = database.ping()
        finally:
            with contextlib.suppress(Exception):
                database.engine.dispose()
        return {
            "ok": bool(ok),
            "backend": "sqlite" if url.startswith("sqlite") else "postgresql",
            "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        }


async def probe_platform(settings: Settings, platform: Platform, token: str) -> dict[str, Any]:
    """用给定 token 问一次「我是谁」——平台会拒掉无效/越权的令牌。

    临时造一份 settings（把 token 换掉）再建适配器：写盘与探活分离，**探活不改任何文件**，
    用户可以在保存之前反复试。
    """
    from ..platforms.registry import build_adapter

    started = datetime.now(timezone.utc)
    cand = _probe_settings(settings)
    setattr(cand.tokens, platform.value, token)
    try:
        adapter = build_adapter(cand, platform=platform)
        try:
            who = await _bounded(adapter.whoami(), what=f"向 {platform.value} 校验 Token")
        finally:
            with contextlib.suppress(Exception):
                await adapter.aclose()
        return {
            "ok": True,
            "platform": platform.value,
            "login": who,
            "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "platform": platform.value,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        }


async def probe_repo(
    settings: Settings, platform: Platform, owner: str, name: str
) -> dict[str, Any]:
    """探测一个仓库：它存不存在、默认分支是什么。

    **刻意不猜测试命令**：那需要挨个平台实现「列根目录文件」的 API（四个适配器各写一份），
    而猜错测试命令的代价是自动修复白跑一轮、回归门给出假结论。所以 `test_hint` 只在
    「添加仓库」表单里给一个可编辑的默认值，由人确认——不假装探测过。
    """
    from ..models import RepoRef
    from ..platforms.registry import build_adapter

    started = datetime.now(timezone.utc)
    cand = _probe_settings(settings)
    cand.repos = [
        *settings.repos,
        RepoConfig(platform=platform, owner=owner, name=name),
    ]
    ref = RepoRef(platform=platform, owner=owner, name=name)
    out: dict[str, Any] = {"ok": False, "platform": platform.value, "slug": f"{owner}/{name}"}
    try:
        adapter = build_adapter(cand, platform=platform)
        try:
            out["default_branch"] = await _bounded(
                adapter.get_default_branch(ref), what=f"探测 {owner}/{name} "
            )
            out["ok"] = True
        finally:
            with contextlib.suppress(Exception):
                await adapter.aclose()
    except Exception as exc:  # noqa: BLE001 - 探不到的原因就是要展示给用户的东西
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)
    return out


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------


class RepoIn(BaseModel):
    platform: Platform
    owner: str
    name: str
    base_branch: str | None = None
    test_hint: str | None = None
    since_days: int = Field(default=30, ge=0, le=3650)
    collect: list[str] = Field(default_factory=lambda: ["issue", "pr"])
    labels_include: list[str] = Field(default_factory=list)
    labels_exclude: list[str] = Field(default_factory=list)
    enabled: bool = True


class RepoPatch(BaseModel):
    enabled: bool | None = None
    base_branch: str | None = None
    test_hint: str | None = None
    since_days: int | None = Field(default=None, ge=0, le=3650)
    collect: list[str] | None = None
    labels_include: list[str] | None = None
    labels_exclude: list[str] | None = None


class SettingsIn(BaseModel):
    """设置页提交的内容：``env`` 是 .env 的键值（白名单内），``overlay`` 是覆盖层路径。"""

    env: dict[str, str] = Field(default_factory=dict)
    overlay: dict[str, Any] = Field(default_factory=dict)


class LlmTestIn(BaseModel):
    """探活参数。都是可选的：没给就用当前生效值。

    这里**没有** ``fallback_model``：探活不做降级（理由见 :func:`probe_llm`），留着这个字段
    等于让人以为它能生效——本项目对「写了不生效」的配置一向是直接删掉。
    """

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(default=None, repr=False)


class DbTestIn(BaseModel):
    """探活参数。

    ``url`` 留空 = 测**当前生效的那个库**：连接串现在是按密钥处理的（输入框为空、只显示掩码），
    所以「不填」是常态。要求必填会让「测试连接」按钮直接 422。
    """

    url: str | None = None


class PlatformTestIn(BaseModel):
    platform: Platform
    token: str = Field(default="", repr=False)


class RepoProbeIn(BaseModel):
    platform: Platform
    owner: str
    name: str


class SetupApplyIn(BaseModel):
    """向导最后一步：一次性写配置，可选立刻跑第一轮抓取。"""

    env: dict[str, str] = Field(default_factory=dict)
    repos: list[RepoIn] = Field(default_factory=list)
    first_scan: bool = True
    scan_limit: int | None = Field(default=None, ge=1, le=500)


_CANONICAL_COLLECT = {"issue", "pr"}


def guard_state(request: Request, settings: Settings) -> tuple[bool, str]:
    """这道边界现在通不通（给页面决定「要不要显示可编辑表单」）。"""
    try:
        setup_guard(request, settings)
        return True, ""
    except HTTPException as exc:
        return False, str(exc.detail)


def _repo_of(request: Request) -> Any:
    """取仓储。**延迟导入** app 的兜底：setup 被 app 导入，模块级互相导入会成环。"""
    state = request.app.state
    if getattr(state, "repo", None) is None:  # pragma: no cover - lifespan 正常会给
        from .app import _ensure_state

        _ensure_state(request)
    return state.repo


def repos_view(request: Request, settings: Settings) -> dict[str, Any]:
    from .status import repo_overview

    ok, reason = guard_state(request, settings)
    return {
        "repos": repo_overview(request.app, settings, _repo_of(request)),
        "writable": ok,
        "blocked_reason": reason,
        "overlay": str(overlay_file(settings)),
        "config_path": settings.config_path,
        "overlay_active": settings.overlay_path is not None,
    }


def _collect_list(values: list[str]) -> list[str]:
    bad = sorted(set(values) - _CANONICAL_COLLECT)
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"collect 只接受 issue / pr，收到：{'、'.join(bad)}",
        )
    return [v for v in ("issue", "pr") if v in values] or ["issue"]


def build_repo(body: RepoIn) -> RepoConfig:
    """表单/请求体 → 仓库配置，顺带做与 ``Settings._validate`` 同源的校验。"""
    owner = body.owner.strip()
    name = body.name.strip()
    if not owner or not name:
        raise HTTPException(status_code=400, detail="owner 与 name 都不能为空")
    if "/" in name:
        raise HTTPException(status_code=400, detail=f"name 不该包含 '/'：{name}（owner 请分开填）")
    if "/" in owner:
        raise HTTPException(status_code=400, detail=f"owner 不该包含 '/'：{owner}")
    return RepoConfig(
        platform=body.platform,
        owner=owner,
        name=name,
        base_branch=(body.base_branch or None),
        test_hint=(body.test_hint or None),
        since_days=body.since_days,
        collect=_collect_list(body.collect),
        labels_include=body.labels_include,
        labels_exclude=body.labels_exclude,
        enabled=body.enabled,
    )


def auth_view(request: Request, settings: Settings) -> dict[str, Any]:
    """设置页「管理员账号」区要的数据（口令/2FA 状态、会话与 Key 计数）。

    为什么单独一块：这些值住在**数据库**（auth_users / sessions / api_keys），
    与 ``describe_settings`` 那套「配置文件里的值」是两种东西。混在一起会让
    「保存设置」看起来能改口令——它不能，也绝不该能（口令只存哈希，不经配置写入器）。

    取不到数据库时**不抛**：设置页在「数据库连不上」时仍然要能渲染出来，
    否则用户连改数据库地址的界面都打不开（而那正是他此刻需要的东西）。
    """
    from .auth import resolve_identity, store_of

    identity = resolve_identity(request)
    info: dict[str, Any] = {
        "username": identity.username,
        "source": identity.label,
        "authenticated": identity.authenticated,
        "provisioned": False,
        "admin_name": "",
        "two_factor": False,
        "sessions": 0,
        "api_keys": 0,
    }
    try:
        store = store_of(request)
        admin = store.admin()
        info.update(
            {
                "provisioned": admin is not None,
                "admin_name": admin.username if admin else "",
                "two_factor": bool(admin and admin.totp_enabled),
                "sessions": len(store.list_sessions(limit=100)),
                "api_keys": store.key_count(active_only=True),
            }
        )
    except Exception as exc:  # noqa: BLE001 - 状态读不到不该让设置页 500
        log.debug("读取鉴权状态失败（设置页将显示为空）：%s", exc)
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def settings_view(request: Request, settings: Settings) -> dict[str, Any]:
    """设置页的数据：每个可写项的值（密钥掩码）、来源、是否需重启，以及当前边界状态。"""
    ok, reason = guard_state(request, settings)
    return {
        **describe_settings(settings),
        "writable": ok,
        "blocked_reason": reason,
        "writable_keys": sorted(ENV_WRITABLE),
        "overlay_writable": sorted(OVERLAY_WRITABLE),
        "restart_keys": sorted(RESTART_REQUIRED),
        "auth": auth_view(request, settings),
    }


def register(app: FastAPI, settings: Settings, *, auth: Any) -> None:
    """注册配置写入类端点。

    每个会写盘的处理函数都走同一套路：``setup_guard``（边界）→ 写文件（白名单）→
    ``reload_state``（立刻生效）→ 返回新视图。少任何一步都会变成「保存了但没生效」或
    「谁都能改密钥」。

    ``settings`` 只用于**注册期**取 API 前缀等常量；处理函数里一律用 :func:`_current`
    现取，否则热重载后闭包里还是旧对象。

    按域拆开注册（原先是一个 279 行的单函数）：仓库管理 / 设置 / 向导探活 / 向导应用。
    切分只按域移动代码，**注册顺序保持原样**。
    """
    _register_repos(app, settings, auth=auth)
    _register_settings(app, settings, auth=auth)
    _register_probes(app, settings, auth=auth)
    _register_apply(app, settings, auth=auth)


def _platform_of(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError as exc:
        allowed = "、".join(p.value for p in Platform)
        raise HTTPException(
            status_code=400, detail=f"平台取值非法：{value!r}；可选：{allowed}"
        ) from exc


def find_index(settings: Settings, platform: Platform, owner: str, name: str) -> int:
    for index, repo in enumerate(settings.repos):
        if repo.platform is platform and repo.owner == owner and repo.name == name:
            return index
    raise HTTPException(status_code=404, detail=f"配置里没有这个仓库：{platform.value}:{owner}/{name}")


def _register_repos(app: FastAPI, settings: Settings, *, auth: Any) -> None:
    """监控仓库：列出 / 加入 / 改 / 移出，以及抓取前的探活。"""
    prefix = settings.api.prefix

    @app.get(f"{prefix}/repos")
    async def list_repos(request: Request, _: None = Depends(auth)) -> dict[str, Any]:
        return repos_view(request, _current(request))

    @app.post(f"{prefix}/repos", status_code=201)
    async def add_repo(
        request: Request, body: RepoIn, _: None = Depends(auth)
    ) -> dict[str, Any]:
        settings = _current(request)
        setup_guard(request, settings)
        new = build_repo(body)
        for existing in settings.repos:
            if existing.platform is new.platform and existing.slug == new.slug:
                raise HTTPException(
                    status_code=409,
                    detail=f"这个仓库已经在监控里：{new.platform.value}:{new.slug}",
                )
        save_repos(overlay_file(settings), settings, [*settings.repos, new])
        fresh = _reload_or_report(app)
        log.info("已加入监控：%s（%s）", new.slug, new.platform.value)
        return repos_view(request, fresh)

    @app.patch(f"{prefix}/repos/{{platform}}/{{owner}}/{{name}}")
    async def patch_repo(
        request: Request,
        platform: str,
        owner: str,
        name: str,
        body: RepoPatch,
        _: None = Depends(auth),
    ) -> dict[str, Any]:
        settings = _current(request)
        setup_guard(request, settings)
        index = find_index(settings, _platform_of(platform), owner, name)
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if "collect" in changes:
            changes["collect"] = _collect_list(body.collect or [])
        for key in ("base_branch", "test_hint"):
            # 显式传空串 = 清掉这个字段（Pydantic 的 exclude_none 会把 None 滤掉，
            # 所以「清空」要靠空串表达，这里把它翻回 None）。
            if changes.get(key) == "":
                changes[key] = None
        updated = settings.repos[index].model_copy(update=changes)
        repos = list(settings.repos)
        repos[index] = updated
        save_repos(overlay_file(settings), settings, repos)
        fresh = _reload_or_report(app)
        log.info("已更新仓库配置：%s（%s）", updated.slug, "、".join(sorted(changes)))
        return repos_view(request, fresh)

    @app.delete(f"{prefix}/repos/{{platform}}/{{owner}}/{{name}}")
    async def delete_repo(
        request: Request,
        platform: str,
        owner: str,
        name: str,
        purge_data: bool = Query(False, description="是否连库里的条目一起删（默认只停止监控）"),
        _: None = Depends(auth),
    ) -> dict[str, Any]:
        """移出监控列表。

        默认**只停止监控、保留历史数据**：条目、评测、验证记录是花了真金白银（token）换来的，
        删掉监控配置就顺手把它们清了是灾难性的。要真删得显式传 ``purge_data=true``。
        """
        settings = _current(request)
        setup_guard(request, settings)
        platform_enum = _platform_of(platform)
        index = find_index(settings, platform_enum, owner, name)
        removed = settings.repos[index]
        repos = [r for i, r in enumerate(settings.repos) if i != index]
        save_repos(overlay_file(settings), settings, repos)

        purged = 0
        if purge_data:
            purged = _purge_repo(request, removed)
        fresh = _reload_or_report(app)
        kept = _repo_of(request).count_items(repo_slug=removed.slug)
        log.info("已移出监控：%s（purge_data=%s）", removed.slug, purge_data)
        return {
            **repos_view(request, fresh),
            "removed": f"{removed.platform.value}:{removed.slug}",
            "purged_items": purged,
            "kept_items": kept,
            "note": (
                "已连同库里的条目一起删除。"
                if purge_data
                else f"只停止了监控，库里还有 {kept} 条历史数据（要一起删可传 purge_data=true）。"
            ),
        }

    @app.post(f"{prefix}/repos/probe")
    async def repo_probe(
        request: Request, body: RepoProbeIn, _: None = Depends(auth)
    ) -> dict[str, Any]:
        settings = _current(request)
        setup_guard(request, settings)
        candidate = build_repo(
            RepoIn(platform=body.platform, owner=body.owner, name=body.name)
        )
        return await probe_repo(settings, candidate.platform, candidate.owner, candidate.name)

def _register_settings(app: FastAPI, settings: Settings, *, auth: Any) -> None:
    """设置：读 / 写（.env 与覆盖层）。"""
    prefix = settings.api.prefix

    # -- 设置 -------------------------------------------------------------

    @app.get(f"{prefix}/settings")
    async def get_settings(request: Request, _: None = Depends(auth)) -> dict[str, Any]:
        return settings_view(request, _current(request))

    @app.put(f"{prefix}/settings")
    async def put_settings(
        request: Request, body: SettingsIn, _: None = Depends(auth)
    ) -> dict[str, Any]:
        settings = _current(request)
        setup_guard(request, settings)
        # 先校验、后落盘：不合法就 400，绝不留下一个「重启起不来」的 .env / 覆盖层。
        _validate_candidate(settings, body.env, body.overlay or {})
        written_env = apply_env(env_file(settings), body.env)
        written_overlay: list[str] = []
        if body.overlay:
            written_overlay = apply_overlay(overlay_file(settings), settings, body.overlay)
        fresh = _reload_or_report(app)
        needs_restart = sorted(k for k in written_env if k in RESTART_REQUIRED)
        return {
            **settings_view(request, fresh),
            "written_env": written_env,
            "written_overlay": written_overlay,
            "restart_required": needs_restart,
            "note": (
                "监听地址/端口是进程启动时绑好的 socket，改动要**重启服务**才生效。"
                if needs_restart
                else "已写盘并热重载，立即生效。"
            ),
        }

def _register_probes(app: FastAPI, settings: Settings, *, auth: Any) -> None:
    """向导探活：真的打一次 LLM / 数据库 / 平台（写盘前先确认能通）。"""
    prefix = settings.api.prefix

    # -- 向导探活 ---------------------------------------------------------

    @app.post(f"{prefix}/setup/test-llm")
    async def test_llm(
        request: Request, body: LlmTestIn, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """真的打一次 LLM。请求里没给的值回落到当前生效值，所以「测现有配置」直接空提交即可。

        **刻意不做备用模型降级**：见 :func:`probe_llm` —— 降级会把报错指向备用模型，
        让人以为自己的配置被环境变量覆盖了。
        """
        settings = _current(request)
        setup_guard(request, settings)
        return await probe_llm(
            base_url=body.base_url or settings.llm.base_url,
            model=body.model or settings.llm.model,
            api_key=body.api_key or settings.llm.api_key,
        )

    @app.post(f"{prefix}/setup/test-db")
    async def test_db(
        request: Request, body: DbTestIn, _: None = Depends(auth)
    ) -> dict[str, Any]:
        settings = _current(request)
        setup_guard(request, settings)
        return await probe_database(body.url or str(settings.database.url))

    @app.post(f"{prefix}/setup/test-platform")
    async def test_platform(
        request: Request, body: PlatformTestIn, _: None = Depends(auth)
    ) -> dict[str, Any]:
        settings = _current(request)
        setup_guard(request, settings)
        token = body.token or str(getattr(settings.tokens, body.platform.value, "") or "")
        if not token:
            return {
                "ok": False,
                "platform": body.platform.value,
                "error": "没有可用的 Token：请求里没带，当前配置里也没配。",
            }
        return await probe_platform(settings, body.platform, token)

def _register_apply(app: FastAPI, settings: Settings, *, auth: Any) -> None:
    """向导最后一步：写 .env + 覆盖层，热重载，可选立刻跑第一轮抓取。"""
    prefix = settings.api.prefix

    @app.post(f"{prefix}/setup/apply")
    async def setup_apply(
        request: Request, body: SetupApplyIn, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """向导最后一步：写 .env + 覆盖层，热重载，可选立刻跑第一轮抓取。"""
        settings = _current(request)
        setup_guard(request, settings)
        _validate_candidate(settings, body.env, {})
        written_env = apply_env(env_file(settings), body.env) if body.env else []
        added: list[str] = []
        if body.repos:
            repos = list(settings.repos)
            for item in body.repos:
                candidate = build_repo(item)
                if not any(
                    r.platform is candidate.platform and r.slug == candidate.slug for r in repos
                ):
                    repos.append(candidate)
                    added.append(f"{candidate.platform.value}:{candidate.slug}")
            save_repos(overlay_file(settings), settings, repos)
        fresh = _reload_or_report(app)

        job: dict[str, Any] | None = None
        error = ""
        if body.first_scan and fresh.enabled_repos():
            # 首轮抓取走作业（可轮询进度），而不是在这里同步等——抓一个真实仓库要几十秒到几分钟。
            from .app import _JOB_WORK, _bind_job

            params = {"repo": None, "full": False, "limit": body.scan_limit}
            try:
                submitted = app.state.jobs.submit(
                    "scan", params, _bind_job(app, _JOB_WORK["scan"], params)
                )
                job = submitted.to_dict()
            except Exception as exc:  # noqa: BLE001 - 首次抓取失败不该让「配置已保存」变成错误
                error = f"{type(exc).__name__}: {exc}"
        return {
            "written_env": written_env,
            "added_repos": added,
            "restart_required": sorted(k for k in written_env if k in RESTART_REQUIRED),
            "first_scan_job": job,
            "first_scan_error": error,
            "settings": settings_view(request, fresh),
        }

def _purge_repo(request: Request, repo: RepoConfig) -> int:
    """把某个仓库的条目连同库里的行一起删掉（显式要求时才走）。

    **必须带上平台**：``slug`` 只是 ``owner/name``，不含平台——两个平台上可以有同名仓库，
    项目自己的 ``Settings.repo()`` 也允许按平台消歧。只按 slug 删，会把另一个平台上
    同名仓库的条目一并删掉（数据不可恢复）。
    """
    from sqlalchemy import delete

    from ..store.tables import ItemRow, RepoRow

    platform = repo.platform.value
    state = request.app.state
    with state.db.session() as session:
        count = len(
            session.scalars(
                select_ids(ItemRow).where(
                    ItemRow.repo_slug == repo.slug, ItemRow.platform == platform
                )
            ).all()
        )
        if count:
            session.execute(
                delete(ItemRow).where(
                    ItemRow.repo_slug == repo.slug, ItemRow.platform == platform
                )
            )
        session.execute(
            delete(RepoRow).where(RepoRow.slug == repo.slug, RepoRow.platform == platform)
        )
    return count


def select_ids(model: Any) -> Any:
    """`select(model.id)` 的小包装（把 SQLAlchemy 的导入留在函数里，模块顶层保持轻）。"""
    from sqlalchemy import select

    return select(model.id)


def _current(request: Request) -> Settings:
    """**当前**生效的配置。

    必须每次现取：设置页/仓库页写完会热重载，捕获在闭包里的那份是启动时的旧对象——
    拿它做「加仓库前先看看有没有重复」会基于过期列表判断。
    """
    state = request.app.state
    if getattr(state, "settings", None) is None:  # pragma: no cover - lifespan 正常会给
        from .app import _ensure_state

        _ensure_state(request)
    return state.settings
