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
import random
import time
from typing import Any, NoReturn

import typer

from ..config import Settings, load_settings
from ..errors import FissueError
from ..logging_setup import get_logger, setup_logging
from ..models import ItemStatus, ItemType, Platform, QueueName

log = get_logger(__name__)

app = typer.Typer(
    name="fissue",
    help="Fissue —— Issue/PR 的 AI 评测、沙盒验证与自动修复",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="数据库管理")
sandbox_app = typer.Typer(help="沙盒与转发组件")
bench_app = typer.Typer(help="跑分基线：冻结测量前提与结果，便于逐项对比")
from .auth import auth_app  # noqa: E402  —— 放在这里：它只用 typer 与 config，不会与 main 成环

app.add_typer(db_app, name="db")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(bench_app, name="bench")
app.add_typer(auth_app, name="auth")

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


def fail(message: str, *, code: int = 1) -> NoReturn:
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
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
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
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
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


def _resolve_repos(settings: Settings, repo: list[str], platform: str | None):
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
    platform: str | None = PLATFORM_OPT,
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    full: bool = typer.Option(False, "--full", help="全量抓取（忽略增量游标）"),
    limit: int | None = LIMIT_OPT,
    concurrency: int = typer.Option(2, "--concurrency", help="并发仓库数"),
) -> None:
    """从平台抓取 Issue/PR（默认增量）。"""
    settings = load_cfg(config, env, verbose=verbose)
    repos = _resolve_repos(settings, repo, platform)

    async def main() -> int:
        from ..pipeline.context import RuntimeContext
        from ..pipeline.fetcher import Fetcher

        async with RuntimeContext.open(settings, exclusive=True) as ctx:
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
    platform: str | None = PLATFORM_OPT,
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    limit: int | None = LIMIT_OPT,
    all_items: bool = typer.Option(False, "--all", help="评测所有未评测条目（而不只是新的）"),
    deep: bool = typer.Option(False, "--deep", help="对 PR 追加 diff 深度评审（更贵更准）"),
    concurrency: int = typer.Option(3, "--concurrency", help="并发评测数"),
    json_out: bool = JSON_OPT,
) -> None:
    """用 AI 评测条目（真实性/重要性/可行性/PR质量 + 标签 + 反刷子）。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=True)
    repos = _resolve_repos(settings, repo, platform)

    async def main() -> int:
        from ..models import ItemStatus as _S
        from ..pipeline.context import RuntimeContext
        from ..pipeline.stages import EvalStage

        async with RuntimeContext.open(settings, exclusive=True) as ctx:
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
    platform: str | None = PLATFORM_OPT,
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    limit: int | None = LIMIT_OPT,
    key: str | None = typer.Option(None, "--key", help="只验证指定条目，如 github:owner/name#12"),
    concurrency: int | None = typer.Option(
        None, "--concurrency", help="条目级并发数（默认取 config.yaml 的 verifier.concurrency）"
    ),
    retries: int | None = typer.Option(
        None, "--retries", help="单条失败（如克隆/网络抖动）后的重试次数（默认取 verifier.item_retries）"
    ),
    json_out: bool = JSON_OPT,
) -> None:
    """生成验证器并在沙盒中做 F2P 验证（base 必失败、修复后必通过）。

    每条耗时几乎全在 LLM 生成验证器上（单次数十秒到数分钟），沙盒执行只占 1~2 秒，
    因此条目级并发能近似线性压缩墙钟时间。每条默认带重试，避免一次克隆/网络抖动
    就把整条打成 ``failed``（重跑要从头再来一遍）。
    """
    settings = load_cfg(config, env, verbose=verbose, require_llm=True)

    async def main() -> int:
        from ..models import Category as _C
        from ..pipeline.context import RuntimeContext
        from ..pipeline.stages import PRVerifyStage, VerifyStage

        async with RuntimeContext.open(settings, exclusive=True) as ctx:
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
            else:
                repos = _resolve_repos(settings, repo, platform)
                targets = []
                for cfg in repos:
                    # 用量按仓库归属，守卫也得跟着换（单槽，与 eval 同一处理）
                    ctx.llm.budget_guard = ctx.budget_guard(cfg.slug)
                    targets.extend(
                        ctx.repo.list_items(
                            repo_slug=cfg.slug,
                            status=ItemStatus.EVALUATED,
                            limit=limit or 30,
                        )
                    )

            stage_issue = VerifyStage(ctx)
            stage_pr = PRVerifyStage(ctx)
            total = len(targets)
            if not total:
                emit("（没有待验证条目：只处理 status=evaluated；failed 条目请用 --key 指定）")
                if json_out:
                    emit_json(rows)
                return 0

            # 基础镜像前置检查（快速存在性判断，**不触发拉取**）。
            # 缺镜像时每条都要「克隆 → LLM 生成验证器 → 沙盒报错」白跑一遍，
            # 十几条叠起来既烧 token 又白等（pipekit 实测 15 条全栽在这同一条上）。
            # 放在「确认有目标条目」之后：空队列仍然安静退出。
            # ``--key`` 单条诊断不拦——那正是逐条定位问题用的路径。
            if not key:
                ready, img_reason = ctx.sandbox.image_ready(settings.sandbox.image)
                if not ready:
                    fail(
                        f"{img_reason}\n"
                        f"   先构建基础镜像再跑验证：docker build -t {settings.sandbox.image} docker/sandbox\n"
                        "   （或以 sandbox.runtime: local 运行，隔离性更弱、仅建议本机开发）"
                    )

            workers = max(1, concurrency or settings.verifier.concurrency)
            max_retries = max(0, retries if retries is not None else settings.verifier.item_retries)
            emit(
                f"待验证 {total} 条｜并发 {min(workers, total)}｜单条重试 {max_retries} 次"
                "（每条耗时几乎全在 LLM 生成验证器，请耐心等待）"
            )

            # 进度计数：并发下由 lock 保护，保证「第 N/M 条」单调不重复。
            done = 0
            lock = asyncio.Lock()
            sem = asyncio.Semaphore(workers)

            async def run_one(item) -> dict[str, Any] | None:
                nonlocal done
                async with sem:
                    # 整条包一层兜底：任何未捕获异常都不能让 gather 连累整批，
                    # 否则一条失败会把已经跑完的 N 条结果一起丢掉。
                    try:
                        ev = ctx.repo.latest_evaluation(item.key)
                        if ev is None:
                            emit(f"跳过 {item.key}：尚未评测")
                            return None
                        # 只有 Issue-FEATURE 不验证（没有缺陷可复现，只打标签等开发者）；
                        # PR-FEATURE 要照常验证——否则「只改文档冒充功能实现」的 PR 会
                        # 没有任何结论（envkit #18 实测）。与 process_repo 的口径一致。
                        if ev.category is not _C.BUG and item.item_type is ItemType.ISSUE:
                            emit(f"跳过 {item.key}：分类为 {ev.category.value}（Issue-FEATURE 不验证，只打标签）")
                            ctx.repo.set_item_status(item.key, ItemStatus.LABELED)
                            return None

                        stage = stage_issue if item.item_type is ItemType.ISSUE else stage_pr
                        started = time.monotonic()
                        async with lock:
                            emit(f"▶  {item.key} 开始验证（{time.strftime('%H:%M:%S')}）")
                        r = await _verify_with_retry(
                            lambda: stage.run(item, ev),
                            item_key=item.key,
                            max_retries=max_retries,
                        )
                        elapsed = time.monotonic() - started
                        row = {
                            "key": r.key,
                            "type": item.item_type.value,
                            "verifier": r.verifier_kind.value if r.verifier_kind else "-",
                            "reproduced": bool(r.verifier_result and r.verifier_result.reproducible),
                            "f2p": bool(r.verifier_result and r.verifier_result.f2p_satisfied),
                            "queued": r.queued.value if r.queued else "-",
                            "note": (r.skipped_reason or r.error or (r.notes[-1] if r.notes else ""))[:48],
                        }
                        async with lock:
                            done += 1
                            emit(
                                f"[{done}/{total}] {_verify_progress(r, elapsed)}"
                                f"（本条 {elapsed:.0f}s）"
                            )
                        return row
                    except Exception as exc:
                        from ..pipeline.stages import StageResult as _SR

                        log.exception("验证条目异常 %s", item.key)
                        err = _SR(key=item.key, error=f"{type(exc).__name__}: {exc}")
                        async with lock:
                            done += 1
                            emit(f"[{done}/{total}] ❌ {item.key} 异常：{(err.error or '')[:60]}")
                        return {
                            "key": item.key,
                            "type": item.item_type.value,
                            "verifier": "-",
                            "reproduced": False,
                            "f2p": False,
                            "queued": "-",
                            "note": (err.error or "")[:48],
                        }

            results = await asyncio.gather(*(run_one(i) for i in targets))
            rows.extend(r for r in results if r is not None)
        if json_out:
            emit_json(rows)
        else:
            print_table(rows, ["key", "type", "verifier", "reproduced", "f2p", "queued", "note"], title="验证结果")
        return 0

    run_async(main())


# 克隆/LLM 网络抖动导致的失败不值得让整条作废：重跑要重走生成+沙盒。
# 只重试这类「环境错误」，验证器自身写错（自错）由 runner 内部的修正回路处理，
# 不在这里重试——那只会重复花 LLM 的钱而不改变结论。
_TRANSIENT_ERROR_MARKERS = (
    "克隆失败",
    "GitError",
    "git 超时",
    "网络错误",
    "连接失败",
    "超时",
    "timed out",
    "timeout",
    "Connection",
    "LLM 服务错误",
    "LLM 请求失败",
)


async def _verify_with_retry(run, *, item_key: str, max_retries: int):
    """执行一次验证；失败且属环境错误时退避重试。

    ``run`` 是无参协程工厂（每次重试都要新建协程对象，协程不可复用）。
    重试用尽仍失败则返回一个带 ``error`` 的 StageResult，而不是抛异常——
    否则一条失败会经 ``asyncio.gather`` 的默认行为连累整批。
    """
    from ..pipeline.stages import StageResult

    delay = 2.0
    last_error = ""
    for attempt in range(1, max_retries + 2):
        try:
            return await run()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt > max_retries:
                break
            if not _is_transient(last_error):
                # 非环境错误（如配置错、条目状态异常）重试无意义，立即上抛由外层兜底
                break
            log.warning("验证失败（第 %d 次，将重试）：%s：%s", attempt, item_key, last_error[:200])
            emit(f"⚠️  {item_key} 第 {attempt} 次失败（{last_error.split(':', 1)[0]}），{delay:.0f}s 后重试")
            await asyncio.sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 2, 30.0)
    log.error("验证失败（已用尽重试）：%s：%s", item_key, last_error)
    return StageResult(key=item_key, error=last_error)


def _is_transient(error: str) -> bool:
    return any(marker in error for marker in _TRANSIENT_ERROR_MARKERS)


def _verify_progress(result, elapsed: float) -> str:
    """把一条 StageResult 压成一行进度摘要，让长跑时有实时反馈。"""
    if result.error:
        return f"❌ {result.key} 失败：{result.error[:60]}"
    if result.skipped_reason:
        return f"⏭  {result.key} 跳过：{result.skipped_reason[:60]}"
    vr = result.verifier_result
    kind = result.verifier_kind.value if result.verifier_kind else "-"
    if vr is None:
        return f"？  {result.key}（{kind}）无结果"
    mark = "✅" if vr.reproducible or vr.f2p_satisfied else "⚠️ "
    note = (result.notes[-1] if result.notes else vr.conclusion or "")[:40]
    return f"{mark} {result.key}（{kind}）复现={vr.reproducible} f2p={vr.f2p_satisfied} {note}"


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


@app.command("flush")
def flush(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    queue: str | None = typer.Option(None, "--queue", help="只 flush 指定队列：verify/fix_bug/fix_feature"),
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

        async with RuntimeContext.open(settings, exclusive=True) as ctx:
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
