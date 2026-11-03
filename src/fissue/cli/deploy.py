"""``fissue host`` / ``fissue deploy``：登记服务器，把仓库部署上去并运维它。

设计取舍
--------
* **凭据只在两个地方出现**：``host add`` 读进来就用 :class:`CredentialBox` 加密，
  之后库里只有密文；``DeploymentRunner`` 在建连接时解出来，用完随对象丢弃。
  本模块任何输出都不打印凭据（``--json`` 也一样，只回显 ``auth_method`` + 指纹）。
* **``deploy plan`` 与 ``deploy apply`` 分开**：计划会落库，可以先看再执行
  （``--plan-file`` 复用手工改过的计划）。虽然默认路径是端到端全自动，但
  「能停下来看一眼」不该被拿走。
* **危险动作不偷偷放行**：``apply --allow-dangerous`` 才能让 ``sudo`` 之类过闸；
  即使开了，``rm -rf /`` 这类仍然一律拒绝（见 ``deploy/guard.py``）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from ..config import Settings
from ..deploy.guard import GuardError
from ..deploy.planner import PlannerError
from ..deploy.runner import DeployError, DeploymentRunner
from ..deploy.schema import AuthMethod, DeployPlan, DeployStatus, HostSpec
from ..deploy.secretbox import CredentialBox, SecretBoxError
from ..deploy.service import DeployService
from ..errors import FissueError
from ..store.deploy_repo import DeployRepository
from ..store.tables import DeployHostRow, DeploymentRow
from .main import (
    CONFIG_OPT,
    ENV_OPT,
    JSON_OPT,
    VERBOSE_OPT,
    deploy_app,
    emit,
    emit_json,
    fail,
    host_app,
    load_cfg,
    print_table,
    run_async,
)

# ---------------------------------------------------------------------------
# 公共装配
# ---------------------------------------------------------------------------


def _repo_and_box(settings: Settings) -> tuple[DeployRepository, CredentialBox]:
    """构造部署仓储与凭据盒。

    这里**不**复用 ``RuntimeContext``：登记主机、看部署历史都不需要 LLM 与沙盒，
    为一条 ``host list`` 去构造沙盒管理器纯属浪费（而且沙盒不可用时它会抛）。
    """
    from ..store.db import build_database

    db = build_database(settings)
    db.create_all()
    return DeployRepository(db), CredentialBox.from_settings(settings)


def _ensure_deploy_enabled(settings: Settings, *, override: bool = False) -> None:
    if not settings.deploy.enabled and not override:
        fail(
            "部署能力未开启：在 config.yaml 里设置 deploy.enabled=true "
            "（这是能改远端机器的能力，默认关闭）"
        )


def _read_credential(
    *,
    key_file: str | None,
    password_stdin: bool,
    auth: str,
) -> tuple[AuthMethod, str]:
    """读取凭据原文。**只在这里**接触明文。"""
    if auth == "password" or password_stdin:
        if password_stdin:
            secret = sys.stdin.readline().rstrip("\n")
        else:
            secret = typer.prompt("密码", hide_input=True)
        if not secret:
            fail("密码为空")
        return AuthMethod.PASSWORD, secret

    path = Path(key_file).expanduser() if key_file else _default_key_path()
    if path is None or not path.exists():
        fail("找不到私钥：请用 --key-file 指定，或用 --password-stdin 走密码登录")
    text = path.read_text(encoding="utf-8")
    if "PRIVATE KEY" not in text:
        fail(f"{path} 看起来不是私钥文件")
    return AuthMethod.KEY, text


def _host_row(rows: list[DeployHostRow], name: str) -> DeployHostRow:
    for row in rows:
        if row.name == name:
            return row
    fail(f"没有这台主机：{name}")
    raise AssertionError("unreachable")  # pragma: no cover - fail() 已退出


def _default_key_path() -> Path | None:
    """找本机默认私钥（与 OpenSSH 的查找顺序一致）。

    找不到就返回 None 让调用方要求 ``--key-file``：**不要**在这里挑一把「看起来像」的
    密钥去猜，猜错的表现是远端认证失败，而用户会以为是服务器的问题。
    """
    home = Path.home()
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        candidate = home / ".ssh" / name
        if candidate.is_file():
            return candidate
    return None


def _host_view(row: DeployHostRow, repo: DeployRepository) -> dict[str, Any]:
    """对外展示用：**不含凭据**，只说明「用什么方式认证」和「指纹是否已钉住」。"""
    spec: HostSpec = repo.host_spec(row)
    return {
        "name": spec.name,
        "address": f"{spec.user}@{spec.address}:{spec.port}",
        "auth": spec.auth_method.value,
        "fingerprint": spec.host_fingerprint or "（未建立，下次连接时 TOFU 记录）",
        "enabled": "yes" if spec.enabled else "no",
        "app_root": row.app_root,
        "last_seen_at": row.last_seen_at or "",
    }


# ---------------------------------------------------------------------------
# fissue host
# ---------------------------------------------------------------------------


@host_app.command("add")
def host_add(
    name: str = typer.Argument(..., help="主机别名（CLI/Web 都用它引用）"),
    address: str = typer.Option(..., "--address", "-a", help="IP 或域名"),
    user: str = typer.Option("root", "--user", "-u", help="登录用户"),
    port: int = typer.Option(22, "--port", "-p", help="SSH 端口"),
    key_file: str | None = typer.Option(None, "--key-file", "-k", help="私钥文件路径"),
    password_stdin: bool = typer.Option(
        False, "--password-stdin", help="从 stdin 读密码（脚本用，避免密码进 history）"
    ),
    auth: str = typer.Option("key", "--auth", help="key | password"),
    app_root: str | None = typer.Option(None, "--app-root", help="该机上的发布根目录"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """登记（或覆盖）一台可部署的服务器。"""
    settings = load_cfg(config, env, verbose=verbose)
    method, credential = _read_credential(key_file=key_file, password_stdin=password_stdin, auth=auth)
    repo, box = _repo_and_box(settings)
    sealed = box.seal(credential)
    del credential
    app_root = app_root or settings.deploy.app_root
    if not app_root.startswith("/"):
        fail(f"--app-root 必须是绝对路径：{app_root}")
    host_id = repo.upsert_host(
        name=name,
        address=address,
        credential=sealed,
        port=port,
        user=user,
        auth_method=method,
        app_root=app_root,
    )
    emit(f"✅ 已登记主机 {name} ({user}@{address}:{port}, {method.value}) id={host_id}")
    emit("提示：首次 `fissue host ping` 会把主机指纹钉住，之后不匹配即拒连。")


@host_app.command("list")
def host_list(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """列出已登记的主机（不含凭据）。"""
    settings = load_cfg(config, env, verbose=verbose)
    repo, _box = _repo_and_box(settings)
    rows = repo.list_hosts()
    views = [_host_view(row, repo) for row in rows]
    if as_json:
        emit_json(views)
        return
    print_table(
        views,
        ["name", "address", "auth", "fingerprint", "enabled", "app_root", "last_seen_at"],
        title="部署主机",
    )


@host_app.command("show")
def host_show(
    name: str = typer.Argument(...),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """看一台主机的登记信息。"""
    settings = load_cfg(config, env, verbose=verbose)
    repo, _box = _repo_and_box(settings)
    row = _host_row(repo.list_hosts(), name)
    view = _host_view(row, repo)
    if as_json:
        emit_json(view)
    else:
        for key, value in view.items():
            emit(f"{key:14} {value}")


@host_app.command("remove")
def host_remove(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="不再确认"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """删除主机登记。

    **不会**碰远端：服务还在跑、文件还在盘上。要停服务请先 ``deploy destroy``。
    删除会连带删掉该主机的部署历史与审计流水——这些是排障要用的，所以默认要确认。
    """
    settings = load_cfg(config, env, verbose=verbose)
    repo, _box = _repo_and_box(settings)
    row = _host_row(repo.list_hosts(), name)
    if not yes:
        typer.confirm(
            f"删除主机 {name} 会同时删掉它的部署历史与审计记录（远端不受影响），继续？",
            abort=True,
        )
    repo.remove_host(name)
    del row
    emit(f"✅ 已删除主机登记：{name}")


@host_app.command("ping")
def host_ping(
    name: str = typer.Argument(...),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """连一下目标机，回显基本信息；首次连接会把主机指纹钉住。"""
    settings = load_cfg(config, env, verbose=verbose)
    repo, _box = _repo_and_box(settings)
    runner = DeploymentRunner(settings, repo)
    info = run_async(runner.ping(name))
    if as_json:
        emit_json(info)
        return
    emit(f"✅ {info['host']} 可连接")
    for key in ("uptime", "disk", "systemd", "fingerprint_pinned"):
        if info.get(key):
            emit(f"{key:18} {info[key]}")


@host_app.command("enable")
def host_enable(
    name: str = typer.Argument(...),
    disable: bool = typer.Option(False, "--disable", help="改为停用"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """启用/停用一台主机（停用后不允许部署到它）。"""
    settings = load_cfg(config, env, verbose=verbose)
    repo, _box = _repo_and_box(settings)
    if not repo.set_host_enabled(name, not disable):
        fail(f"没有这台主机：{name}")
    emit(f"✅ {name} 已{'停用' if disable else '启用'}")


# ---------------------------------------------------------------------------
# fissue deploy
# ---------------------------------------------------------------------------


def _print_plan(plan: DeployPlan) -> None:
    emit(f"仓库      {plan.repo_slug}@{plan.ref}" + (f" ({plan.commit_sha[:12]})" if plan.commit_sha else ""))
    emit(f"应用      {plan.app.name}  runtime={plan.app.runtime}  service={plan.app.service_name or '-'}")
    if plan.app.port:
        emit(f"端口      {plan.app.port}")
    emit(f"步骤      {len(plan.steps)}")
    for seq, step in enumerate(plan.steps, start=1):
        emit(f"  {seq:>2}. [{step.kind.value}] {step.id}: {step.summary()}")
    emit(f"健康探针  {len(plan.health)}")
    for spec in plan.health:
        emit(f"  - {spec.url or f'tcp:{spec.port}'}")


@deploy_app.command("plan")
def deploy_plan(
    slug: str = typer.Argument(..., help="owner/name"),
    host: str = typer.Option(..., "--host", "-H", help="目标主机别名"),
    ref: str | None = typer.Option(None, "--ref", help="分支/标签，默认仓库默认分支"),
    platform: str | None = typer.Option(None, "--platform", help="github | gitee | atomgit | gitlab"),
    plan_file: str | None = typer.Option(None, "--plan-file", help="复用手写计划（JSON）"),
    allow_dangerous: bool = typer.Option(False, "--allow-dangerous", help="允许 sudo 之类的高危动作"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """只产出并校验部署计划，不连目标机（结果落库，便于事后查「它当时想干什么」）。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=not plan_file)
    _ensure_deploy_enabled(settings)
    repo, _box = _repo_and_box(settings)
    service = DeployService(settings, repo)
    try:
        deployment_id, plan, warning = run_async(
            service.plan_only(
                slug=slug,
                host=host,
                ref=ref,
                platform=platform,
                plan_file=plan_file,
                allow_dangerous=allow_dangerous or None,
            )
        )
    except (DeployError, PlannerError, GuardError, FissueError, SecretBoxError) as exc:
        fail(str(exc))
        raise AssertionError("unreachable") from None  # pragma: no cover

    if warning:
        emit("⚠️ 计划能解析，但过不了安全校验（apply 会被拒绝）：", style="bold yellow")
        emit(f"   {warning}", style="yellow")
    emit(f"✅ 计划已保存：deployment #{deployment_id}")
    if as_json:
        emit_json({"deployment_id": deployment_id, "plan": plan.dump(), "warning": warning})
    else:
        _print_plan(plan)


