"""``fissue onboard``：交互式初始化，与 WebUI 向导共用同一套内核。

为什么两个入口都要有：WebUI 向导需要有人打开浏览器、而且服务得先起来；无头机器（树莓派、
容器）上只有一个终端。两者写的是同一批文件、跑的是同一批探测，所以内核共用
（``fissue/web/setup.py``），这里只负责「问」与「显示」，**不另写一套写入逻辑**。

非交互用法（CI / 脚本）：

.. code-block:: console

    fissue onboard --yes --llm-api-key sk-xxx --llm-model deepseek-chat \\
        --repo John-is-playing/envkit --test-hint "python -m pytest -q"

写入位置：密钥 → ``.env``（逐行改写，注释保留）；仓库列表 → 覆盖层
``config.local.yaml``。人手写的 ``config.yaml`` 永不改动。
"""

from __future__ import annotations

import sys
from typing import Any

import typer

from ..errors import FissueError
from ..models import Platform
from ..web.setup import (
    RepoIn,
    apply_env,
    build_repo,
    env_file,
    overlay_file,
    probe_database,
    probe_llm,
    probe_platform,
    probe_repo,
    save_repos,
)
from .main import (
    CONFIG_OPT,
    ENV_OPT,
    VERBOSE_OPT,
    app,
    emit,
    fail,
    load_cfg,
    run_async,
)

_PLATFORMS = [p.value for p in Platform]

# 选项写成模块级常量（与 main.py / ops.py 同一惯例）：ruff 的 B008 不允许在参数默认值里
# 调用函数，而 `typer.Option(...)` 正是那种调用。
_LLM_KEY_OPT = typer.Option(None, "--llm-api-key", help="LLM API Key（写进 .env）")
_LLM_BASE_OPT = typer.Option(None, "--llm-base-url", help="OpenAI 兼容端点")
_LLM_MODEL_OPT = typer.Option(None, "--llm-model", help="主模型名")
_LLM_FALLBACK_OPT = typer.Option(None, "--llm-fallback-model", help="备用模型名")
_DB_URL_OPT = typer.Option(None, "--db-url", help="数据库连接串（不给则不动）")
_PLATFORM_OPT = typer.Option(None, "--platform", help="代码托管平台：github/gitee/atomgit/gitlab")
_TOKEN_OPT = typer.Option(None, "--token", help="该平台的 Token（不给则不动）")
_REPO_OPT = typer.Option(None, "--repo", help="owner/name（可重复多次）")
_TEST_HINT_OPT = typer.Option(None, "--test-hint", help="回归门用的测试命令")
_SINCE_DAYS_OPT = typer.Option(30, "--since-days", help="抓取时间窗（0 = 不限）")
_YES_OPT = typer.Option(False, "--yes", "-y", help="不提问：只用命令行给的值")
_CHECK_OPT = typer.Option(True, "--check/--no-check", help="是否真的探活（LLM / 数据库 / 平台）")
_SCAN_OPT = typer.Option(True, "--scan/--no-scan", help="配好之后是否立刻抓第一轮")
_SCAN_LIMIT_OPT = typer.Option(None, "--scan-limit", help="首轮最多抓多少条")


