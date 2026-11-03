"""部署运维的 Web 层：REST 端点 + ``/ops`` 页面。

**只做参数解析与输出**——克隆仓库、AI 出计划、SSH 执行、健康探针、回滚全在
:mod:`fissue.deploy.service` / ``runner`` 里。CLI 走同一条链（见 ``cli/deploy.py``），
两处各写一遍的话「CLI 能跑通但 Web 不行」只是时间问题。

安全闸门与现有写路径一致（docs/DEPLOY.md §9）：

* 每个写端点叠 ``@_exclusive_write``（进程内闸门 + 跨进程 flock，拿不到就是 409）
  并调 ``_require_writable``（``web.read_only=true`` 时 403）；
* ``deploy.enabled=false`` 时**读端点照常**（历史还要能查），**写端点一律 409**；
* 凭据只在 ``POST {prefix}/hosts`` 的请求体里出现一次，入库前用
  :class:`fissue.deploy.secretbox.CredentialBox` 加密；**任何响应都不回显它**
  （密文也不回：回密文等于把「可离线爆破的凭据」摆到浏览器上）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from ..deploy.guard import GuardError
from ..deploy.planner import PlannerError
from ..deploy.runner import DeployError
from ..deploy.secretbox import CredentialBox
from ..deploy.service import DeployService
from ..deploy.ssh import SSHError
from ..logging_setup import get_logger
from ..store.deploy_repo import DeployRepository
from .app import (
    _db,
    _exclusive_write,
    _page_context,
    _require_writable,
    _settings,
)

log = get_logger(__name__)

#: 部署链路上「可以翻译成人话」的错误。SSHError 是 DeployError 的兄弟而不是子类，
#: 所以要一起列出来——漏一个就会变成 500 + 堆栈，用户看不出是自己填错了地址。
DEPLOY_ERRORS = (DeployError, GuardError, PlannerError, SSHError)


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class HostCreateRequest(BaseModel):
    """登记一台主机。``credential`` 是**明文**，只在这一跳出现。"""

    name: str = Field(min_length=1, max_length=128, description="主机别名，CLI/Web 都用它引用")
    address: str = Field(min_length=1, max_length=255, description="IP 或域名")
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(default="root", min_length=1, max_length=128)
    auth_method: Literal["key", "password"] = "key"
    #: 私钥正文或密码。**响应里绝不回显**（见模块说明）。
    credential: str = Field(min_length=1, description="私钥正文或密码（明文，仅此一次）")
    app_root: str | None = Field(default=None, description="该机上的发布根目录，缺省用 deploy.app_root")


class DeploymentCreateRequest(BaseModel):
    """触发一次部署。**不要**期待它同步返回结果（见端点说明）。"""

    repo_slug: str = Field(min_length=1, max_length=511)
    host: str = Field(min_length=1, max_length=128)
    ref: str | None = None
    platform: str | None = None
    allow_dangerous: bool = False


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _deploy_repo(request: Request) -> DeployRepository:
    """部署表与条目表共用同一个 ``app.state.db``（不另开连接池）。"""
    return DeployRepository(_db(request))


def _service(settings: Any, request: Request) -> DeployService:
    return DeployService(settings, _deploy_repo(request))


def _require_deploy_enabled(settings: Any) -> None:
    """写端点的统一前置：能力没开就**不做任何事**，并且说清楚怎么开。

    409 而不是 403：这不是「你没权限」，而是「这个实例此刻没打开这项能力」——
    用户要做的是改配置，不是换账号。
    """
    if not settings.deploy.enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "部署能力未开启：请在 config.yaml 里设置 deploy.enabled=true"
                "（这是能改远端机器的能力，默认关闭；改成 true 后重启 Web 进程生效）"
            ),
        )


def _http_error(exc: Exception) -> HTTPException:
    """把部署链路的异常翻成 HTTP 错误，**只带人话，不带 traceback 也不带凭据**。"""
    if isinstance(exc, HTTPException):  # pragma: no cover - 调用点已直接抛
        return exc
    # 计划被安全闸门拦下 → 409（「这份计划与当前策略冲突」），其余 → 400
    # （「你给的参数有问题」）。两者都是客户端能自己救回来的。
    status = 409 if isinstance(exc, GuardError) else 400
    detail = str(exc).strip() or exc.__class__.__name__
    return HTTPException(status_code=status, detail=detail)


def _host_view(row: Any) -> dict[str, Any]:
    """主机对外视图：**不含 credential**（连密文都不给）。

    与 ``cli/deploy.py::_host_view`` 同一口径：只说明「用什么方式认证」与
    「指纹有没有钉住」。字段名按 Web 契约（``fingerprint`` 而不是列名）。
    """
    return {
        "name": row.name,
        "address": row.address,
        "user": row.user,
        "port": int(row.port or 22),
        "auth_method": row.auth_method,
        "fingerprint": row.host_fingerprint,
        "enabled": bool(row.enabled),
        "app_root": row.app_root,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _deployment_view(row: Any, host_name: str | None) -> dict[str, Any]:
    """部署记录对外视图（列表与详情共用同一套字段，免得两处漂移）。"""
    return {
        "id": int(row.id),
        "host": host_name,
        "host_id": int(row.host_id),
        "repo_slug": row.repo_slug,
        "ref": row.ref,
        "commit_sha": row.commit_sha,
        "status": row.status,
        "release": row.release,
        "previous_release": row.previous_release,
        "service_name": row.service_name,
        "url": row.url,
        "app_root": row.app_root,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _step_view(row: Any) -> dict[str, Any]:
    return {
        "seq": int(row.seq or 0),
        "step_id": row.step_id,
        "kind": row.kind,
        "status": row.status,
        "exit_code": row.exit_code,
        "error": row.error,
        "output_tail": row.output_tail,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _event_view(row: Any) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "level": row.level,
        "message": row.message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _hosts_by_id(repo: DeployRepository) -> dict[int, str]:
    return {int(row.id): row.name for row in repo.list_hosts()}


# ---------------------------------------------------------------------------
# REST 端点
# ---------------------------------------------------------------------------


def register_deploy_api(app: FastAPI, prefix: str, auth: Any) -> None:
    """挂上部署运维的 REST 端点（``{prefix}/hosts`` 与 ``{prefix}/deployments``）。"""

    # -- 主机 -------------------------------------------------------------

    @app.get(f"{prefix}/hosts")
    async def list_hosts(request: Request, _: None = Depends(auth)) -> list[dict[str, Any]]:
        """已登记的主机。**只读**：能力关闭时也照常返回（历史要看）。"""
        repo = _deploy_repo(request)
        return [_host_view(row) for row in repo.list_hosts()]

    @app.post(f"{prefix}/hosts")
    @_exclusive_write
    async def create_host(
        request: Request, body: HostCreateRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """登记（或覆盖）一台主机。

        凭据在这里**唯一一次**以明文出现：立刻 ``CredentialBox.seal`` 后再入库，
        响应只有 ``id`` 与 ``name``——回显（无论明文还是密文）都是把凭据摊到浏览器上。
        """
        settings = _settings(request)
        _require_writable(settings)
        _require_deploy_enabled(settings)

        app_root = (body.app_root or settings.deploy.app_root or "").strip()
        if not app_root.startswith("/") or ".." in app_root.split("/"):
            raise HTTPException(
                status_code=400,
                detail=f"发布根目录必须是绝对路径且不含 '..'：{app_root!r}",
            )

        try:
            sealed = CredentialBox.from_settings(settings).seal(body.credential)
        except Exception as exc:  # noqa: BLE001 - 加密失败是环境问题，要说人话
            raise HTTPException(status_code=400, detail=f"凭据加密失败：{exc}") from exc

        repo = _deploy_repo(request)
        try:
            host_id = repo.upsert_host(
                name=body.name,
                address=body.address,
                credential=sealed,
                port=body.port,
                user=body.user,
                auth_method=body.auth_method,
                app_root=app_root,
            )
        except Exception as exc:  # noqa: BLE001 - 数据库层错误也翻成人话
            raise HTTPException(status_code=400, detail=f"登记主机失败：{exc}") from exc
        # 响应刻意只有这两个字段：凭据不回显，其余信息走 GET /hosts。
        return {"id": host_id, "name": body.name}

    @app.delete(f"{prefix}/hosts/{{name}}")
    @_exclusive_write
    async def delete_host(request: Request, name: str, _: None = Depends(auth)) -> dict[str, Any]:
        """删除主机登记（**不碰远端**：服务还在跑、文件还在盘上）。"""
        settings = _settings(request)
        _require_writable(settings)
        _require_deploy_enabled(settings)

        repo = _deploy_repo(request)
        if not repo.remove_host(name):
            raise HTTPException(status_code=404, detail=f"没有这台主机：{name}")
        return {
            "deleted": name,
            "note": "只删了本地登记（含该机的部署历史与审计流水），远端服务与文件未受影响。",
        }

    @app.post(f"{prefix}/hosts/{{name}}/ping")
    @_exclusive_write
    async def ping_host(request: Request, name: str, _: None = Depends(auth)) -> dict[str, Any]:
        """连一下目标机。**算写操作**：首次连接会把主机指纹 TOFU 钉进库里。"""
        settings = _settings(request)
        _require_writable(settings)
        _require_deploy_enabled(settings)

        service = _service(settings, request)
        try:
            return await service.ping(name)
        except DEPLOY_ERRORS as exc:
            raise _http_error(exc) from exc

    # -- 部署记录 ---------------------------------------------------------

    @app.get(f"{prefix}/deployments")
    async def list_deployments(
        request: Request,
        limit: int = Query(20, ge=1, le=200),
        host: str | None = Query(None, description="只看某台主机"),
        _: None = Depends(auth),
    ) -> list[dict[str, Any]]:
        """最近的部署记录（新的在前）。**只读**：能力关闭时也照常返回。"""
        repo = _deploy_repo(request)
        host_id: int | None = None
        if host:
            row = repo.get_host(host)
            if row is None:
                raise HTTPException(status_code=400, detail=f"没有这台主机：{host}")
            host_id = int(row.id)
        names = _hosts_by_id(repo)
        rows = repo.list_deployments(host_id=host_id, limit=limit)
        return [_deployment_view(row, names.get(int(row.host_id))) for row in rows]

    @app.get(f"{prefix}/deployments/{{deployment_id}}")
    async def get_deployment(
        request: Request, deployment_id: int, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """一次部署的详情：步骤 + 审计事件 + 健康证据。"""
        repo = _deploy_repo(request)
        row = repo.get_deployment(deployment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"没有这次部署：{deployment_id}")
        host_row = repo.get_host_by_id(int(row.host_id))
        data = _deployment_view(row, host_row.name if host_row else None)
        data["health_json"] = row.health_json or {}
        data["steps"] = [_step_view(s) for s in repo.steps_of(deployment_id)]
        data["events"] = [_event_view(e) for e in repo.events_of(deployment_id)]
        return data

    @app.post(f"{prefix}/deployments")
    @_exclusive_write
    async def create_deployment(
        request: Request, body: DeploymentCreateRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """**受理**一次部署，立刻返回。

        一次部署可能跑几分钟（克隆 → 出计划 → SSH 执行 → 健康探针），同步等它跑完
        只会让浏览器超时、让反向代理先断、让「到底有没有开始」变得不可知。所以这里
        用 ``asyncio.create_task`` 把 ``service.apply`` 丢到后台，响应只回
        ``{"accepted": true, ...}``，进度与结果看 ``GET {prefix}/deployments``。

        后台任务失败**只记日志**：请求早就返回了，没有客户端可通知。任务对象存进
        ``app.state.deploy_tasks``（并 ``add_done_callback`` 移除）——不留引用的话
        asyncio 只持弱引用，任务可能被 GC 掉，表现是「部署跑到一半无声消失」。
        """
        settings = _settings(request)
        _require_writable(settings)
        _require_deploy_enabled(settings)

        service = _service(settings, request)

        async def _run() -> None:
            try:
                await service.apply(
                    slug=body.repo_slug,
                    host=body.host,
                    ref=body.ref,
                    platform=body.platform,
                    allow_dangerous=body.allow_dangerous,
                )
            except DEPLOY_ERRORS as exc:
                log.warning("后台部署失败（%s → %s）：%s", body.repo_slug, body.host, exc)
            except Exception as exc:  # noqa: BLE001 - 后台任务没人接得住异常
                log.exception("后台部署出现意外错误（%s → %s）：%s", body.repo_slug, body.host, exc)

        task = asyncio.create_task(_run(), name=f"deploy:{body.repo_slug}->{body.host}")
        tasks = getattr(app.state, "deploy_tasks", None)
        if not isinstance(tasks, set):  # 首次使用时惰性建（lifespan 不必知道它）
            tasks = set()
            app.state.deploy_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        return {"accepted": True, "host": body.host, "repo_slug": body.repo_slug}

    @app.post(f"{prefix}/deployments/{{deployment_id}}/rollback")
    @_exclusive_write
    async def rollback_deployment(
        request: Request, deployment_id: int, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """回滚到上一次成功发布（几十秒级，同步等——用户要的就是这个结果）。"""
        settings = _settings(request)
        _require_writable(settings)
        _require_deploy_enabled(settings)

        service = _service(settings, request)
        try:
            report = await service.rollback(deployment_id)
        except DEPLOY_ERRORS as exc:
            raise _http_error(exc) from exc
        return report.model_dump(mode="json")

    @app.delete(f"{prefix}/deployments/{{deployment_id}}")
    @_exclusive_write
    async def destroy_deployment(
        request: Request, deployment_id: int, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """下线：停服务 + 删 release（``shared/`` 保留，数据不该跟着走）。"""
        settings = _settings(request)
        _require_writable(settings)
        _require_deploy_enabled(settings)

        service = _service(settings, request)
        try:
            report = await service.destroy(deployment_id)
        except DEPLOY_ERRORS as exc:
            raise _http_error(exc) from exc
        return report.model_dump(mode="json")

    @app.get(f"{prefix}/deployments/{{deployment_id}}/logs")
    async def deployment_logs(
        request: Request,
        deployment_id: int,
        step: str | None = Query(None, description="只看某一步的 step_id"),
        _: None = Depends(auth),
    ) -> dict[str, Any]:
        """步骤与审计流水（页面展开一行时拉的就是它）。

        与 ``GET {prefix}/deployments/{id}`` 的区别：这里**不做**主机/计划拼装，
        只回两张表，轮询刷新时可以更频繁地调用。
        """
        repo = _deploy_repo(request)
        if repo.get_deployment(deployment_id) is None:
            raise HTTPException(status_code=404, detail=f"没有这次部署：{deployment_id}")
        steps = [_step_view(s) for s in repo.steps_of(deployment_id)]
        if step:
            steps = [s for s in steps if s["step_id"] == step]
        events = [_event_view(e) for e in repo.events_of(deployment_id)]
        return {"steps": steps, "events": events}


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def register_deploy_pages(app: FastAPI, settings: Any) -> None:
    """挂上 ``/ops`` 运维页。

    鉴权沿用现有页面约定（``page_guard_middleware``，见 ``web/auth.py``），
    上下文走 ``_page_context``——不在这里另造一套「谁能看这个页面」的判定。

    ``settings`` 只作为**兜底**：真值一律现取 ``app.state.settings``（设置页热重载
    会替换那个对象，闭包里这份是启动时的旧货）。
    """

    @app.get("/ops", response_class=HTMLResponse)
    async def ops_page(request: Request) -> Response:
        """部署运维页：主机、部署记录、以及四个操作区块（新增/部署/回滚/下线）。"""
        # 鉴权与其它页面同一口径（回环 / 可信网段 / 会话）。页面守卫中间件按
        # ``auth.PAGE_PREFIXES`` 前缀拦，**`/ops` 必须出现在那张表里**；这里再自查一次是
        # 兜底：漏登记一个前缀的表现是「整页对外可见」，部署历史里有什么不该靠运气。
        from .auth import resolve_identity

        if not resolve_identity(request).allowed:
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

        st = getattr(request.app.state, "settings", None) or settings
        repo = _deploy_repo(request)
        names = _hosts_by_id(repo)

        hosts = [_host_view(row) for row in repo.list_hosts()]
        deployments = [
            _deployment_view(row, names.get(int(row.host_id)))
            for row in repo.list_deployments(limit=50)
        ]
        stats = repo.stats()
        # 页面自身只做「渲染 + 由 JS 调 REST」：所有写操作都走上面那几个端点，
        # 页面上不存在第二条写路径（否则闸门与错误提示会分叉）。
        return request.app.state.templates.TemplateResponse(
            request,
            "ops.html",
            _page_context(
                request,
                nav="ops",
                hosts=hosts,
                deployments=deployments,
                stats=stats,
                deploy_enabled=bool(st.deploy.enabled),
                app_root=st.deploy.app_root,
            ),
        )
