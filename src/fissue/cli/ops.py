"""CLI 其余命令：fix / report / export / serve / web / status / sandbox。

单独成模块是为了让 ``main.py`` 只保留「入口 + 抓取评测验证」这一组高频命令，
读写更清爽。这里在导入时把命令注册到同一个 :data:`fissue.cli.main.app` 上。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer

from ..errors import FissueError
from ..models import Category, FixOutcome, ItemStatus, ItemType, Priority, QueueName
from .main import (
    CONFIG_OPT,
    ENV_OPT,
    JSON_OPT,
    LIMIT_OPT,
    PLATFORM_OPT,
    REPO_OPT,
    VERBOSE_OPT,
    app,
    emit,
    emit_json,
    fail,
    load_cfg,
    print_table,
    run_async,
    sandbox_app,
)


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


@app.command("fix")
def fix(
    repo: list[str] = REPO_OPT,
    platform: Optional[str] = PLATFORM_OPT,
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    limit: int = typer.Option(5, "--limit", "-n", help="最多修复多少条"),
    key: Optional[str] = typer.Option(None, "--key", help="只修复指定条目"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只产出补丁与报告，不提 PR"),
    plan: bool = typer.Option(False, "--plan", help="先让 AI 批量规划优先级再修"),
    rounds: Optional[int] = typer.Option(None, "--rounds", help="Agent 循环最大轮次"),
    json_out: bool = JSON_OPT,
) -> None:
    """自动修复「低难度」Issue 并提 PR（fork → 分支 → PR，带 AI 标签）。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=True)
    repos = _resolve_repos_safe(settings, repo, platform)

    async def main() -> int:
        from ..fixer.autofix import AutoFixer
        from ..pipeline.context import RuntimeContext
        from ..pipeline.flush import FlushProcessor

        async with RuntimeContext.open(settings) as ctx:
            ctx.init_db()
            fixer = AutoFixer(ctx)
            rows: list[dict[str, Any]] = []

            if key:
                item = ctx.repo.get_item(key)
                if item is None:
                    fail(f"条目不存在：{key}")
                attempt = await fixer.fix_item(item, dry_run=dry_run, max_rounds=rounds)
                rows.append(_attempt_row(attempt, dry_run=dry_run))
            else:
                for cfg in repos:
                    if plan:
                        # 先批量规划优先级（Q1 规则）
                        pending = ctx.repo.list_items(
                            repo_slug=cfg.slug, status=ItemStatus.EVALUATED, limit=limit * 4
                        )
                        planned = await FlushProcessor(ctx).plan_priorities(pending)
                        emit(f"[{cfg.slug}] 已规划优先级 {len(planned)} 条")

                    if dry_run:
                        emit("⚠️  dry-run 模式：只产出 patch 与报告，不会创建 PR", style="yellow")

                    report = await fixer.fix_candidates(
                        repo_slug=cfg.slug, limit=limit, dry_run=dry_run, max_rounds=rounds
                    )
                    emit(report.summary)
                    rows.extend(_attempt_row(a, dry_run=dry_run) for a in report.attempts)

        if json_out:
            emit_json(rows)
        else:
            print_table(
                rows,
                ["key", "outcome", "rounds", "branch", "pr", "error"],
                title="自动修复结果",
            )
        return 0

    run_async(main())


def _attempt_row(attempt, *, dry_run: bool = False) -> dict[str, Any]:
    """把一次修复尝试转成明细表的一行。

    明细与汇总必须同一口径：dry-run 下「补丁已产出」的尝试，汇总计入「成功」
    （见 ``FixReport.record``），明细也应显示成功态，否则会出现「汇总说成功 6、
    明细说需人工 6」的自相矛盾。补丁路径仍照常给出，便于核对。
    """
    outcome = attempt.outcome.value
    error = attempt.error or ""
    if (
        dry_run
        and attempt.outcome is FixOutcome.NEEDS_MANUAL
        and attempt.patch_path
    ):
        outcome = FixOutcome.SUCCESS.value
        error = "dry-run：已产出补丁，未提 PR"
    return {
        "key": attempt.item_key,
        "outcome": outcome,
        "rounds": attempt.rounds,
        "branch": attempt.branch or "-",
        "pr": attempt.pr_url or attempt.patch_path or "-",
        "error": error[:60],
    }


def _resolve_repos_safe(settings, repo: list[str], platform: Optional[str]):
    """解析仓库；失败时回退到配置里的启用仓库。"""
    from ..platforms.registry import resolve_repos

    specs = list(repo or [])
    if platform and specs:
        specs = [s if ":" in s else f"{platform}:{s}" for s in specs]
    try:
        repos = resolve_repos(settings, specs or None)
    except FissueError:
        repos = []
    if not repos:
        repos = settings.enabled_repos()
    return repos


# ---------------------------------------------------------------------------
# report / export
# ---------------------------------------------------------------------------