@deploy_app.command("apply")
def deploy_apply(
    slug: str = typer.Argument(..., help="owner/name"),
    host: str = typer.Option(..., "--host", "-H"),
    ref: str | None = typer.Option(None, "--ref"),
    platform: str | None = typer.Option(None, "--platform"),
    plan_file: str | None = typer.Option(None, "--plan-file"),
    allow_dangerous: bool = typer.Option(False, "--allow-dangerous"),
    accept_new_host_key: bool = typer.Option(
        True, "--accept-new-host-key/--strict-host-key", help="首次连接是否接受未知主机公钥"
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="本地源码目录（只在复用 --plan-file 时需要；不给就拒绝执行，"
        "以免把控制端当前目录传到别人服务器上）",
    ),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """端到端部署：生成计划 → 校验 → 上传 → 起服务 → 独立证据判定（失败自动回滚）。"""
    settings = load_cfg(config, env, verbose=verbose, require_llm=not plan_file)
    _ensure_deploy_enabled(settings)
    repo, _box = _repo_and_box(settings)
    service = DeployService(settings, repo)

    if plan_file and not source:
        # 复用计划时没有仓库可克隆，缺了源码目录就只能传 cwd——那正是最危险的行为，
        # 所以在连机器之前就拒绝，而不是让它「看起来能跑」。
        fail("使用 --plan-file 时必须同时给 --source（本地源码目录），避免误传当前目录")

    if not as_json:
        emit(f"开始部署 {slug} → {host} ……")
    try:
        report = run_async(
            service.apply(
                slug=slug,
                host=host,
                ref=ref,
                platform=platform,
                plan_file=plan_file,
                allow_dangerous=allow_dangerous or None,
                accept_new_host_key=accept_new_host_key,
                source_dir=Path(source).expanduser() if source else None,
            )
        )
    except (DeployError, PlannerError, GuardError, SecretBoxError, FissueError) as exc:
        fail(str(exc))
        raise AssertionError("unreachable") from None  # pragma: no cover

    if as_json:
        emit_json(report.model_dump(mode="json"))
    else:
        for step in report.steps:
            mark = "✅" if step.status.value == "ok" else "❌"
            emit(f"{mark} {step.step_id}  exit={step.exit_code}" + (f"  {step.error}" if step.error else ""))
        emit(f"状态      {report.status.value}")
        if report.url:
            emit(f"地址      {report.url}")
        health = report.health.get("detail", "")
        if health:
            emit(f"证据      {health}")
    if report.status is not DeployStatus.SUCCESS:
        fail(f"部署未成功：{report.status.value}" + (f"（{report.error}）" if report.error else ""), code=2)


def _deployment_view(repo: DeployRepository, row: DeploymentRow) -> dict[str, Any]:
    host = repo.get_host_by_id(int(row.host_id))
    return {
        "id": row.id,
        "host": host.name if host else f"id={row.host_id}",
        "repo": row.repo_slug,
        "ref": row.ref,
        "status": row.status,
        "release": row.release or "",
        "url": row.url or "",
        "created_at": row.created_at,
        "finished_at": row.finished_at or "",
        "error": row.error or "",
    }


@deploy_app.command("status")
def deploy_status(
    deployment_id: int | None = typer.Argument(None, help="部署 id；省略则列出最近的部署"),
    limit: int = typer.Option(20, "--limit", "-n"),
    host: str | None = typer.Option(None, "--host", "-H", help="只看某台主机"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """看一次部署的详情，或列出最近的部署。"""
    settings = load_cfg(config, env, verbose=verbose)
    repo, _box = _repo_and_box(settings)

    if deployment_id is None:
        host_id = None
        if host:
            host_id = int(_host_row(repo.list_hosts(), host).id)
        rows = repo.list_deployments(host_id=host_id, limit=limit)
        views = [_deployment_view(repo, row) for row in rows]
        if as_json:
            emit_json(views)
        else:
            print_table(
                views,
                ["id", "host", "repo", "ref", "status", "release", "url", "created_at"],
                title="部署记录",
            )
        return

    row = repo.get_deployment(deployment_id)
    if row is None:
        fail(f"没有这条部署记录：{deployment_id}")
    view = _deployment_view(repo, row)
    steps = [
        {
            "seq": step.seq,
            "step_id": step.step_id,
            "kind": step.kind,
            "status": step.status,
            "exit_code": step.exit_code,
            "error": step.error or "",
        }
        for step in repo.steps_of(deployment_id)
    ]
    events = [f"[{e.created_at}] {e.level}: {e.message}" for e in repo.events_of(deployment_id)]
    if as_json:
        emit_json({**view, "steps": steps, "events": events, "health": row.health_json})
        return
    emit(f"部署 #{view['id']}  {view['status']}")
    emit(f"仓库   {view['repo']}@{view['ref']}")
    emit(f"主机   {view['host']}")
    emit(f"发布   {view['release']}  (上一版 {row.previous_release or '-'})")
    if view["url"]:
        emit(f"地址   {view['url']}")
    if view["error"]:
        emit(f"错误   {view['error']}", style="red")
    print_table(steps, ["seq", "step_id", "kind", "status", "exit_code", "error"], title="步骤")
    for line in events:
        emit(line)


@deploy_app.command("logs")
def deploy_logs(
    deployment_id: int = typer.Argument(...),
    step: str | None = typer.Option(None, "--step", help="只看某一步的尾部输出"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """打印某次部署的步骤输出尾部与审计流水。"""
    settings = load_cfg(config, env, verbose=verbose)
    repo, _box = _repo_and_box(settings)
    if repo.get_deployment(deployment_id) is None:
        fail(f"没有这条部署记录：{deployment_id}")
    for row in repo.steps_of(deployment_id):
        if step and row.step_id != step:
            continue
        emit(f"── [{row.seq}] {row.step_id} ({row.kind}) {row.status} exit={row.exit_code}")
        if row.command:
            emit(f"   $ {row.command}")
        if row.output_tail:
            emit(row.output_tail)
    emit("── 审计流水")
    for event in repo.events_of(deployment_id):
        emit(f"[{event.created_at}] {event.level}: {event.message}")


@deploy_app.command("rollback")
def deploy_rollback(
    deployment_id: int = typer.Argument(...),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """把服务切回上一次成功部署的 release，并重新做健康判定。"""
    settings = load_cfg(config, env, verbose=verbose)
    _ensure_deploy_enabled(settings)
    repo, _box = _repo_and_box(settings)
    runner = DeploymentRunner(settings, repo)
    report = run_async(runner.rollback(deployment_id))
    if as_json:
        emit_json(report.model_dump(mode="json"))
        return
    emit(f"状态  {report.status.value}")
    emit(f"证据  {report.health.get('detail', '')}")
    if report.status is not DeployStatus.ROLLED_BACK:
        fail(f"回滚未成功：{report.status.value}" + (f"（{report.error}）" if report.error else ""))


@deploy_app.command("destroy")
def deploy_destroy(
    deployment_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """停服务并删除 release 目录（**保留** shared/，数据与 .env 在里面）。"""
    settings = load_cfg(config, env, verbose=verbose)
    _ensure_deploy_enabled(settings)
    repo, _box = _repo_and_box(settings)
    row = repo.get_deployment(deployment_id)
    if row is None:
        fail(f"没有这条部署记录：{deployment_id}")
    if not yes:
        typer.confirm(
            f"停止服务并删除 {row.repo_slug} 在 {row.app_root} 下的 releases（shared 保留），继续？",
            abort=True,
        )
    runner = DeploymentRunner(settings, repo)
    report = run_async(runner.destroy(deployment_id))
    emit(f"✅ 已下线：{report.repo_slug}（deployment #{report.deployment_id}）")