@app.command("onboard")
def onboard(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    llm_api_key: str | None = _LLM_KEY_OPT,
    llm_base_url: str | None = _LLM_BASE_OPT,
    llm_model: str | None = _LLM_MODEL_OPT,
    llm_fallback_model: str | None = _LLM_FALLBACK_OPT,
    db_url: str | None = _DB_URL_OPT,
    platform: str | None = _PLATFORM_OPT,
    token: str | None = _TOKEN_OPT,
    repo: list[str] | None = _REPO_OPT,
    test_hint: str | None = _TEST_HINT_OPT,
    since_days: int = _SINCE_DAYS_OPT,
    yes: bool = _YES_OPT,
    check: bool = _CHECK_OPT,
    scan: bool = _SCAN_OPT,
    scan_limit: int | None = _SCAN_LIMIT_OPT,
) -> None:
    """初始化 Fissue：模型 → 数据库 → 平台凭据 → 监控仓库 → 抓第一轮。"""
    settings = load_cfg(config, env, verbose=verbose)
    interactive = not yes
    if interactive and not sys.stdin.isatty():
        fail(
            "当前不是交互式终端：请加 --yes 并直接给出参数（见 `fissue onboard --help`），"
            "或改用 WebUI 的「初始化向导」。"
        )

    emit("🔧 Fissue 初始化向导（写入 .env 与覆盖层，不动 config.yaml）")
    emit("")

    env_values: dict[str, str] = {}

    # -- 1. 模型 ---------------------------------------------------------
    emit("── 1/5 AI 模型 ──")
    base_url = _pick(llm_base_url, interactive, "LLM 端点", settings.llm.base_url)
    model = _pick(llm_model, interactive, "主模型", settings.llm.model)
    fallback = _pick(
        llm_fallback_model, interactive, "备用模型（可留空）", settings.llm.fallback_model or ""
    )
    key = llm_api_key
    if key is None and interactive:
        key = _ask_secret("LLM API Key（留空 = 不改动）", current=settings.llm.api_key)
    if key:
        env_values["FISSUE_LLM_API_KEY"] = key
    if base_url:
        env_values["FISSUE_LLM_BASE_URL"] = base_url
    if model:
        env_values["FISSUE_LLM_MODEL"] = model
    if fallback:
        env_values["FISSUE_LLM_FALLBACK_MODEL"] = fallback

    if check:
        emit("  探测中…（真的发一次最小请求）")
        result = run_async(
            probe_llm(
                base_url=base_url or settings.llm.base_url,
                model=model or settings.llm.model,
                api_key=key or settings.llm.api_key,
            )
        )
        _report_probe(result, ok_hint=f"连通：{result.get('model')}（{result.get('seconds')}s）")

    # -- 2. 数据库 -------------------------------------------------------
    emit("")
    emit("── 2/5 数据库 ──")
    chosen_db = _pick(db_url, interactive, "连接串", settings.database.url)
    if chosen_db and chosen_db != settings.database.url:
        env_values["FISSUE_DATABASE_URL"] = chosen_db
    if check:
        _report_probe(probe_database(chosen_db or settings.database.url))

    # -- 3. 平台凭据 -----------------------------------------------------
    emit("")
    emit("── 3/5 平台凭据（可跳过：匿名只能读公开仓库，且无法提 PR）──")
    chosen_platform = platform
    if chosen_platform is None and interactive:
        chosen_platform = _ask_choice("平台", _PLATFORMS, default="github")
    chosen_platform = chosen_platform or ""
    plat_enum: Platform | None = None
    if chosen_platform:
        try:
            plat_enum = Platform(chosen_platform)
        except ValueError:
            fail(f"平台取值非法：{chosen_platform!r}；可选：{'、'.join(_PLATFORMS)}")
    chosen_token = token
    if plat_enum is not None and chosen_token is None and interactive:
        chosen_token = _ask_secret(f"{plat_enum.value} Token（留空 = 不改动）")
    if plat_enum is not None and chosen_token:
        env_values[f"FISSUE_{plat_enum.value.upper()}_TOKEN"] = chosen_token
        if check:
            _report_probe(
                run_async(probe_platform(settings, plat_enum, chosen_token)),
                ok_hint="校验通过",
            )

    # -- 4. 监控仓库 -----------------------------------------------------
    emit("")
    emit("── 4/5 监控仓库 ──")
    specs = list(repo or [])
    if not specs and interactive:
        emit("  逐个填写，留空结束（owner 与 name 分开写）")
        while True:
            owner = _ask("  owner（留空结束）", default="")
            if not owner:
                break
            name = _ask("  name", default="")
            if not name:
                break
            specs.append(f"{owner}/{name}")
    if not specs and not settings.repos:
        emit("  ⚠️ 没有仓库：流水线没有输入，抓取与评测都不会做任何事。")

    new_repos: list[Any] = []
    for spec in specs:
        owner, _, name = spec.partition("/")
        if not owner or not name:
            fail(f"--repo 需要 owner/name 形式，收到：{spec!r}")
        candidate = build_repo(
            RepoIn(
                platform=plat_enum or Platform.GITHUB,
                owner=owner,
                name=name,
                test_hint=test_hint if test_hint is not None else "python -m pytest -q",
                since_days=since_days,
            )
        )
        if check and plat_enum is not None:
            _report_probe(
                run_async(probe_repo(settings, candidate.platform, candidate.owner, candidate.name)),
                ok_hint=f"存在：{owner}/{name}",
            )
        new_repos.append(candidate)
        emit(f"  + {candidate.platform.value}:{candidate.slug}")

    # -- 5. 写入 ---------------------------------------------------------
    emit("")
    emit("── 5/5 写入 ──")
    if not interactive or typer.confirm("确认写入以上配置？", default=True):
        return _write(settings, env_values, new_repos, scan=scan, scan_limit=scan_limit)
    emit("已取消，未写入任何文件。")


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _pick(value: str | None, interactive: bool, label: str, default: str | None) -> str | None:
    """命令行给了就用；交互式则问（回车接受当前值）；``--yes`` 且没给 → **返回 None**。

    最后那条很关键：非交互模式下把「当前生效值」写进 .env 会把配置来源悄悄挪一层
    （原先由 config.yaml 提供，之后变成 .env 覆盖），用户没要求改却被改了。
    """
    if value is not None:
        return value
    if interactive:
        return _ask(f"{label}", default=default or "")
    return None


