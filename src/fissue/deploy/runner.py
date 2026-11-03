"""部署执行器：把一份 :class:`~fissue.deploy.schema.DeployPlan` 真正跑在远端。

三条不可让步的规矩（见 ``docs/DEPLOY.md`` §1 §7）：

1. **执行前先校验整份计划**。``guard.validate_plan`` 不过就一步都不跑——「跑到一半
   才发现第 20 步是 ``rm -rf /``」不是安全策略。
2. **成功由独立证据判定**。步骤退出码只是必要条件；收尾必须再验三样：
   systemd 说这个 unit 是 active、端口在 ``ss`` 里真的在听、以及**从控制端发起**的
   健康探针通。远端自己 ``curl localhost`` 成功什么也证明不了。
3. **失败就停，不「尽力而为」**。半启动的服务比没部署更糟：它占着端口、报着
   一半的错、还让人以为旧版本还在。停下 + 回滚 + 重新探针。

SSH 是阻塞的（paramiko），这里的 async 方法把每段远端操作丢进
``asyncio.to_thread``，避免把 FastAPI 的事件循环钉死。
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..config import Settings
from ..errors import FissueError
from ..logging_setup import get_logger
from ..net import httpx_verify
from ..store.deploy_repo import DeployRepository
from ..store.tables import DeployHostRow, DeploymentRow
from . import guard
from .schema import (
    AppSpec,
    DeployPlan,
    DeployStatus,
    DeployStep,
    ExecSpec,
    ProbeSpec,
    ServiceAction,
    ServiceSpec,
    StepResult,
    StepStatus,
    UploadSpec,
    WriteFileSpec,
)
from .secretbox import CredentialBox
from .ssh import SSHChannel, SSHTarget, connect

log = get_logger(__name__)


class DeployError(FissueError):
    """部署无法继续（计划非法、主机不可用、前置条件不满足）。"""


class DeploymentReport(BaseModel):
    """一次部署/回滚的结果。CLI 打印它，Web 返回它的 JSON。"""

    deployment_id: int
    host: str
    repo_slug: str
    status: DeployStatus
    release: str | None = None
    previous_release: str | None = None
    service_name: str | None = None
    url: str | None = None
    steps: list[StepResult] = Field(default_factory=list)
    health: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    rolled_back: bool = False


# ---------------------------------------------------------------------------
# 路径与命名
# ---------------------------------------------------------------------------


def app_dir_name(repo_slug: str) -> str:
    """``owner/name`` → 远端目录名。

    只保留 ``[A-Za-z0-9._-]``：它会被拼进 ``mkdir`` / ``ln`` 命令，也是「计划里
    的字符串直接变成路径」那类事故的入口。
    """
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in repo_slug.replace("/", "-"))
    return cleaned.strip("-") or "app"


def release_stamp(now: datetime | None = None) -> str:
    """release 目录名：UTC 紧凑时间戳，可排序。"""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


class RemoteLayout(BaseModel):
    """远端目录布局（docs/DEPLOY.md §3）。"""

    base: str            # <app_root>/<app-dir>
    releases: str
    shared: str
    current: str

    @classmethod
    def of(cls, app_root: str, repo_slug: str) -> RemoteLayout:
        base = f"{app_root.rstrip('/')}/{app_dir_name(repo_slug)}"
        return cls(
            base=base,
            releases=f"{base}/releases",
            shared=f"{base}/shared",
            current=f"{base}/current",
        )

    def release_dir(self, release: str) -> str:
        return f"{self.releases}/{release}"


# ---------------------------------------------------------------------------
# 控制端健康探针
# ---------------------------------------------------------------------------


async def probe(spec: ProbeSpec) -> dict[str, Any]:
    """从**控制端**验证服务是否真的可达。

    返回结构固定为 ``{"target", "ok", "detail", "elapsed"}``：上层不管几种探针
    都按同一形状汇总，省得每种探针各写一套「算不算成功」的判断。
    """
    started = time.monotonic()
    out: dict[str, Any] = {"target": spec.url or f"tcp:{spec.port}", "ok": False, "detail": "", "elapsed": 0.0}
    try:
        if spec.url:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=spec.timeout,
                # 与其余客户端一致：TLS 走操作系统信任库，否则企业代理/抓包工具做的
                # 中间人环境里探针必然失败——而我们会把它误判成「服务没起来」。
                verify=httpx_verify(),
            ) as client:
                resp = await client.get(spec.url)
            ok = resp.status_code == (spec.expect_status or 200)
            detail = f"HTTP {resp.status_code}"
            if ok and spec.expect_contains and spec.expect_contains not in resp.text:
                ok = False
                detail += "，但响应体不含期望内容"
            out.update(ok=ok, detail=detail)
        else:
            assert spec.port is not None  # schema 已保证二选一
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(_probe_host(spec), spec.port), timeout=spec.timeout
            )
            writer.close()
            # 关闭阶段的报错不影响「连得上」这个判定，压掉即可。
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            del reader
            out.update(ok=True, detail=f"TCP {spec.port} 可连接")
    except Exception as exc:  # noqa: BLE001 - 探针失败的种类很多，统一记原因
        out.update(ok=False, detail=f"{type(exc).__name__}: {exc}")
    out["elapsed"] = round(time.monotonic() - started, 3)
    return out


def _probe_host(spec: ProbeSpec) -> str:
    """探针主机：优先取 url 里的主机名，其次由调用方在 port 探针上补 ``ProbeSpec``。

    ``ProbeSpec`` 只有一个 ``port`` 字段（目标主机是部署上下文给的），所以 TCP
    探针必须由 runner 先填好主机——见 :meth:`DeploymentRunner._probe_all`。
    """
    if spec.url:
        return str(httpx.URL(spec.url).host)
    host = getattr(spec, "_host", None)
    if not host:
        raise DeployError("TCP 探针缺少目标主机")
    return str(host)


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


class DeploymentRunner:
    """执行一份计划，并把每一步的真实结果写进库。"""

    def __init__(
        self,
        settings: Settings,
        repo: DeployRepository,
        *,
        box: CredentialBox | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.cfg = settings.deploy
        self.box = box or CredentialBox.from_settings(settings)

    # -- 对外入口 ---------------------------------------------------------

    async def run(
        self,
        *,
        host_name: str,
        plan: DeployPlan,
        source_dir: Path,
        accept_new_host_key: bool | None = None,
        allow_dangerous: bool | None = None,
    ) -> DeploymentReport:
        """全自动跑一次部署：校验 → 建记录 → 执行 → 切换 → 健康判定（失败则回滚）。"""
        if not self.cfg.enabled:
            raise DeployError("部署能力未开启：请设置 deploy.enabled=true")

        host = self._host(host_name)
        layout = RemoteLayout.of(host.app_root or self.cfg.app_root, plan.repo_slug)

        # 契约第一条：执行前校验整份计划。顺序很重要——先把计划看死，再连机器。
        try:
            guard.validate_plan(
                plan,
                max_steps=self.cfg.max_plan_steps,
                allow_dangerous=self.cfg.allow_dangerous if allow_dangerous is None else allow_dangerous,
                app_root=host.app_root or self.cfg.app_root,
            )
        except FissueError as exc:
            raise DeployError(f"计划未通过校验，未执行任何步骤：{exc}") from exc

        previous = self.repo.latest_deployment(host_id=int(host.id), repo_slug=plan.repo_slug)
        deployment_id = self.repo.create_deployment(
            host_id=int(host.id),
            repo_slug=plan.repo_slug,
            ref=plan.ref,
            commit_sha=plan.commit_sha,
            plan=plan.dump(),
            app_root=host.app_root or self.cfg.app_root,
            previous_release=previous.release if previous else None,
        )
        self.repo.set_status(deployment_id, DeployStatus.RUNNING)
        self.repo.add_event(deployment_id, f"开始部署 {plan.repo_slug}@{plan.ref} 到 {host.name}")

        release = release_stamp()
        self.repo.set_release(
            deployment_id, release=release, previous=previous.release if previous else None
        )
        self.repo.set_service_info(
            deployment_id,
            service_name=plan.app.service_name,
            url=self._plan_url(plan, host),
        )

        report = DeploymentReport(
            deployment_id=deployment_id,
            host=host.name,
            repo_slug=plan.repo_slug,
            status=DeployStatus.RUNNING,
            release=release,
            previous_release=previous.release if previous else None,
            service_name=plan.app.service_name,
            url=self._plan_url(plan, host),
        )

        target = self._target(host)
        accept_new = self.cfg.accept_new_host_key if accept_new_host_key is None else accept_new_host_key
        dangerous = self.cfg.allow_dangerous if allow_dangerous is None else allow_dangerous

        try:
            channel = await asyncio.to_thread(connect, target, accept_new_host_key=accept_new)
        except FissueError as exc:
            return self._fail(report, None, f"连接失败：{exc}")

        try:
            await self._prepare(channel, host, layout, release, deployment_id)
            ok = await self._run_steps(
                channel=channel,
                plan=plan,
                layout=layout,
                release=release,
                deployment_id=deployment_id,
                source_dir=source_dir,
                dangerous=dangerous,
                report=report,
            )
            if not ok:
                return await self._rollback_or_fail(channel, report, layout, previous, plan)

            # 落地切换 + 独立证据
            await asyncio.to_thread(self._switch_current, channel, layout, release)
            self.repo.add_event(deployment_id, f"current -> releases/{release}")

            health = await self._health_check(channel, plan, host, layout)
            self.repo.set_health(deployment_id, health)
            report.health = health
            if not health.get("ok"):
                report.error = f"健康检查未通过：{health.get('detail', '')}"
                self.repo.add_event(
                    deployment_id, f"健康检查未通过：{health.get('detail', '')}", level="error"
                )
                return await self._rollback_or_fail(channel, report, layout, previous, plan)

            self.repo.set_status(deployment_id, DeployStatus.SUCCESS)
            self.repo.touch_host(int(host.id))
            self.repo.add_event(deployment_id, "部署成功（证据：systemd + 端口/进程 + 控制端探针）")
            report.status = DeployStatus.SUCCESS
            await asyncio.to_thread(self._cleanup_releases, channel, layout, release, report.previous_release)
            return report
        finally:
            await asyncio.to_thread(channel.close)

    async def rollback(self, deployment_id: int) -> DeploymentReport:
        """把 ``current`` 切回上一次成功部署的 release，并**重新**验一遍健康。"""
        row = self.repo.get_deployment(deployment_id)
        if row is None:
            raise DeployError(f"没有这条部署记录：{deployment_id}")
        if not row.previous_release:
            raise DeployError(f"部署 {deployment_id} 没有可回滚的上一版本")

        host = self._host_by_id(int(row.host_id))
        layout = RemoteLayout.of(row.app_root or self.cfg.app_root, row.repo_slug)
        plan = DeployPlan.load(row.plan_json)
        report = DeploymentReport(
            deployment_id=int(row.id),
            host=host.name,
            repo_slug=row.repo_slug,
            status=DeployStatus.ROLLED_BACK,
            release=row.release,
            previous_release=row.previous_release,
            service_name=row.service_name,
            url=row.url,
        )

        target = self._target(host)
        channel = await asyncio.to_thread(
            connect, target, accept_new_host_key=self.cfg.accept_new_host_key
        )
        try:
            self.repo.add_event(int(row.id), f"人工回滚 → releases/{row.previous_release}")
            if not await asyncio.to_thread(
                self._release_exists, channel, layout, str(row.previous_release)
            ):
                # 目标 release 已被清理（destroy 之后很常见）：拒绝切过去，
                # 否则会留下悬空软链，服务因工作目录不存在而反复重启。
                self.repo.add_event(
                    int(row.id), f"回滚点 releases/{row.previous_release} 不存在，拒绝切换", level="error"
                )
                raise DeployError(
                    f"回滚点 releases/{row.previous_release} 已不存在（可能已被 destroy 清理），"
                    "未做任何改动"
                )
            await asyncio.to_thread(self._switch_current, channel, layout, str(row.previous_release))
            await self._restart(channel, plan)
            health = await self._health_check(channel, plan, host, layout)
            report.health = health
            if health.get("ok"):
                self.repo.set_status(int(row.id), DeployStatus.ROLLED_BACK)
                self.repo.add_event(int(row.id), "回滚完成，健康检查通过")
            else:
                report.status = DeployStatus.FAILED
                report.error = "回滚后仍不健康，需人工介入"
                self.repo.set_status(int(row.id), DeployStatus.FAILED, error=report.error)
                self.repo.add_event(int(row.id), report.error, level="error")
            return report
        finally:
            await asyncio.to_thread(channel.close)

    async def destroy(self, deployment_id: int) -> DeploymentReport:
        """停服务并删 release 目录，**保留** ``shared/``（数据与 .env 在里面）。"""
        row = self.repo.get_deployment(deployment_id)
        if row is None:
            raise DeployError(f"没有这条部署记录：{deployment_id}")
        host = self._host_by_id(int(row.host_id))
        layout = RemoteLayout.of(row.app_root or self.cfg.app_root, row.repo_slug)
        report = DeploymentReport(
            deployment_id=int(row.id),
            host=host.name,
            repo_slug=row.repo_slug,
            status=DeployStatus.DESTROYED,
            release=row.release,
            service_name=row.service_name,
            url=row.url,
        )
        target = self._target(host)
        channel = await asyncio.to_thread(
            connect, target, accept_new_host_key=self.cfg.accept_new_host_key
        )
        try:
            if row.service_name:
                # 提权口径从**计划**里取（与 restart/rollback 一致）。真机验证时这里
                # 漏了 sudo，destroy 直接以 "Interactive authentication required" 失败：
                # 非 root 用户停系统级 unit 必须提权，这是同一个坑的第二次。
                plan = DeployPlan.load(row.plan_json or {"repo_slug": row.repo_slug, "app": {"name": "unknown"}, "health": [{"port": 1}]})
                sudo = plan.app.service_sudo
                await self._systemctl(channel, ServiceAction.STOP, row.service_name, sudo=sudo)
                # 「下线」必须把 unit 也收掉。只停服务的话，`enabled` 还在，机器一重启
                # systemd 就去启动一个指向**已删除目录**的服务——真机验证后实测到的
                # 第 7 个缺陷：服务看似下线了，重启后一路报错，而列表里没有任何痕迹。
                prefix = "sudo -n " if sudo else ""
                unit_path = f"/etc/systemd/system/{row.service_name}.service"
                await asyncio.to_thread(
                    channel.run,
                    f"{prefix}systemctl disable {shlex.quote(row.service_name)} 2>&1; "
                    f"{prefix}rm -f {shlex.quote(unit_path)}; "
                    f"{prefix}systemctl daemon-reload",
                    timeout=180,
                )
            removed = await asyncio.to_thread(
                channel.run,
                f"rm -rf {shlex.quote(layout.base)}/releases {shlex.quote(layout.current)}",
                timeout=180,
            )
            if removed.exit_code != 0:
                # 常见原因：unit 缺 `User=`，服务曾以 root 运行，生成的 __pycache__ /
                # SQLite 属主是 root，普通用户删不掉。**不能静默当成功**——真机验证时
                # 这里吞掉了 rm 的报错，`releases/` 明明还在，记录却写「已删除」。
                escalated = await asyncio.to_thread(
                    channel.run,
                    f"sudo -n rm -rf {shlex.quote(layout.base)}/releases {shlex.quote(layout.current)}",
                    timeout=300,
                )
                if escalated.exit_code != 0:
                    detail = (escalated.stderr or removed.stderr or "").strip()
                    self.repo.add_event(int(row.id), f"删除 releases 失败：{detail}", level="error")
                    raise DeployError(
                        "删除 releases 失败（普通与 sudo 都失败）："
                        f"{detail or '权限不足'}。多半是服务曾以 root 运行"
                        "（unit 缺 User=）或目录属主异常，请人工确认后重试"
                    )
                self.repo.add_event(int(row.id), "releases 属主为 root，已用 sudo 删除", level="warn")
            self.repo.add_event(int(row.id), "已停止服务、移除 unit 并删除 releases（保留 shared）")
            # 历史里那些「成功」记录指向的 release 也一起没了。不把它们标掉的话，
            # 下一次部署的 `previous_release` 会挑到这些**已删除**的目录，回滚就变成
            # 往悬空软链上切（真机验证踩过：服务以 200/CHDIR 反复重启）。
            for stale in self.repo.list_deployments(
                host_id=int(row.host_id), repo_slug=row.repo_slug, limit=200
            ):
                if stale.id != row.id and stale.status == DeployStatus.SUCCESS.value:
                    self.repo.set_status(int(stale.id), DeployStatus.DESTROYED)
                    self.repo.add_event(
                        int(stale.id), "所属 releases 已随下线删除，标记为 destroyed（不再作为回滚点）"
                    )
            self.repo.set_status(int(row.id), DeployStatus.DESTROYED)
            return report
        finally:
            await asyncio.to_thread(channel.close)

    async def ping(self, host_name: str) -> dict[str, Any]:
        """连一下、跑两条只读命令，回显基本信息（`fissue host ping`）。"""
        host = self._host(host_name)
        target = self._target(host)
        channel = await asyncio.to_thread(
            connect, target, accept_new_host_key=self.cfg.accept_new_host_key
        )
        try:
            info: dict[str, Any] = {"host": host.name, "ok": True}
            for key, command in (
                ("uptime", "uptime -p 2>/dev/null || true"),
                ("disk", "df -h / | tail -1"),
                ("systemd", "systemctl --version | head -1"),
            ):
                result = await asyncio.to_thread(channel.run, command, timeout=30)
                info[key] = (result.stdout or result.stderr).strip()
            if channel.host_fingerprint and not host.host_fingerprint:
                self.repo.remember_host_fingerprint(int(host.id), channel.host_fingerprint)
                info["fingerprint_pinned"] = channel.host_fingerprint
            self.repo.touch_host(int(host.id))
            return info
        finally:
            await asyncio.to_thread(channel.close)

    # -- 步骤执行 ---------------------------------------------------------

    async def _run_steps(
        self,
        *,
        channel: SSHChannel,
        plan: DeployPlan,
        layout: RemoteLayout,
        release: str,
        deployment_id: int,
        source_dir: Path,
        dangerous: bool,
        report: DeploymentReport,
    ) -> bool:
        """按顺序执行；任一步不满足判定条件就返回 False（调用方负责回滚）。"""
        release_dir = layout.release_dir(release)
        for seq, step in enumerate(plan.steps, start=1):
            row_id = self.repo.start_step(
                deployment_id=deployment_id,
                seq=seq,
                step_id=step.id,
                kind=step.kind.value,
                command=step.summary(),
            )
            self.repo.add_event(deployment_id, f"[{seq}/{len(plan.steps)}] {step.id}: {step.summary()}")
            started = datetime.now(timezone.utc)
            try:
                result = await self._execute(
                    channel=channel,
                    step=step,
                    plan=plan,
                    layout=layout,
                    release_dir=release_dir,
                    source_dir=source_dir,
                    dangerous=dangerous,
                )
            except FissueError as exc:
                result = StepResult(
                    step_id=step.id,
                    kind=step.kind,
                    status=StepStatus.FAILED,
                    error=str(exc),
                    output_tail=str(exc),
                )

            if result.status is StepStatus.OK:
                self.repo.finish_step(
                    row_id,
                    status=StepStatus.OK,
                    exit_code=result.exit_code,
                    output=result.output_tail,
                )
            else:
                self.repo.finish_step(
                    row_id,
                    status=StepStatus.FAILED,
                    exit_code=result.exit_code,
                    output=result.output_tail,
                    error=result.error,
                )
                self.repo.add_event(
                    deployment_id,
                    f"步骤失败：{step.id}（{result.error or '判定条件不满足'}）",
                    level="error",
                )
                await self._run_step_rollback(channel, step, layout, release_dir, dangerous, deployment_id)
                report.steps.append(result)
                report.error = f"步骤 {step.id} 失败：{result.error or '判定条件不满足'}"
                return False
            del started
            report.steps.append(result)
        return True

    async def _execute(
        self,
        *,
        channel: SSHChannel,
        step: DeployStep,
        plan: DeployPlan,
        layout: RemoteLayout,
        release_dir: str,
        source_dir: Path,
        dangerous: bool,
    ) -> StepResult:
        """按 kind 分派，并核对 ``check``。"""
        spec = step.spec
        check = step.check

        if isinstance(spec, ExecSpec):
            command = f"cd {shlex.quote(release_dir)} && {spec.command}" if not spec.cwd else spec.command
            if spec.cwd:
                command = f"cd {shlex.quote(_join(release_dir, spec.cwd))} && {spec.command}"
            guard.check_command(spec.command, allow_dangerous=dangerous)
            result = await asyncio.to_thread(
                channel.run, command, timeout=spec.timeout or self.cfg.command_timeout
            )
            ok = result.exit_code == (check.expect_exit_code if check else 0)
            if ok and check and check.output_contains:
                ok = check.output_contains in (result.stdout + result.stderr)
            if ok and check and check.output_excludes:
                ok = check.output_excludes not in (result.stdout + result.stderr)
            return StepResult(
                step_id=step.id,
                kind=step.kind,
                status=StepStatus.OK if ok else StepStatus.FAILED,
                exit_code=result.exit_code,
                output_tail=result.stdout + result.stderr,
                error=None if ok else f"退出码 {result.exit_code}",
            )

        if isinstance(spec, UploadSpec):
            local = _resolve_local(spec.src, source_dir)
            if not local.exists():
                raise DeployError(f"本地产物不存在：{local}")
            remote = _join(release_dir, spec.dst)
            await asyncio.to_thread(self._put_path, channel, local, remote)
            return StepResult(step_id=step.id, kind=step.kind, status=StepStatus.OK)

        if isinstance(spec, WriteFileSpec):
            path = spec.path if spec.allow_outside_release else _join(release_dir, spec.path.lstrip("/"))
            parent = path.rsplit("/", 1)[0] if "/" in path else "/"
            await asyncio.to_thread(
                channel.run, f"mkdir -p {shlex.quote(parent)}", timeout=60
            )
            await asyncio.to_thread(channel.put_text, spec.content, path, spec.mode)
            return StepResult(step_id=step.id, kind=step.kind, status=StepStatus.OK)

        if isinstance(spec, ServiceSpec):
            # service 动作不走 guard 的命令检查（它不是 exec），提权检查补在这里：
            # 否则「计划里只写一个 service 步骤」就能绕过 allow_dangerous 拿到 root。
            if spec.sudo and not dangerous:
                raise DeployError(
                    f"步骤 {step.id} 需要对 systemd 提权（sudo），但 deploy.allow_dangerous=false"
                )
            await self._systemctl(channel, spec.action, spec.name, sudo=spec.sudo)
            return StepResult(step_id=step.id, kind=step.kind, status=StepStatus.OK)

        if isinstance(spec, ProbeSpec):
            if spec.port is not None and not spec.url:
                # 内联的 TCP 探针步骤同样要知道「探哪个主机」。`ProbeSpec` 只有 port，
                # 目标主机来自连接上下文——不补就会在 `_probe_host` 里抛
                # "TCP 探针缺少目标主机"（AI 计划里正好有一步 `probe tcp:5000`，真机踩到）。
                object.__setattr__(spec, "_host", getattr(channel.target, "address", ""))
            outcome = await probe(spec)
            return StepResult(
                step_id=step.id,
                kind=step.kind,
                status=StepStatus.OK if outcome["ok"] else StepStatus.FAILED,
                output_tail=str(outcome.get("detail", "")),
                error=None if outcome["ok"] else str(outcome.get("detail", "")),
            )

        raise DeployError(f"未知动作：{step.kind}")  # pragma: no cover - schema 已穷举

    async def _run_step_rollback(
        self,
        channel: SSHChannel,
        step: DeployStep,
        layout: RemoteLayout,
        release_dir: str,
        dangerous: bool,
        deployment_id: int,
    ) -> None:
        """执行该步自带的 rollback 动作；失败只记日志，不掩盖「原步骤已经失败」。"""
        for action in step.rollback:
            self.repo.add_event(deployment_id, f"回滚动作：{action.id}: {action.summary()}", level="warn")
            try:
                await self._execute(
                    channel=channel,
                    step=action,
                    plan=DeployPlan(
                        repo_slug="rollback",
                        app=AppSpec(name="rollback"),
                        steps=[action],
                        health=[ProbeSpec(port=1)],
                    ),
                    layout=layout,
                    release_dir=release_dir,
                    source_dir=Path("."),
                    dangerous=dangerous,
                )
            except Exception as exc:  # noqa: BLE001 - 回滚动作失败不能盖掉主错误
                self.repo.add_event(
                    deployment_id, f"回滚动作 {action.id} 也失败了：{exc}", level="error"
                )

    # -- 证据判定 ---------------------------------------------------------

    async def _health_check(
        self,
        channel: SSHChannel,
        plan: DeployPlan,
        host: DeployHostRow,
        layout: RemoteLayout,
    ) -> dict[str, Any]:
        """三类证据合起来判定「服务真的起来了」。

        少任何一类都会漏掉真实故障：
        - 只有探针通：可能连的是**旧进程**（新版本根本没起，端口被上一次的占着）；
        - 只有 systemd active：进程活着，但应用在报 500 或卡在迁移里；
        - 只有端口在听：同上，甚至可能是别的程序占了这个端口。

        **必须等**。``Type=simple`` 的 unit 在进程刚 exec 时就报 active，而 Flask/uvicorn
        真正 bind 端口还要一两秒——真机首次部署时正是这里误判成失败（探针随后明明通了）。
        所以按 ``deploy.health_timeout`` 轮询，直到全部证据通过或超时；超时后再抓一段
        journal 作为失败原因（否则使用者只能自己 SSH 上去猜）。
        """
        deadline = time.monotonic() + float(self.cfg.health_timeout)
        attempt = 0
        report: dict[str, Any] = {}
        while True:
            attempt += 1
            report = await self._collect_health(channel, plan, host, layout)
            report["attempts"] = attempt
            if report["ok"] or time.monotonic() >= deadline:
                break
            await asyncio.sleep(2.0)

        if not report["ok"] and plan.app.service_name:
            journal = await self._service_journal(channel, plan)
            if journal:
                report["journal"] = journal
                report["detail"] = f"{report.get('detail', '')}；journal：{journal}"
        report["waited"] = round(float(self.cfg.health_timeout) - max(0.0, deadline - time.monotonic()), 1)
        return report

    async def _collect_health(
        self,
        channel: SSHChannel,
        plan: DeployPlan,
        host: DeployHostRow,
        layout: RemoteLayout,
    ) -> dict[str, Any]:
        """采一次证据（不等待、不重试）。"""
        checks: list[dict[str, Any]] = []

        if plan.app.service_name:
            unit = plan.app.service_name
            result = await asyncio.to_thread(channel.run, f"systemctl is-active {shlex.quote(unit)}", timeout=30)
            active = (result.stdout or "").strip() == "active"
            checks.append(
                {
                    "kind": "systemd",
                    "target": unit,
                    "ok": active,
                    "detail": (result.stdout or result.stderr).strip() or f"exit={result.exit_code}",
                }
            )

        if plan.app.port:
            # 用 ss 的过滤表达式而不是「grep 文本」：grep ':5000 ' 既依赖对齐的空格，
            # 也会匹配到 `:50000`。``-H`` 去掉表头，命中即非空。
            result = await asyncio.to_thread(
                channel.run,
                f"ss -ltnH 'sport = :{int(plan.app.port)}' 2>/dev/null | head -1",
                timeout=30,
            )
            listening = bool((result.stdout or "").strip())
            checks.append(
                {
                    "kind": "listen",
                    "target": f"{host.address}:{plan.app.port}",
                    "ok": listening,
                    "detail": (result.stdout or "").strip() or "远端没有进程监听该端口",
                }
            )

        probe_results: list[dict[str, Any]] = []
        for spec in plan.health:
            if spec.port is not None and not spec.url:
                # TCP 探针的目标主机来自部署上下文；这里补上再探测。
                object.__setattr__(spec, "_host", host.address)
            probe_results.append(await probe(spec))
        checks.extend({"kind": "probe", **item} for item in probe_results)

        ok = bool(checks) and all(item["ok"] for item in checks)
        failed = [f"{item['kind']}:{item['target']}（{item.get('detail', '')}）" for item in checks if not item["ok"]]
        return {
            "ok": ok,
            "detail": "全部证据通过" if ok else "；".join(failed) or "没有任何可用证据",
            "checks": checks,
            "release": layout.current,
        }

    async def _service_journal(self, channel: SSHChannel, plan: DeployPlan) -> str:
        """取最近的 unit 日志（失败现场的第一手证据）。

        提权要求跟 service 步骤一致：非 root 用户读系统 unit 的 journal 需要 sudo。
        取不到就返回空串——它只是**附加证据**，不该让失败原因再叠一层错误。
        """
        unit = plan.app.service_name
        if not unit:
            return ""
        prefix = "sudo -n " if plan.app.service_sudo else ""
        result = await asyncio.to_thread(
            channel.run,
            f"{prefix}journalctl -u {shlex.quote(unit)} -n 20 --no-pager 2>&1 | tail -20",
            timeout=60,
        )
        text = (result.stdout or "").strip()
        return text[-1500:]

    # -- 远端基础操作 -----------------------------------------------------

    async def _prepare(
        self,
        channel: SSHChannel,
        host: DeployHostRow,
        layout: RemoteLayout,
        release: str,
        deployment_id: int,
    ) -> None:
        """建目录、探基础环境。每一条都先跑再判：远端到底有没有 systemd / 磁盘够不够，
        不该等到第 20 步才暴露。
        """
        release_dir = layout.release_dir(release)
        dirs = " ".join(
            shlex.quote(p) for p in (layout.releases, layout.shared, release_dir)
        )
        # **一条命令一件事**。此前把 mkdir 与后面两条探测用 `&&` / `||` 串成一句，
        # 结果 mkdir 因权限失败时被后面的 `|| echo WARN` 短路吃掉了——整体退出码仍是
        # 0，于是「目录没建出来」要等到上传步骤以 tar 的报错形式暴露（真机验证踩到）。
        # 判定必须落在**这条命令自己的退出码**上。
        result = await asyncio.to_thread(channel.run, f"mkdir -p {dirs}", timeout=120)
        if result.exit_code != 0:
            # 默认 app_root 是 /opt，非 root 用户建不了目录。这是正常场景而不是错误：
            # 回退到 sudo，并把属主交给连接用户，后续步骤才不需要层层提权。
            owner_name = self._current_user(channel)
            owner = shlex.quote(owner_name)
            elevated = await asyncio.to_thread(
                channel.run,
                f"sudo -n mkdir -p {dirs} && sudo -n chown -R {owner} {shlex.quote(layout.base)}",
                timeout=180,
            )
            if elevated.exit_code != 0:
                self.repo.add_event(
                    deployment_id,
                    f"准备远端目录失败（普通与 sudo 都失败）：{(elevated.stderr or result.stderr).strip()}",
                    level="error",
                )
                raise DeployError(
                    "准备远端目录失败："
                    f"{(elevated.stderr or result.stderr).strip() or '权限不足且 sudo 不可用'}"
                    f"（连接用户 {owner_name}，目标 {layout.base}）"
                )
            self.repo.add_event(
                deployment_id, f"{layout.base} 由 sudo 创建并把属主交给 {owner_name}", level="warn"
            )

        env = await asyncio.to_thread(
            channel.run,
            f"df -Pk {shlex.quote(layout.base)} | tail -1; command -v systemctl >/dev/null 2>&1 && echo has-systemd || echo no-systemd",
            timeout=60,
        )
        self.repo.add_event(deployment_id, f"远端准备完成：{env.stdout.strip()}")

    def _current_user(self, channel: SSHChannel) -> str:
        """连接用户名。用于把 sudo 建出来的目录交还给部署用户。"""
        target = getattr(channel, "target", None)
        return str(getattr(target, "user", "") or "root")

    async def _systemctl(
        self,
        channel: SSHChannel,
        action: ServiceAction,
        name: str,
        *,
        sudo: bool = False,
    ) -> None:
        """跑一条 systemctl 命令。

        ``sudo`` 走 ``sudo -n``：**非交互**。真机上加 ``-n`` 很关键——没有它，
        提权失败会卡在密码提示上直到超时，而 ``-n`` 会立刻以非零码失败，错误
        信息也更直白（"sudo: a password is required"）。
        """
        prefix = "sudo -n " if sudo else ""
        unit = shlex.quote(name)
        if action is ServiceAction.DAEMON_RELOAD:
            command = f"{prefix}systemctl daemon-reload"
        else:
            command = f"{prefix}systemctl {action.value} {unit}"
        result = await asyncio.to_thread(channel.run, command, timeout=180)
        if result.exit_code != 0:
            raise DeployError(
                f"systemctl {action.value} {name} 失败：{(result.stderr or result.stdout).strip()}"
            )

    async def _restart(self, channel: SSHChannel, plan: DeployPlan) -> None:
        """按计划里记录的服务与提权要求重启（回滚路径也复用这一处）。"""
        if not plan.app.service_name:
            return
        await self._systemctl(
            channel,
            ServiceAction.RESTART,
            plan.app.service_name,
            sudo=plan.app.service_sudo,
        )

    def _release_exists(self, channel: SSHChannel, layout: RemoteLayout, release: str) -> bool:
        """回滚点是否还在盘上。

        `destroy` 会删掉整个 `releases/`，而历史记录里那条「成功部署」仍留着
        `release` 名——不校验就切过去，得到的是一个**悬空软链**：unit 的工作目录
        不存在，服务以 200/CHDIR 反复重启。真机验证时正是这样把服务搞挂的。
        """
        target = layout.release_dir(release)
        result = channel.run(f"test -d {shlex.quote(target)} && echo yes || echo no", timeout=60)
        return (result.stdout or "").strip() == "yes"

    def _switch_current(self, channel: SSHChannel, layout: RemoteLayout, release: str) -> None:
        """原子切换 ``current`` 软链。回滚与上线走的是**同一段**代码。"""
        target = layout.release_dir(release)
        result = channel.run(
            f"ln -sfn {shlex.quote(target)} {shlex.quote(layout.current)} && "
            f"readlink -f {shlex.quote(layout.current)}",
            timeout=60,
        )
        if result.exit_code != 0:
            raise DeployError(f"切换 current 失败：{(result.stderr or result.stdout).strip()}")

    def _cleanup_releases(
        self,
        channel: SSHChannel,
        layout: RemoteLayout,
        current: str,
        previous: str | None,
    ) -> None:
        """按 ``keep_releases`` 删旧 release，但**永不**删 current 与 previous。"""
        keep = max(1, self.cfg.keep_releases)
        protected = {current, previous} - {None}
        script = (
            f"cd {shlex.quote(layout.releases)} && ls -1dt */ 2>/dev/null | sed 's#/##' | tail -n +{keep + 1}"
        )
        result = channel.run(script, timeout=60)
        candidates = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        doomed = [name for name in candidates if name not in protected]
        if not doomed:
            return
        rm = " && ".join(
            f"rm -rf {shlex.quote(layout.releases + '/' + name)}" for name in doomed
        )
        channel.run(rm, timeout=300)

    def _put_path(self, channel: SSHChannel, local: Path, remote: str) -> None:
        """传文件或目录。目录走 tar 打包（SFTP 递归既慢又容易丢掉符号链接语义）。"""
        if local.is_file():
            channel.run(f"mkdir -p {shlex.quote(_dirname(remote))}", timeout=60)
            channel.put_file(local, remote)
            return
        if not local.is_dir():
            raise DeployError(f"不支持的产物类型：{local}")
        channel.run(f"mkdir -p {shlex.quote(remote)}", timeout=60)
        archive = local.parent / f".fissue-upload-{int(time.time() * 1000)}.tar.gz"
        try:
            import tarfile

            with tarfile.open(archive, "w:gz") as tar:
                for entry in sorted(local.iterdir()):
                    tar.add(entry, arcname=entry.name)
            remote_tar = f"/tmp/{archive.name}"
            channel.put_file(archive, remote_tar)
            result = channel.run(
                f"tar -xzf {shlex.quote(remote_tar)} -C {shlex.quote(remote)} && rm -f {shlex.quote(remote_tar)}",
                timeout=600,
            )
            if result.exit_code != 0:
                raise DeployError(f"解包失败：{(result.stderr or result.stdout).strip()}")
        finally:
            archive.unlink(missing_ok=True)

    # -- 失败与回滚 -------------------------------------------------------

    async def _rollback_or_fail(
        self,
        channel: SSHChannel,
        report: DeploymentReport,
        layout: RemoteLayout,
        previous: DeploymentRow | None,
        plan: DeployPlan,
    ) -> DeploymentReport:
        """没有回滚点就判失败；有就把 current 切回去并**重新探针**验证。"""
        if previous is None or not previous.release:
            report.status = DeployStatus.FAILED
            self.repo.set_status(report.deployment_id, DeployStatus.FAILED, error=report.error)
            self.repo.add_event(report.deployment_id, "无可用回滚点，保留现场目录供排查", level="error")
            return report

        self.repo.add_event(report.deployment_id, f"开始回滚到 releases/{previous.release}", level="warn")
        try:
            if not await asyncio.to_thread(self._release_exists, channel, layout, str(previous.release)):
                # 回滚点已被删除（例如上线后执行过 destroy）。这时**绝不能**切过去：
                # 悬空软链会让 unit 的工作目录不存在，服务以 200/CHDIR 反复重启，
                # 比「一开始就没回滚」更糟。真机验证时正是这样把服务搞挂的。
                report.status = DeployStatus.FAILED
                report.error = (
                    f"{report.error}；回滚点 releases/{previous.release} 已不存在（可能已被 destroy 清理），"
                    "保持当前状态，需人工处理"
                )
                self.repo.set_status(report.deployment_id, DeployStatus.FAILED, error=report.error)
                self.repo.add_event(report.deployment_id, "回滚点不存在，已放弃回滚", level="error")
                return report

            await asyncio.to_thread(self._switch_current, channel, layout, str(previous.release))
            await self._restart(channel, plan)
            health = await self._health_check(channel, plan, self._host_by_id(int(previous.host_id)), layout)
            report.health = health
            report.rolled_back = True
            if health.get("ok"):
                report.status = DeployStatus.ROLLED_BACK
                self.repo.set_status(report.deployment_id, DeployStatus.ROLLED_BACK, error=report.error)
                self.repo.add_event(report.deployment_id, "回滚完成，服务已恢复")
            else:
                report.status = DeployStatus.FAILED
                report.error = f"{report.error}；回滚后仍不健康，需人工介入"
                self.repo.set_status(report.deployment_id, DeployStatus.FAILED, error=report.error)
                self.repo.add_event(report.deployment_id, "回滚后仍不健康", level="error")
        except Exception as exc:  # noqa: BLE001 - 回滚失败要让原始错误也保留
            report.status = DeployStatus.FAILED
            report.error = f"{report.error}；回滚失败：{exc}"
            self.repo.set_status(report.deployment_id, DeployStatus.FAILED, error=report.error)
            self.repo.add_event(report.deployment_id, f"回滚失败：{exc}", level="error")
        return report

    def _fail(self, report: DeploymentReport, _channel: None, message: str) -> DeploymentReport:
        report.status = DeployStatus.FAILED
        report.error = message
        self.repo.set_status(report.deployment_id, DeployStatus.FAILED, error=message)
        self.repo.add_event(report.deployment_id, message, level="error")
        return report

    # -- 主机与目标 -------------------------------------------------------

    def _host(self, name: str) -> DeployHostRow:
        row = self.repo.get_host(name)
        if row is None:
            raise DeployError(f"没有这台主机：{name}（先 `fissue host add`）")
        if not row.enabled:
            raise DeployError(f"主机已停用：{name}")
        return row

    def _host_by_id(self, host_id: int) -> DeployHostRow:
        row = self.repo.get_host_by_id(host_id)
        if row is None:
            raise DeployError(f"主机记录不存在：id={host_id}")
        return row

    def _target(self, host: DeployHostRow) -> SSHTarget:
        """组装连接目标。凭据在这里才解密，**用完即随对象丢弃**。"""
        from .schema import AuthMethod

        return SSHTarget(
            name=host.name,
            address=host.address,
            port=int(host.port),
            user=host.user,
            auth_method=AuthMethod(host.auth_method),
            credential=self.box.open(host.credential),
            host_fingerprint=host.host_fingerprint,
            timeout=float(self.cfg.connect_timeout),
        )

    def _plan_url(self, plan: DeployPlan, host: DeployHostRow) -> str | None:
        url = next((spec.url for spec in plan.health if spec.url), None)
        if url:
            return url
        if plan.app.port:
            return f"http://{host.address}:{plan.app.port}"
        return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _join(base: str, rel: str) -> str:
    rel = rel.strip()
    if not rel or rel == ".":
        return base
    return f"{base.rstrip('/')}/{rel.lstrip('/')}"


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "/"


def _resolve_local(src: str, source_dir: Path) -> Path:
    """``upload.src`` 解析：相对路径相对仓库根，绝对路径原样。"""
    candidate = Path(src)
    return candidate if candidate.is_absolute() else (source_dir / candidate)