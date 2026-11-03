"""部署服务层：CLI 与 Web 共用的那一条链。

**为什么必须有这一层**：``deploy plan`` 与 ``POST /api/v1/deployments`` 做的是同一件事
（克隆 → AI 出计划 → 校验 → 落库），如果各自实现一遍，「CLI 能跑通但 Web 不行」这类
分叉只是时间问题。约定：**路由与命令都只做参数解析和输出**，真正的动作全在这里。

仍然遵守 runner 的三条规矩：执行前校验计划、成功后独立证据、失败即停并回滚。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging_setup import get_logger
from ..models import Platform
from ..pipeline.context import RuntimeContext
from ..store.deploy_repo import DeployRepository
from ..store.tables import DeployHostRow
from . import guard
from .normalize import normalize_plan
from .planner import DeploymentPlanner, PlannerError
from .runner import DeployError, DeploymentReport, DeploymentRunner
from .schema import DeployPlan

log = get_logger(__name__)


class DeployService:
    """一次部署从「读仓库」到「证据判定」的完整流程。"""

    def __init__(
        self,
        settings: Settings,
        repo: DeployRepository,
        *,
        runner: DeploymentRunner | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.runner = runner or DeploymentRunner(settings, repo)

    # -- 前置检查 ---------------------------------------------------------

    def require_enabled(self) -> None:
        if not self.settings.deploy.enabled:
            raise DeployError("部署能力未开启：请设置 deploy.enabled=true")

    def host(self, name: str) -> DeployHostRow:
        row = self.repo.get_host(name)
        if row is None:
            raise DeployError(f"没有这台主机：{name}")
        if not row.enabled:
            raise DeployError(f"主机已停用：{name}")
        return row

    def app_root_of(self, host: DeployHostRow) -> str:
        return host.app_root or self.settings.deploy.app_root

    # -- 计划 -------------------------------------------------------------

    async def build_plan(
        self,
        *,
        slug: str,
        host: str,
        ref: str | None = None,
        platform: str | None = None,
        notes: str = "",
    ) -> tuple[DeployPlan, Any]:
        """克隆仓库并生成计划。返回 ``(plan, workspace)``，**调用方负责 cleanup**。"""
        plat = Platform(platform) if platform else None
        repo_cfg = self.settings.repo(slug, plat)
        ctx = RuntimeContext.create(self.settings)
        workspace = await ctx.clone_workspace(repo_cfg, branch=ref)
        target_ref = ref or repo_cfg.base_branch or "main"
        planner = DeploymentPlanner(
            ctx.llm,
            repo_cfg,
            app_root=self.app_root_of(self.host(host)),
            max_steps=self.settings.deploy.max_plan_steps,
        )
        try:
            plan = await planner.plan(
                workspace.root,
                ref=target_ref,
                commit_sha=_commit_sha_of(workspace),
                notes=notes or f"目标主机 {host}",
            )
        except PlannerError:
            workspace.cleanup()
            raise
        self._record_llm_usage(planner, repo_cfg.slug)
        return plan, workspace

    def validate(self, plan: DeployPlan, host: DeployHostRow, *, allow_dangerous: bool | None = None) -> None:
        """计划校验（与 runner 用的是同一个函数，Web 上「预检」也走它）。"""
        guard.validate_plan(
            plan,
            max_steps=self.settings.deploy.max_plan_steps,
            allow_dangerous=(
                self.settings.deploy.allow_dangerous if allow_dangerous is None else allow_dangerous
            ),
            app_root=self.app_root_of(host),
        )

    def save_plan(self, plan: DeployPlan, host: DeployHostRow) -> int:
        return self.repo.create_deployment(
            host_id=int(host.id),
            repo_slug=plan.repo_slug,
            ref=plan.ref,
            commit_sha=plan.commit_sha,
            plan=plan.dump(),
            app_root=self.app_root_of(host),
        )

    async def plan_only(
        self,
        *,
        slug: str,
        host: str,
        ref: str | None = None,
        platform: str | None = None,
        plan_file: str | None = None,
        allow_dangerous: bool | None = None,
    ) -> tuple[int, DeployPlan, str | None]:
        """只产计划并落库。

        返回 ``(deployment_id, plan, 校验告警)``：校验不过**不阻止落库**（用户要能看到
        「它想干什么」），但不给 apply 用——真正执行时 runner 会再校验一次。
        """
        self.require_enabled()
        host_row = self.host(host)
        plan, workspace, warning = await self._resolve_plan(
            slug=slug, host=host, ref=ref, platform=platform, plan_file=plan_file, allow_dangerous=allow_dangerous
        )
        try:
            deployment_id = self.save_plan(plan, host_row)
        finally:
            if workspace is not None:
                workspace.cleanup()
        return deployment_id, plan, warning

    async def apply(
        self,
        *,
        slug: str,
        host: str,
        ref: str | None = None,
        platform: str | None = None,
        plan_file: str | None = None,
        allow_dangerous: bool | None = None,
        accept_new_host_key: bool | None = None,
        source_dir: Path | None = None,
    ) -> DeploymentReport:
        """端到端部署。计划非法时**在连机器之前**就抛错。

        ``source_dir`` 用于「复用已有计划」时指定本地源码目录：那种场景不克隆仓库，
        若不给就会退化成「上传当前工作目录」——把控制端仓库传到别人服务器上是
        最低级的事故，所以调用方（CLI ``--source``）必须显式给出。
        """
        self.require_enabled()
        self.host(host)
        plan, workspace, _warning = await self._resolve_plan(
            slug=slug, host=host, ref=ref, platform=platform, plan_file=plan_file, allow_dangerous=allow_dangerous
        )
        try:
            return await self.runner.run(
                host_name=host,
                plan=plan,
                source_dir=(
                    workspace.root
                    if workspace is not None
                    else (source_dir or Path("."))
                ),
                accept_new_host_key=accept_new_host_key,
                allow_dangerous=allow_dangerous,
            )
        finally:
            if workspace is not None:
                workspace.cleanup()

    async def _resolve_plan(
        self,
        *,
        slug: str,
        host: str,
        ref: str | None,
        platform: str | None,
        plan_file: str | None,
        allow_dangerous: bool | None,
    ) -> tuple[DeployPlan, Any, str | None]:
        if plan_file:
            plan = load_plan_file(plan_file)
            workspace = None
        else:
            plan, workspace = await self.build_plan(
                slug=slug, host=host, ref=ref, platform=platform
            )
        # 归一化：把「模型漏写但计划里能推出来」的细节补上（提权、探针别名）。
        # 放在校验**之前**，否则那些能自动救回来的计划会先被 guard 判死。
        host_row = self.host(host)
        plan = normalize_plan(
            plan, host_name=host_row.name, host_address=host_row.address, host_user=host_row.user
        )
        warning: str | None = None
        try:
            self.validate(plan, host_row, allow_dangerous=allow_dangerous)
        except guard.GuardError as exc:
            warning = str(exc)
        return plan, workspace, warning

    # -- 其余动作 ---------------------------------------------------------

    async def rollback(self, deployment_id: int) -> DeploymentReport:
        self.require_enabled()
        return await self.runner.rollback(deployment_id)

    async def destroy(self, deployment_id: int) -> DeploymentReport:
        self.require_enabled()
        return await self.runner.destroy(deployment_id)

    async def ping(self, host: str) -> dict[str, Any]:
        return await self.runner.ping(host)

    # -- 内部 -------------------------------------------------------------

    def _record_llm_usage(self, planner: DeploymentPlanner, repo_slug: str) -> None:
        """把规划用掉的 token 记进 ``llm_usage``（预算统计不能漏掉部署这一路）。

        失败只记日志：用量记账失败不该让一次已经成功的部署变成失败。
        """
        usage = getattr(planner, "last_usage", None)
        if usage is None:
            return
        try:
            self.repo.record_usage(usage, repo_key=repo_slug)
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响主流程
            log.debug("部署规划的用量记账失败：%s", exc)


def _commit_sha_of(workspace: Any) -> str | None:
    """取工作副本的提交 sha；取不到就返回 ``None``。

    **不要**写 ``getattr(workspace, "commit", None)``：``RepoWorkspace`` 里
    ``commit`` 是**提交方法**（``def commit(self, message, ...)``），这样取到的是
    bound method。它会一路塞进 ``plan.commit_sha``，直到落库时炸成
    ``Unable to serialize unknown type: <class 'method'>``——真机验证踩过一次。
    这里只接受真正的字符串，宁可让 sha 为空（它只用于展示与追溯）。
    """
    for name in ("commit_sha", "head_sha", "sha"):
        value = getattr(workspace, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_plan_file(path: str) -> DeployPlan:
    """读一份计划文件（JSON）。格式错误直接抛，不当成「空计划」静默继续。"""
    import json

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return DeployPlan.load(raw)