def _ask(label: str, *, default: str = "") -> str:
    return str(typer.prompt(f"  {label}", default=default, show_default=bool(default))).strip()


def _ask_secret(label: str, *, current: str | None = None) -> str:
    """问一个密钥：**默认留空 = 不改动**，避免手滑把已有的 key 抹掉。"""
    hint = f"{label}［已有，留空不改］" if current else label
    return str(typer.prompt(f"  {hint}", default="", hide_input=True, show_default=False)).strip()


def _ask_choice(label: str, choices: list[str], *, default: str) -> str:
    return str(
        typer.prompt(f"  {label} ({'/'.join(choices)})", default=default, show_default=True)
    ).strip()


def _report_probe(result: dict[str, Any], *, ok_hint: str = "通过") -> None:
    if result.get("ok"):
        emit(f"  ✅ {ok_hint}")
    else:
        # 探活失败**不中断**：离线/被墙/证书问题都可能，而配置本身可能是对的。
        emit(f"  ⚠️ 探活失败（不中断）：{result.get('error') or result}")


def _write(
    settings: Any,
    env_values: dict[str, str],
    new_repos: list[Any],
    *,
    scan: bool,
    scan_limit: int | None,
) -> None:
    """落盘 + 重载 +（可选）首轮抓取。与 WebUI 向导共用同一套写入函数。"""
    if env_values:
        written = apply_env(env_file(settings), env_values)
        emit(f"  ✅ 已写入 .env：{'、'.join(written)}")
        emit(f"     路径：{env_file(settings)}")
    else:
        emit("  （.env 无改动）")

    if new_repos:
        existing = {(r.platform, r.slug) for r in settings.repos}
        merged = [*settings.repos, *(r for r in new_repos if (r.platform, r.slug) not in existing)]
        save_repos(overlay_file(settings), settings, merged)
        emit(f"  ✅ 已写入覆盖层（仓库共 {len(merged)} 个）")
        emit(f"     路径：{overlay_file(settings)}")
    else:
        emit("  （仓库列表无改动）")

    # 重新加载：后续的抓取要用新配置（新库、新 token、新仓库）。
    from ..config import load_settings

    fresh = load_settings(
        getattr(settings, "config_source", None) or getattr(settings, "config_path", None),
        getattr(settings, "env_source", None) or getattr(settings, "env_path", None),
    )
    emit("")
    if not fresh.enabled_repos():
        emit("（没有启用中的仓库，跳过首轮抓取）")
        return
    if not scan:
        emit("（已按 --no-scan 跳过首轮抓取）")
        return
    _first_scan(fresh, limit=scan_limit)


def _first_scan(settings: Any, *, limit: int | None) -> None:
    """抓第一轮。用与 ``fissue fetch`` 相同的独占上下文——它要写库、要花平台配额。"""
    from ..pipeline.context import RuntimeContext
    from ..pipeline.fetcher import Fetcher

    async def main() -> None:
        async with RuntimeContext.open(settings, exclusive=True) as ctx:
            ctx.init_db()
            repos = settings.enabled_repos()
            emit(f"开始首轮抓取：{len(repos)} 个仓库…")
            stats = await Fetcher(ctx).fetch_many(repos, limit=limit)
            emit(
                f"✅ 首轮完成：拉取 {stats.fetched}，新增 {stats.created}，"
                f"更新 {stats.updated}，未变 {stats.unchanged}"
                + (f"（错误：{stats.error}）" if stats.error else "")
            )

    try:
        run_async(main())
    except FissueError as exc:
        # 抓取失败不该抹掉「配置已写好」这个事实——分开说清楚。
        emit(f"⚠️ 首轮抓取未完成：{exc}")
        emit("   配置已经写好，稍后可以单独执行 `fissue fetch` 重试。")


__all__ = ["onboard"]
