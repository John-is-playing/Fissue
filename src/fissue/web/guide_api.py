"""新手引导的 REST 接口与页面（``/ops/guide``，见 docs/DEPLOY.md §15）。

为什么把「动作」收敛成一个端点
------------------------------
页面上的半自动/自动档需要点一下就执行「登记主机 / 生成计划 / 部署 / 看证据 / 回滚」。
如果每一档都去调不同的既有端点，页面就成了第二个 API 客户端，闸门与错误处理迟早
分叉；更危险的是，如果让 AI 生成的引导**指定 URL**，模型一句
``DELETE /api/v1/hosts/all`` 就能借引导页之手删东西。

所以这里只有 ``POST {prefix}/guides/actions``，``action`` 是**枚举白名单**，
每个动作内部调的还是 :class:`~fissue.deploy.service.DeployService` 与仓储——
与 CLI 完全同一条链。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..deploy.guide import GuideBuilder, fallback_guide
from ..deploy.planner import PlannerError
from ..deploy.runner import DeployError
from ..deploy.schema import AuthMethod
from ..deploy.secretbox import CredentialBox, SecretBoxError
from ..deploy.service import DeployService
from ..errors import FissueError
from ..logging_setup import get_logger
from ..models import Platform
from ..pipeline.context import RuntimeContext
from ..store.deploy_repo import DeployRepository
from ..store.tables import DeployGuideRow
from .app import (
    _exclusive_write,
    _page_context,
    _reject_proxied_write,
    _reject_remote_write,
    _require_writable,
    _settings,
)

log = get_logger(__name__)

#: 页面能点到的动作。**只增不改名**——改名等于让旧页面静默失效。
GUIDE_ACTIONS = ("add-host", "generate-plan", "apply", "check", "rollback")


class GuideRequest(BaseModel):
    repo_slug: str = Field(min_length=1, max_length=511)
    host: str = Field(default="", max_length=128)
    ref: str | None = None
    platform: str | None = None


class HostActionSpec(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=1, max_length=255)
    user: str = "root"
    port: int = Field(default=22, ge=1, le=65535)
    auth_method: str = "key"
    #: 明文凭据只在这一跳里出现，写完立刻加密入库。
    credential: str = Field(default="", max_length=20000)
    app_root: str | None = None


class GuideActionRequest(BaseModel):
    action: str
    repo_slug: str = ""
    host: str = ""
    ref: str | None = None
    platform: str | None = None
    allow_dangerous: bool = False
    deployment_id: int | None = None
    host_spec: HostActionSpec | None = None


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


def _deploy_repo(request: Request) -> DeployRepository:
    return DeployRepository(request.app.state.db)


def _service(settings: Any, request: Request) -> DeployService:
    return DeployService(settings, _deploy_repo(request))


def _guide_view(row: DeployGuideRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "repo_slug": row.repo_slug,
        "host": row.host,
        "mode": row.mode,
        "source": row.source,
        "notes": row.notes,
        "updated_at": row.updated_at,
        "stages": row.stages or [],
    }


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (DeployError, PlannerError, SecretBoxError, FissueError)):
        return HTTPException(status_code=400, detail=str(exc))
    log.exception("引导接口内部错误")
    return HTTPException(status_code=500, detail="内部错误，详情见服务端日志")


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


async def _generate(
    app: FastAPI,
    settings: Any,
    repo: DeployRepository,
    body: GuideRequest,
) -> None:
    """克隆仓库 → AI 写第三阶段 → 拼上固定两阶段 → 落库。

    整个函数**不抛异常给调用方**（它在后台跑）：失败也要落一份兜底引导，
    否则页面会永远停在「生成中」，新手什么也看不到。
    """
    try:
        repo_cfg = settings.repo(
            body.repo_slug, Platform(body.platform) if body.platform else None
        )
        ctx = RuntimeContext.create(settings)
        workspace = await ctx.clone_workspace(repo_cfg, branch=body.ref)
        try:
            host_row = repo.get_host(body.host) if body.host else None
            builder = GuideBuilder(ctx.llm)
            plan = await builder.build(
                workspace.root,
                repo_slug=repo_cfg.slug,
                host=body.host,
                host_address=host_row.address if host_row else "",
                deploy_enabled=bool(settings.deploy.enabled),
            )
        finally:
            workspace.cleanup()
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自己收尾
        log.warning("生成引导失败（落兜底模板）：%s", exc)
        plan = fallback_guide(body.repo_slug, body.host, notes=f"生成失败：{type(exc).__name__}")

    repo.upsert_guide(
        repo_slug=plan.repo_slug or body.repo_slug,
        host=plan.host or body.host,
        mode=plan.mode.value,
        stages=[stage.model_dump(mode="json") for stage in plan.stages],
        notes=plan.notes,
        source=plan.source,
    )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def _latest_any_status(
    repo: DeployRepository, host_id: int, repo_slug: str
) -> Any:
    """最近一次部署，**不限状态**。

    ``check`` 动作必须能看到「正在跑」和「刚失败」的部署：一键模式里
    `apply` 是后台任务，紧接着的 `check` 往往拿不到 success 记录；
    只查 success 会让页面把「还在跑」当成「没有部署」，新手会以为操作丢了。
    """
    rows = repo.list_deployments(host_id=host_id, repo_slug=repo_slug, limit=1)
    return rows[0] if rows else None


def register_guide_api(app: FastAPI, prefix: str, auth: Any) -> None:
    """挂上引导相关端点。"""

    @app.get(f"{prefix}/guides")
    async def get_guide(
        request: Request,
        repo_slug: str = Query(..., min_length=1),
        host: str = Query(""),
        _: None = Depends(auth),
    ) -> dict[str, Any]:
        repo = _deploy_repo(request)
        row = repo.get_guide(repo_slug=repo_slug, host=host)
        if row is None:
            raise HTTPException(status_code=404, detail="还没有为这个仓库生成引导")
        return _guide_view(row)

    @app.post(f"{prefix}/guides")
    @_exclusive_write
    async def create_guide(
        request: Request, body: GuideRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        settings = _settings(request)
        _require_writable(settings)
        _reject_proxied_write(request, settings)
        _reject_remote_write(request, settings)

        tasks: set[asyncio.Task] = getattr(request.app.state, "guide_tasks", None) or set()
        request.app.state.guide_tasks = tasks
        task = asyncio.create_task(
            _generate(request.app, settings, _deploy_repo(request), body)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"accepted": True, "repo_slug": body.repo_slug, "host": body.host}

    @app.post(f"{prefix}/guides/actions")
    @_exclusive_write
    async def run_action(
        request: Request, body: GuideActionRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """执行一个**固定动作**。动作名不在白名单里直接 400。"""
        settings = _settings(request)
        _require_writable(settings)
        _reject_proxied_write(request, settings)
        _reject_remote_write(request, settings)

        action = (body.action or "").strip()
        if action not in GUIDE_ACTIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"不认识的动作 {action!r}；只支持（白名单）：{', '.join(GUIDE_ACTIONS)}"
                ),
            )

        repo = _deploy_repo(request)
        service = _service(settings, request)
        try:
            if action == "add-host":
                if body.host_spec is None:
                    raise DeployError("add-host 需要 host_spec（地址/用户/凭据）")
                spec = body.host_spec
                sealed = CredentialBox.from_settings(settings).seal(spec.credential or "")
                host_id = repo.upsert_host(
                    name=spec.name,
                    address=spec.address,
                    credential=sealed,
                    port=spec.port,
                    user=spec.user,
                    auth_method=AuthMethod(spec.auth_method),
                    app_root=spec.app_root,
                )
                return {"action": action, "host_id": host_id, "name": spec.name}

            if action == "generate-plan":
                deployment_id, plan, warning = await service.plan_only(
                    slug=body.repo_slug,
                    host=body.host,
                    ref=body.ref,
                    platform=body.platform,
                    allow_dangerous=body.allow_dangerous or None,
                )
                return {
                    "action": action,
                    "deployment_id": deployment_id,
                    "steps": len(plan.steps),
                    "warning": warning,
                    "plan": plan.dump(),
                }

            if action == "apply":
                # 部署可能跑几分钟：后台执行，页面轮询 /deployments 看结果。
                tasks: set[asyncio.Task] = getattr(request.app.state, "guide_tasks", None) or set()
                request.app.state.guide_tasks = tasks
                task = asyncio.create_task(
                    service.apply(
                        slug=body.repo_slug,
                        host=body.host,
                        ref=body.ref,
                        platform=body.platform,
                        allow_dangerous=body.allow_dangerous or None,
                    )
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                return {"action": action, "accepted": True, "host": body.host}

            if action == "check":
                latest = (
                    repo.get_deployment(int(body.deployment_id))
                    if body.deployment_id
                    else _latest_any_status(repo, int(service.host(body.host).id), body.repo_slug)
                )
                if latest is None:
                    return {"action": action, "deployment": None}
                return {
                    "action": action,
                    "deployment": {
                        "id": latest.id,
                        "status": latest.status,
                        "release": latest.release,
                        "url": latest.url,
                        "health": latest.health_json,
                        "error": latest.error,
                    },
                    "steps": [
                        {
                            "seq": step.seq,
                            "step_id": step.step_id,
                            "status": step.status,
                            "exit_code": step.exit_code,
                            "error": step.error,
                        }
                        for step in repo.steps_of(int(latest.id))
                    ],
                }

            # rollback
            target = body.deployment_id
            if target is None:
                latest = repo.latest_deployment(
                    host_id=int(service.host(body.host).id), repo_slug=body.repo_slug
                )
                if latest is None:
                    raise DeployError("没有可回滚的部署记录")
                target = int(latest.id)
            report = await service.rollback(int(target))
            return {"action": action, "report": report.model_dump(mode="json")}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成人话错误
            raise _http_error(exc) from exc


def register_guide_pages(app: FastAPI, settings: Any) -> None:
    """挂上 ``/ops/guide`` 页面。鉴权沿用现有页面守卫（``/ops`` 前缀已纳入）。"""

    @app.get("/ops/guide")
    async def guide_page(request: Request, repo_slug: str = "", host: str = "") -> Any:
        st = getattr(request.app.state, "settings", None) or settings
        repo = _deploy_repo(request)
        effective_host = host or (repo.list_hosts(enabled_only=True)[0].name if repo.list_hosts(enabled_only=True) else "")
        row = repo.get_guide(repo_slug=repo_slug, host=effective_host) if repo_slug else (
            repo.latest_guide_for_host(effective_host) if effective_host else None
        )
        guide = _guide_view(row) if row is not None else None
        if guide is None and repo_slug:
            # 没生成过就先用兜底模板渲染：页面不该是一片空白。
            fallback = fallback_guide(repo_slug, effective_host)
            guide = {
                "id": None,
                "repo_slug": repo_slug,
                "host": effective_host,
                "mode": fallback.mode.value,
                "source": "fallback",
                "notes": "尚未生成针对本仓库的引导，下面是通用步骤",
                "updated_at": None,
                "stages": [s.model_dump(mode="json") for s in fallback.stages],
            }
        return request.app.state.templates.TemplateResponse(
            request,
            "guide.html",
            _page_context(
                request,
                nav="ops",
                guide=guide,
                repo_slug=repo_slug or (guide or {}).get("repo_slug", ""),
                host=effective_host,
                hosts=[h.name for h in repo.list_hosts()],
                repos=[r.slug for r in st.enabled_repos()],
                deploy_enabled=bool(st.deploy.enabled),
                actions=list(GUIDE_ACTIONS),
            ),
        )