@app.command("report")
def report(
    repo: list[str] = REPO_OPT,
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    fmt: str = typer.Option("markdown", "--format", "-f", help="markdown | json"),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="输出文件路径"),
    limit: int = typer.Option(200, "--limit", help="最多输出多少条"),
    include_body: bool = typer.Option(False, "--with-body", help="包含正文"),
) -> None:
    """生成可读报告（Markdown / JSON）。"""
    settings = load_cfg(config, env, verbose=verbose)
    from ..store import export as exp
    from ..store.db import build_database
    from ..store.repository import Repository

    database = build_database(settings)
    database.ensure_schema()          # 只读命令也要保证表存在（空库时输出空报告）
    repository = Repository(database)

    slugs = list(repo or []) or [r.slug for r in settings.enabled_repos()]
    if not slugs:
        fail("没有可报告的仓库：请用 --repo 指定或在 config.yaml 中登记")

    for slug in slugs:
        if fmt.lower() == "json":
            content = exp.export_json(repository, repo_slug=slug, limit=limit)
        else:
            content = exp.export_markdown(repository, repo_slug=slug, limit=limit, include_body=include_body)

        if out:
            path = out if len(slugs) == 1 else str(_numbered(out, slug))
            written = exp.write_export(content, path)
            emit(f"✅ 已写入 {written}（{len(content)} 字节）", style="green")
        else:
            print(content)


def _numbered(path: str, slug: str):
    from pathlib import Path

    p = Path(path)
    safe = slug.replace("/", "_")
    return p.with_name(f"{p.stem}-{safe}{p.suffix or '.md'}")


