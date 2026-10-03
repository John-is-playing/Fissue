"""Fissue 命令行入口。

命令一览
--------
::

    fissue db init                    建表
    fissue fetch                      抓取 Issue/PR（增量）
    fissue eval                        AI 评测
    fissue verify                      生成验证器 + 沙盒 F2P 验证
    fissue flush                       手动 flush 队列
    fissue fix                         自动修复并提 PR
    fissue report                      生成报告
    fissue export                      导出数据
    fissue sandbox serve               启动沙盒转发组件
    fissue serve                       常驻服务（扫描 + 调度 + 通知）
    fissue web                         Web 仪表盘 + REST API
    fissue status                      查看当前状态

设计：所有命令共享 :class:`~fissue.pipeline.context.RuntimeContext`，
输出统一用 rich（装了彩色，没装优雅降级）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ..config import Settings, load_settings
from ..errors import FissueError
from ..logging_setup import setup_logging
from ..models import Category, ItemStatus, ItemType, Platform, Priority, QueueName

app = typer.Typer(
    name="fissue",
    help="Fissue —— Issue/PR 的 AI 评测、沙盒验证与自动修复",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="数据库管理")
sandbox_app = typer.Typer(help="沙盒与转发组件")
app.add_typer(db_app, name="db")
app.add_typer(sandbox_app, name="sandbox")

# ---------------------------------------------------------------------------
# 通用选项与输出
# ---------------------------------------------------------------------------

CONFIG_OPT = typer.Option(None, "--config", "-c", help="config.yaml 路径")
ENV_OPT = typer.Option(None, "--env", "-e", help=".env 路径")
VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="输出调试日志")
JSON_OPT = typer.Option(False, "--json", help="以 JSON 输出（便于脚本消费）")


def console():
    """惰性获取 rich Console（没装 rich 时用 print 降级）。"""
    try:
        from rich.console import Console

        return Console()
    except Exception:  # pragma: no cover
        return None


def emit(message: str, *, style: str | None = None) -> None:
    con = console()
    if con is not None:
        con.print(message, style=style)
    else:  # pragma: no cover
        print(message)


def emit_json(data: Any) -> None:
    import json

    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def load_cfg(config: str | None, env: str | None, *, verbose: bool = False, require_llm: bool = False) -> Settings:
    settings = load_settings(config, env, require_llm_key=require_llm)
    if verbose:
        settings.app.log_level = "debug"
    setup_logging(settings.app.log_level)
    return settings


def fail(message: str, *, code: int = 1) -> None:
    emit(f"❌ {message}", style="bold red")
    raise typer.Exit(code)


def run_async(coro):
    """统一跑协程（Windows 上避免事件循环策略告警）。"""
    try:
        return asyncio.run(coro)
    except FissueError as exc:
        fail(str(exc))


def print_table(rows: list[dict[str, Any]], columns: list[str], *, title: str | None = None) -> None:
    """打印表格：有 rich 用 rich，否则回退纯文本。"""
    if not rows:
        emit("（无数据）")
        return
    try:
        from rich.table import Table

        table = Table(title=title, show_lines=False)
        for col in columns:
            table.add_column(col, overflow="fold")
        for row in rows:
            table.add_row(*[str(row.get(c, "")) for c in columns])
        con = console()
        con.print(table) if con else print(table)
    except Exception:  # pragma: no cover
        header = " | ".join(columns)
        print(header)
        print("-" * len(header))
        for row in rows:
            print(" | ".join(str(row.get(c, "")) for c in columns))


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@db_app.command("init")
def db_init(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    drop: bool = typer.Option(False, "--drop", help="先删除所有表（危险）"),
) -> None:
    """初始化数据库表结构。"""
    settings = load_cfg(config, env, verbose=verbose)
    from ..store.db import build_database

    database = build_database(settings)
    if not database.ping():
        fail(f"无法连接数据库：{database._safe_url()}（请检查 .env 中的 FISSUE_DATABASE_URL）")
    if drop:
        confirm = typer.confirm("确认要删除所有表？此操作不可恢复", default=False)
        if not confirm:
            emit("已取消")
            raise typer.Exit(0)
        database.drop_all()
    database.create_all()
    emit(f"✅ 数据库已就绪：{database._safe_url()}", style="green")


@db_app.command("check")
def db_check(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
) -> None:
    """检查数据库连通性。"""
    settings = load_cfg(config, env)
    from ..store.db import build_database

    database = build_database(settings)
    ok = database.ping()
    emit(("✅ 连接正常：" if ok else "❌ 连接失败：") + database._safe_url())
    raise typer.Exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def _open_ctx(settings: Settings):
    """构造 RuntimeContext（同步，用于非异步命令）。"""
    from ..pipeline.context import RuntimeContext

    return RuntimeContext.create(settings)


def _resolve_repos(settings: Settings, repo: list[str], platform: Optional[str]):
    """把 CLI 的 --repo / --platform 解析成 RepoConfig 列表。"""
    from ..platforms.registry import resolve_repos

    specs = list(repo or [])
    if platform and specs:
        plat = Platform(platform)
        specs = [s if ":" in s else f"{plat.value}:{s}" for s in specs]
    elif platform and not specs:
        # 只给了平台但没给仓库 → 用配置里该平台的仓库
        return [r for r in settings.enabled_repos() if r.platform is Platform(platform)]

    repos = resolve_repos(settings, specs or None)
    if not repos:
        fail("没有可处理的仓库：请用 --repo owner/name 指定，或在 config.yaml 的 repos 中登记")
    return repos


REPO_OPT = typer.Option(None, "--repo", "-r", help="仓库，如 owner/name（可重复）；支持 platform:owner/name")
PLATFORM_OPT = typer.Option(None, "--platform", "-p", help="平台：github/gitee/atomgit/gitlab")
LIMIT_OPT = typer.Option(None, "--limit", "-n", help="最多处理多少条")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


@app.command("fetch")
def fetch(
    repo: list[str] = REPO_OPT,
    platform: Optional[str] = PLATFORM_OPT,
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    full: bool = typer.Option(False, "--full", help="全量抓取（忽略增量游标）"),
    limit: Optional[int] = LIMIT_OPT,
    concurrency: int = typer.Option(2, "--concurrency", help="并发仓库数"),
) -> None:
    """从平台抓取 Issue/PR（默认增量）。"""
    settings = load_cfg(config, env, verbose=verbose)
    repos = _resolve_repos(settings, repo, platform)

    async def main() -> int:
        from ..pipeline.context import RuntimeContext
        from ..pipeline.fetcher import Fetcher

        async with RuntimeContext.open(settings) as ctx:
            ctx.init_db()
            stats = await Fetcher(ctx).fetch_many(
                repos, full=full, limit=limit, concurrency=concurrency
            )
        emit(
            f"✅ 抓取完成：拉取 {stats.fetched}，新增 {stats.created}，"
            f"更新 {stats.updated}，未变 {stats.unchanged}",
            style="green",
        )
        return 0

    run_async(main())


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


@app.command("eval")
def evaluate(
    repo: list[str] = REPO_OPT,
    platform: Optional[str] = PLATFORM_OPT,
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    limit: Optional[int] = LIMIT_OPT,
    all_items: bool = typer.Option(False, "--all", help="评测所有未评测条目（而不只是新的）"),
    deep: bool = typer.Option(False, "--deep", help="对 PR 追加 diff 深度评审（更贵更准）"),
    concurrency: int = typer.Option(3, "--concurrency", help="并发评测数"),
    json_out: bool = JSON_OPT,
) -> None:
    """用 AI 评测条目（真实性/重要性/可行性/PR质量 + 标签 + 反刷子）。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=True)
    repos = _resolve_repos(settings, repo, platform)

    async def main() -> int:
        from ..pipeline.context import RuntimeContext
        from ..pipeline.stages import EvalStage
        from ..models import ItemStatus as _S

        async with RuntimeContext.open(settings) as ctx:
            ctx.init_db()
            stage = EvalStage(ctx)
            rows: list[dict[str, Any]] = []
            for cfg in repos:
                status = None if all_items else _S.NEW
                items = ctx.repo.list_items(
                    repo_slug=cfg.slug,
                    status=status,
                    limit=limit or (200 if all_items else 50),
                )
                if not items:
                    emit(f"（{cfg.slug} 没有待评测条目）")
                    continue
                conv = ctx.budget_guard(cfg.slug)
                ctx.llm.budget_guard = conv
                result = await stage.run_batch(items, deep=deep, concurrency=concurrency)
                emit(result.summary)
                for r in result.results:
                    if r.evaluation is not None:
                        rows.append(
                            {
                                "key": r.key,
                                "category": r.evaluation.category.value,
                                "authenticity": r.evaluation.authenticity,
                                "importance": r.evaluation.importance,
                                "difficulty": r.evaluation.difficulty,
                                "priority": r.evaluation.priority.value,
                                "action": r.evaluation.action.value,
                            }
                        )
        if json_out:
            emit_json(rows)
        else:
            print_table(
                rows,
                ["key", "category", "authenticity", "importance", "difficulty", "priority", "action"],
                title="评测结果",
            )
        return 0

    run_async(main())


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@app.command("verify")
def verify(
    repo: list[str] = REPO_OPT,
    platform: Optional[str] = PLATFORM_OPT,
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    limit: Optional[int] = LIMIT_OPT,
    key: Optional[str] = typer.Option(None, "--key", help="只验证指定条目，如 github:owner/name#12"),
    json_out: bool = JSON_OPT,
) -> None:
    """生成验证器并在沙盒中做 F2P 验证（base 必失败、修复后必通过）。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=True)

    async def main() -> int:
        from ..pipeline.context import RuntimeContext
        from ..pipeline.stages import VerifyStage, PRVerifyStage
        from ..models import Category as _C

        async with RuntimeContext.open(settings) as ctx:
            ctx.init_db()
            ok, reason = ctx.sandbox.available
            emit(f"沙盒：{reason}")
            if not ok:
                emit("⚠️  沙盒不可用，验证将降级为本地执行或直接失败", style="yellow")

            rows: list[dict[str, Any]] = []
            if key:
                item = ctx.repo.get_item(key)
                if item is None:
                    fail(f"条目不存在：{key}")
                targets = [item]
                repos_cfg = None
            else:
                repos = _resolve_repos(settings, repo, platform)
                targets = []
                for cfg in repos:
                    targets.extend(
                        ctx.repo.list_items(
                            repo_slug=cfg.slug,
                            status=ItemStatus.EVALUATED,
                            limit=limit or 30,
                        )
                    )
                repos_cfg = repos

            stage_issue = VerifyStage(ctx)
            stage_pr = PRVerifyStage(ctx)
            for item in targets:
                ev = ctx.repo.latest_evaluation(item.key)
                if ev is None:
                    emit(f"跳过 {item.key}：尚未评测")
                    continue
                if ev.category is not _C.BUG:
                    emit(f"跳过 {item.key}：分类为 {ev.category.value}（FEATURE 不验证，只打标签）")
                    ctx.repo.set_item_status(item.key, ItemStatus.LABELED)
                    continue
                r = await (stage_issue.run(item, ev) if item.item_type is ItemType.ISSUE else stage_pr.run(item, ev))
                rows.append(
                    {
                        "key": r.key,
                        "type": item.item_type.value,
                        "verifier": r.verifier_kind.value if r.verifier_kind else "-",
                        "reproduced": bool(r.verifier_result and r.verifier_result.reproducible),
                        "f2p": bool(r.verifier_result and r.verifier_result.f2p_satisfied),
                        "queued": r.queued.value if r.queued else "-",
                        "note": (r.skipped_reason or r.error or (r.notes[-1] if r.notes else ""))[:48],
                    }
                )
        if json_out:
            emit_json(rows)
        else:
            print_table(rows, ["key", "type", "verifier", "reproduced", "f2p", "queued", "note"], title="验证结果")
        return 0

    run_async(main())


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


@app.command("flush")
def flush(
    config: Optional[str] = CONFIG_OPT,
    env: Optional[str] = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    queue: Optional[str] = typer.Option(None, "--queue", help="只 flush 指定队列：verify/fix_bug/fix_feature"),
    force: bool = typer.Option(True, "--force/--auto", help="手动强制 flush（默认）或按触发条件判断"),
    limit: int = typer.Option(50, "--limit", help="单批最多处理多少条"),
    json_out: bool = JSON_OPT,
) -> None:
    """手动 flush 队列：把一批已验证条目交 AI 出结论并打标签。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=True)

    async def main() -> int:
        from ..pipeline.context import RuntimeContext
        from ..pipeline.flush import FlushProcessor
        from ..pipeline.queue import QueueManager

        async with RuntimeContext.open(settings) as ctx:
            ctx.init_db()
            qm = QueueManager(ctx)
            processor = FlushProcessor(ctx)
            handler = processor.make_handler()

            names = [QueueName(queue)] if queue else list(QueueName)
            outcomes = []
            for q in names:
                decision = qm.should_flush(q, force=force)
                emit(f"[{q.value}] {decision.reason}")
                if not decision.should_flush:
                    continue
                outcomes.append(await qm.flush(q, handler=handler, force=force, limit=limit))

            for o in outcomes:
                emit(f"✅ [{o.queue}] 已 flush {o.flushed} 条，结论 {len(o.conclusions)} 条"
                     + (f"（错误：{o.error}）" if o.error else ""))
            if not outcomes:
                emit("（没有需要 flush 的队列）")
        return 0

    run_async(main())
