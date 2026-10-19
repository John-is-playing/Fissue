"""状态聚合：给「状态监测」页与 ``/api/v1/status`` 提供一份**实时快照**。

为什么单独成一个模块：这份快照要拼七八个来源（写闸门、数据库、沙盒、队列、作业表、今日用量、
抓取记录、配置告警）。把拼装写在 ``web/app.py`` 的路由函数里会让那个文件更难读；更重要的是
CLI 的 ``fissue status`` 与 Web 应当**共用同一份口径**——各拼一遍必然会对不上，而「同一个东西
两个页面显示不同数字」正是本项目在 ``summarize`` 那里踩过的老坑。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

from .. import __version__
from ..config import DEFAULT_OVERLAY_FILE, Settings
from ..lockfile import GATE_KINDS, fcntl, probe_write_lock
from ..logging_setup import get_logger
from ..models import QueueName

log = get_logger(__name__)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def mask_url(url: str) -> str:
    """把 URL 里的用户口令与查询串值遮掉，**其余部分逐字节保留**。

    刻意不走 ``urlsplit``/``urlunsplit`` 那条路：它们会「规范化」，而规范化的代价在这里很具体，
    两处都是实测出来的——

    * ``sqlite:///E:/x.db?mode=rw`` → ``sqlite:/E:/x.db``（netloc 为空时前导双斜杠被吞掉），
      脱敏反而把库路径显示坏了；
    * 查询串自带 ``?`` 再交给 ``urlunsplit``，会拼出 ``??mode=``。

    所以这里只做两件手术：查询串的**值**、权限段里的**口令**。
    """
    if not url:
        return ""
    base, qmark, query = url.partition("?")
    if qmark:
        # 查询串的值整体遮掉：`?key=xxx` 这类写法真的有人用（LLM 端点），
        # 一旦被原样打出来就等于把密钥贴在页面上。
        parts: list[str] = []
        for pair in query.split("&"):
            key, eq, _ = pair.partition("=")
            parts.append(f"{key}=***" if eq else key)
        url = f"{base}?{'&'.join(parts)}"

    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, tail = rest.partition("/")
    if "@" in authority:
        # `user:pw@host` → `user:***@host`（只留用户名，便于确认「连的是哪个账号」）
        userinfo, _, host = authority.rpartition("@")
        authority = f"{userinfo.split(':', 1)[0]}:***@{host}"
    return f"{scheme}{sep}{authority}{slash}{tail}"


def _backend_of(url: str) -> str:
    scheme = url.split(":", 1)[0].lower()
    if scheme.startswith("sqlite"):
        return "sqlite"
    if "postgres" in scheme:
        return "postgresql"
    return scheme or "unknown"


def queue_panel(repo: Any, settings: Settings) -> list[dict[str, Any]]:
    """队列积压面板的数据。

    为什么不直接构造 ``QueueManager(ctx)``：它的构造要一个完整的 ``RuntimeContext``
    （含 LLM 客户端、沙盒管理器……），而这里只要几个计数——每渲染一次就组装一整套对象图是
    白花钱。这里只读 ``repo`` 的计数与阈值**原样展示**（``待定论 3 / 阈值 10``），
    **不在前端重算「该不该 flush」**：那个判定只有服务端一份（``QueueManager.should_flush``），
    看板不复制它——复制出来的第二份口径迟早会和第一份不一样。
    """
    stats = repo.queue_stats()
    panel: list[dict[str, Any]] = []
    for queue in QueueName:
        spec = (
            settings.queues.verify_queue
            if queue is QueueName.VERIFY
            else settings.queues.fix_queue
        )
        panel.append(
            {
                "name": queue.value,
                "counts": stats.get(queue.value, {}),
                "pending": repo.queue_pending_count(queue),
                "waiting": len(repo.queue_unflushed(queue)),
                "flush_size": spec.flush_size,
                "idle_flush_seconds": spec.idle_flush_seconds,
                "oldest_seconds": repo.oldest_unflushed_age(queue),
            }
        )
    return panel


def repo_overview(app: FastAPI, settings: Settings, repo: Any) -> list[dict[str, Any]]:
    """每个监控仓库的一行：配置里的、库里的、以及两者是否一致。"""
    rows = {r.slug: r for r in repo.list_repos()}
    out: list[dict[str, Any]] = []
    for cfg in settings.repos:
        row = rows.get(cfg.slug)
        pending = row.cursor if row is not None else {}
        out.append(
            {
                "slug": cfg.slug,
                "platform": cfg.platform.value,
                "enabled": cfg.enabled,
                "collection": list(cfg.collect),
                # `base_branch` 必须回传：仓库页的输入框用它回填。原先漏了这个字段，输入框
                # 永远是空的，而「保存参数」会把空值按「清空」语义写下去——点一下就把已配的
                # base 分支静默清成 null，自动修复提 PR 从此退化成对着 main 改。
                "base_branch": cfg.base_branch,
                "test_hint": cfg.test_hint,
                "since_days": cfg.since_days,
                # 库里那一份（抓取快照）：配置里登记了但还没抓过的仓库，这里是 None。
                "in_db": row is not None,
                "items": repo.count_items(repo_slug=cfg.slug),
                "last_scanned_at": _iso(row.last_scanned_at) if row is not None else None,
                "cursor": pending if isinstance(pending, dict) else {},
            }
        )
    # 只在库里、不在配置里的「孤儿」仓库：说明 config.yaml / 覆盖层把某条移除了，
    # 但历史数据还在。不显示的话，用户会奇怪「条目怎么还在」。
    known = {c.slug for c in settings.repos}
    for slug, row in rows.items():
        if slug not in known:
            out.append(
                {
                    "slug": slug,
                    "platform": str(row.platform),
                    "enabled": False,
                    "collection": [],
                    "test_hint": None,
                    "since_days": None,
                    "in_db": True,
                    "orphan": True,
                    "items": repo.count_items(repo_slug=slug),
                    "last_scanned_at": _iso(row.last_scanned_at),
                    "cursor": {},
                }
            )
    return out


def _sandbox_section(app: FastAPI, settings: Settings, *, probe: bool) -> dict[str, Any]:
    sb = settings.sandbox
    out: dict[str, Any] = {
        "enabled": sb.enabled,
        "runtime": sb.runtime,
        "image": sb.image,
        "network": sb.network,
        "read_only_root": sb.read_only_root,
        "user": sb.user or "（镜像默认）",
        "limits": sb.limits.model_dump(),
        "ready": None,
        "reason": "未探测（加 ?sandbox=1 触发）",
    }
    if not probe:
        return out
    if not sb.enabled:
        out["reason"] = "沙盒已关闭（sandbox.enabled=false）——验证器无法运行"
        return out
    # 探测会真的起一次 Docker 调用（本地模式则直接可用），所以只在显式要求时做，
    # 免得每 2 秒一次的状态轮询都在敲 Docker。
    try:
        from ..sandbox.runtime import DockerRuntime

        ok, reason = DockerRuntime(sb).available()
        out["ready"] = bool(ok)
        out["reason"] = str(reason)
    except Exception as exc:  # noqa: BLE001 - 探测失败本身就是状态的一部分
        out["ready"] = False
        out["reason"] = f"{type(exc).__name__}: {exc}"
    return out


def _alerts(
    settings: Settings,
    repo: Any,
    *,
    usage: dict[str, Any],
    locks_ok: bool,
) -> list[dict[str, str]]:
    """把「当前配置/环境有什么不对」写成可操作的一条条。

    为什么要有它：新手最常见的问题不是「功能不work」，而是「我少配了一个东西，而没有任何地方
    告诉我」。每条都带 ``hint``（怎么办），而不是只说「未配置」。
    """
    alerts: list[dict[str, str]] = []

    def add(level: str, message: str, hint: str = "") -> None:
        alerts.append({"level": level, "message": message, "hint": hint})

    if not settings.llm.api_key:
        add(
            "error",
            "未配置 LLM API Key：评测、验证器生成、自动修复都会失败。",
            "在「设置」里填 Key，或写进 .env 的 FISSUE_LLM_API_KEY。",
        )
    repos = settings.enabled_repos()
    if not repos:
        add(
            "warn",
            "没有启用中的监控仓库：抓取与流水线都没有输入。",
            "到「仓库」页添加一个，或在 config.yaml 的 repos 里登记。",
        )
    if not any(getattr(settings.tokens, p) for p in ("github", "gitee", "atomgit", "gitlab")):
        add(
            "warn",
            "未配置任何平台 Token：只能匿名读取公开仓库，限流很紧（GitHub 60 次/小时），且无法提 PR。",
            "到「设置」里填平台 Token。",
        )
    if not settings.sandbox.enabled:
        add(
            "warn",
            "沙盒已关闭：验证器不跑，F2P 与回归门都失效（自动修复会失去证据）。",
            "把 config.yaml 的 sandbox.enabled 打开。",
        )
    if not settings.web.auth.enabled and not settings.web.trusted_networks:
        add(
            "warn",
            "会话鉴权已关闭（web.auth.enabled=false）且没有声明可信网段："
            "只有本机回环能访问，其它来源一律被拒。",
            "要开放给内网，请把网段写进 web.trusted_networks；要对外开放，打开 web.auth.enabled。",
        )
    elif not settings.web.trusted_networks:
        host = str(settings.web.host or "").strip().lower()
        loopback = host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")
        if not loopback:
            add(
                "info",
                f"已绑定 {settings.web.host} 但未声明可信网段：内网浏览器需要先登录。",
                "想免登录直连，可在设置页把内网网段写进 web.trusted_networks（例如 192.168.1.0/24）。",
            )
    if not locks_ok:
        add(
            "info",
            "本平台没有 flock：跨进程互斥不生效——别同时跑常驻服务与 CLI，会重复评测、重复计费。",
            f"当前平台：{sys.platform}。",
        )
    stale = repo.items_with_stale_conclusions()
    if stale:
        add(
            "warn",
            f"有 {len(stale)} 条条目的结论已过期（内容改过、当时正在执行，没自动重评）。",
            "执行 fissue requeue --stale 把它们放回流水线。",
        )
    if usage.get("calls") and usage.get("total_tokens", 0) > 0:
        budget = settings.llm.budget
        if budget.daily_usd_per_repo and usage.get("cost_usd", 0.0) > budget.daily_usd_per_repo:
            add(
                "warn",
                f"今日花费 ${usage['cost_usd']:.4f} 已超过单仓库日预算 ${budget.daily_usd_per_repo}。",
                "调高 llm.budget.daily_usd_per_repo，或等 UTC 午夜归零。",
            )
    return alerts


def writer_panel(settings: Settings) -> dict[str, Any]:
    """「谁在写、在做什么、写了多久」——状态页与任务页共用这一份口径。

    为什么抽出来：**两个页面都要用它**。状态页显示写闸门；任务页则要能说明「正在写的东西
    不在这张表里」（常驻服务的定时轮次不是网页作业——真机实测里用户就是对着空表 +
    「写操作进行中」发懵的）。各拼一遍迟早对不上。
    """
    writer = probe_write_lock(settings)
    return {
        "busy": writer.busy,
        "who": writer.who,
        # `kind` 是给页面用的稳定取值（见 lockfile.GATE_KINDS），`kind_label` 是给人看的中文。
        "kind": writer.kind,
        "kind_label": GATE_KINDS.get(writer.kind, writer.kind or "写操作"),
        "since": _iso(writer.since) if writer.since else None,
        "seconds": writer.seconds,
        "locks_supported": fcntl is not None,
    }


def collect(
    app: FastAPI,
    settings: Settings,
    repo: Any,
    *,
    with_sandbox: bool = False,
) -> dict[str, Any]:
    """拼出状态快照。

    ``with_sandbox`` 会真的探测一次沙盒（起一次 Docker 调用），所以默认关闭：状态页每 2 秒
    轮询一次，每次都敲 Docker 既慢又吵。
    """
    now = datetime.now(timezone.utc)
    # 平台没有 flock（Windows）时跨进程互斥不生效——这条要作为告警出现在页面上，
    # 而不是等用户发现「同一条被评测了两遍、钱花了两份」。
    locks_ok = fcntl is not None

    usage = repo.usage_today()
    started = getattr(app.state, "started_at", None)
    jobs = getattr(app.state, "jobs", None)
    db_url = ""
    database = getattr(app.state, "db", None)
    if database is not None:
        db_url = str(getattr(database, "_safe_url", lambda: "")())

    return {
        "now": _iso(now),
        "app": {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": _iso(started),
            "uptime_seconds": round((now - started).total_seconds(), 1) if started else None,
            "read_only": settings.web.read_only,
            "api_prefix": settings.api.prefix,
            "auth_enabled": settings.web.auth.enabled,
            "trusted_networks": list(settings.web.trusted_networks),
            "session_ttl_minutes": settings.web.auth.session_ttl_minutes,
            "secure_cookie": settings.web.auth.secure_cookie,
            "host": settings.web.host,
            "port": settings.web.port,
            "config_path": settings.config_path,
            "env_path": settings.env_path,
            "overlay_path": settings.overlay_path,
            "overlay_file": DEFAULT_OVERLAY_FILE,
            "overlay_active": settings.overlay_path is not None,
        },
        "writer": writer_panel(settings),
        "database": {
            "backend": _backend_of(str(settings.database.url)),
            "url": mask_url(db_url or str(settings.database.url)),
            "items": repo.count_items(),
            "repos": len(repo.list_repos()),
        },
        "llm": {
            "model": settings.llm.model,
            "fallback_model": settings.llm.fallback_model,
            "base_url": mask_url(settings.llm.base_url),
            "api_key_set": bool(settings.llm.api_key),
            "max_tokens": settings.llm.max_tokens,
            "budget": settings.llm.budget.model_dump(),
            "today": usage,
        },
        "sandbox": _sandbox_section(app, settings, probe=with_sandbox),
        "queues": queue_panel(repo, settings),
        "jobs": jobs.to_dict() if jobs is not None else {"jobs": [], "current": None},
        "repos": repo_overview(app, settings, repo),
        "scans": [
            {
                "repo_key": row.repo_key,
                "started_at": _iso(row.started_at),
                "finished_at": _iso(row.finished_at),
                "fetched": row.fetched,
                "created": row.created,
                "updated": row.updated,
                "unchanged": row.unchanged,
                "error": row.error,
                "running": row.finished_at is None,
            }
            for row in repo.list_scan_runs(limit=15)
        ],
        "alerts": _alerts(settings, repo, usage=usage, locks_ok=locks_ok),
    }