@app.command("export")
def export(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    fmt: str = typer.Option("json", "--format", "-f", help="json | markdown"),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="输出目录或文件"),
    repo: list[str] = REPO_OPT,
    limit: int = typer.Option(1000, "--limit", help="最多导出多少条"),
    category: Optional[str] = typer.Option(None, "--category", help="过滤：bug/feature/unknown"),
    status: Optional[str] = typer.Option(None, "--status", help="过滤：new/evaluated/verified/labeled…"),
) -> None:
    """全量导出（含评测结论），默认写文件。"""
    settings = load_cfg(config, env, verbose=verbose)
    from ..store import export as exp
    from ..store.db import build_database
    from ..store.repository import Repository

    database = build_database(settings)
    database.ensure_schema()          # 只读命令也要保证表存在（空库时导出空集合）
    repository = Repository(database)

    slugs = list(repo or []) or [r.slug for r in settings.enabled_repos()]
    cat = Category(category) if category else None
    st = ItemStatus(status) if status else None

    out_dir = out or settings.export.output_dir
    # `--out` 指向文件还是目录：有后缀且只导出一个仓库 → 当作文件路径
    out_is_file = bool(out) and Path(out).suffix.lower() in (".json", ".md", ".markdown")
    if out_is_file and len(slugs) > 1:
        emit("⚠️  --out 指定了单个文件，但有多个仓库；将改为按目录输出", style="yellow")
        out_is_file = False

    written: list[str] = []
    for slug in slugs or [None]:
        if fmt.lower() == "json":
            content = exp.export_json(repository, repo_slug=slug, status=st, category=cat, limit=limit)
            ext = "json"
        else:
            content = exp.export_markdown(repository, repo_slug=slug, status=st, category=cat, limit=limit)
            ext = "md"
        if out_is_file:
            path = exp.write_export(content, out)
        else:
            name = (slug.replace("/", "_") if slug else "all") + f".{ext}"
            path = exp.write_export(content, f"{out_dir}/{name}")
        written.append(str(path))
        emit(f"✅ {path}（{len(content)} 字节）", style="green")
    if len(written) > 1:
        emit(f"共导出 {len(written)} 个文件到 {out_dir}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command("status")
def status(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """查看当前状态：条目统计、队列情况、模型用量、沙盒可用性。"""
    settings = load_cfg(config, env, verbose=verbose)
    from ..store.db import build_database
    from ..store.repository import Repository

    database = build_database(settings)
    if not database.ping():
        fail(f"数据库不可用：{database._safe_url()}")
    database.ensure_schema()          # 全新库也要能看状态，而不是 no such table
    repository = Repository(database)

    from ..sandbox.forwarder import SandboxManager

    ok, reason = SandboxManager(settings.sandbox).available
    usage = repository.usage_today()
    stats = repository.queue_stats()

    by_status: dict[str, int] = {}
    for s in ItemStatus:
        c = repository.count_items(status=s)
        if c:
            by_status[s.value] = c

    data = {
        "repos_config": [r.slug for r in settings.enabled_repos()],
        "sandbox": {"ok": ok, "reason": reason},
        "items": by_status,
        "queues": stats,
        "llm_usage_today": usage,
    }

    if json_out:
        emit_json(data)
        return

    emit(f"配置仓库：{', '.join(data['repos_config']) or '（无）'}")
    emit(f"沙盒：{'✅ ' if ok else '❌ '}{reason}")
    print_table([{"status": k, "count": v} for k, v in by_status.items()], ["status", "count"], title="条目状态")
    qrows = [
        {"queue": q, "status": st, "count": c}
        for q, sts in stats.items()
        for st, c in sts.items()
    ]
    print_table(qrows, ["queue", "status", "count"], title="队列")
    emit(
        f"今日用量：{usage['total_tokens']} tokens（prompt {usage['prompt_tokens']} / "
        f"completion {usage['completion_tokens']}），约 ${usage['cost_usd']:.4f}，调用 {usage['calls']} 次"
    )


# ---------------------------------------------------------------------------
# sandbox serve
# ---------------------------------------------------------------------------


@sandbox_app.command("serve")
def sandbox_serve(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    socket: Optional[str] = typer.Option(None, "--socket", help="覆盖转发组件 socket 路径"),
) -> None:
    """启动沙盒转发组件（宿主侧常驻，沙盒执行的唯一通道）。"""
    settings = load_cfg(config, env, verbose=verbose)
    from ..sandbox.forwarder import ForwarderServer
    from ..sandbox.runtime import DockerRuntime

    runtime = DockerRuntime(settings.sandbox)
    ok, reason = runtime.available()
    if settings.sandbox.runtime == "docker":
        if ok:
            emit(f"✅ Docker 可用：{reason}")
            if not runtime.ensure_image(settings.sandbox.image):
                emit(f"⚠️  基础镜像不可用：{settings.sandbox.image}（执行时会失败）", style="yellow")
        else:
            emit(f"⚠️  Docker 不可用：{reason}；将降级为本地执行（无隔离）", style="yellow")

    server = ForwarderServer(settings.sandbox, socket_path=socket)
    emit(f"转发组件监听：{server.socket_path}（Ctrl+C 退出）")
    try:
        server.start(blocking=True)
    except KeyboardInterrupt:  # pragma: no cover
        emit("\n正在停止…")
    finally:
        server.stop()


@sandbox_app.command("check")
def sandbox_check(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
) -> None:
    """检查沙盒环境是否就绪。"""
    settings = load_cfg(config, env)
    from ..sandbox.forwarder import SandboxManager
    from ..sandbox.runtime import DockerRuntime

    ok, reason = DockerRuntime(settings.sandbox).available()
    manager = SandboxManager(settings.sandbox)
    emit(f"Docker：{'✅ ' if ok else '❌ '}{reason}")
    emit(f"执行后端：{'inline（进程内调用）' if manager.inline else 'forwarder（转发组件）'}")
    emit(f"镜像：{settings.sandbox.image}")
    emit(
        f"隔离：网络={settings.sandbox.network}｜只读根={settings.sandbox.read_only_root}"
        f"｜用户={settings.sandbox.user}｜CPU={settings.sandbox.limits.cpus}"
        f"｜内存={settings.sandbox.limits.memory_mb}MB｜超时={settings.sandbox.limits.timeout_seconds}s"
    )


# ---------------------------------------------------------------------------
# serve / web
# ---------------------------------------------------------------------------


@app.command("serve")
def serve(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    once: bool = typer.Option(False, "--once", help="只跑一轮扫描就退出（适合 cron）"),
    with_web: bool = typer.Option(False, "--with-web", help="同时启动 Web/API"),
) -> None:
    """启动常驻服务：增量扫描 + 队列调度 + 阈值通知。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=True)
    from ..service.daemon import FissueService

    service = FissueService(settings, with_web=with_web)
    if once:
        run_async(service.run_once())
        emit("✅ 单轮扫描完成", style="green")
        return
    try:
        run_async(service.run_forever())
    except KeyboardInterrupt:  # pragma: no cover
        emit("\n已停止")


@app.command("web")
def web(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    host: Optional[str] = typer.Option(None, "--host", help="监听地址"),
    port: Optional[int] = typer.Option(None, "--port", help="监听端口"),
    reload: bool = typer.Option(False, "--reload", help="开发模式自动重载"),
) -> None:
    """启动 Web 仪表盘 + REST API。"""
    settings = load_cfg(config, env, verbose=verbose)
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        fail("缺少 uvicorn：请执行 pip install -e .")

    from ..web.app import create_app

    application = create_app(settings)
    h = host or settings.web.host
    p = port or settings.web.port
    emit(f"🌐 Web/API 启动于 http://{h}:{p}（API 前缀 {settings.api.prefix}）")
    uvicorn.run(application, host=h, port=p, reload=reload, log_level=settings.app.log_level)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def cli_main() -> None:  # pragma: no cover
    """``fissue`` 控制台脚本入口。"""
    try:
        app()
    except FissueError as exc:
        emit(f"❌ {exc}", style="bold red")
        raise SystemExit(1) from exc
