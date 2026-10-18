"""FastAPI 应用入口（Web 仪表盘 + REST API）。

形态（Q14 选 B：只读 + 操作）
----------------------------
* **只读**：列表、筛选、详情、评分理由、统计、导出。
* **操作**：手动重评、flush 队列、批准提 PR、批准合并、触发扫描。
* 仓库与密钥管理界面（Q14:C）**尚未实现**。原先这里写着「默认关闭，需显式打开
  ``web.allow_repo_management``」，但那个开关背后没有任何路由——属于「配置写了却
  不生效」，已随 P2 清理一并删除（要做得另开一个特性，而不是留个空开关）。

鉴权：``api.auth_required=true`` 时所有 ``/api`` 请求必须带
``Authorization: Bearer <FISSUE_API_TOKEN>``。
"""

from __future__ import annotations

import functools
import ipaddress
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..config import Settings, load_settings
from ..errors import FissueError
from ..lockfile import WRITE_GATE, single_flight
from ..logging_setup import get_logger, setup_logging
from ..models import Category, ItemStatus, ItemType, Platform, Priority, QueueName
from .hooks import parse_event, signature_ok

log = get_logger(__name__)

TEMPLATES_DIR = __import__("pathlib").Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class FlushRequest(BaseModel):
    queue: str | None = Field(default=None, description="verify / fix_bug / fix_feature，留空表示全部")
    force: bool = True


class EvalRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, description="要重评的条目 key；留空则取未评测条目")
    deep: bool = False
    limit: int = 20


class FixRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    dry_run: bool = True
    limit: int = 5


class ScanRequest(BaseModel):
    repo: str | None = None
    full: bool = False
    limit: int | None = None


class ApproveRequest(BaseModel):
    note: str = ""


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def _enum_param(value: str | None, enum_cls: Any, *, name: str) -> Any:
    """查询参数 → 枚举；非法取值返回 **400**（原先是 500 + 一堆堆栈）。

    FastAPI 的 Query 只做 str→str，非法值会在处理函数里抛 `ValueError`，被兜成 500——可这是
    **客户端**的参数错误，应当是 4xx，而且要明确告诉对方可选值有哪些。
    """
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = "、".join(str(m.value) for m in enum_cls)
        raise HTTPException(
            status_code=400, detail=f"{name} 取值非法：{value!r}；可选：{allowed}"
        ) from exc


async def _run_round_after_hook(settings: Settings) -> None:
    """webhook 触发的后台一轮：抓取 → 评测 → 验证 → flush → 修复。

    为什么跑**整轮**而不是只抓那一条：条目的后续处理（打标签、派发修复）本来就是独立循环，
    只有跑完一轮它才真正走完整条链路。写锁被占时**直接跳过**——说明已经有写进程在跑，
    下一轮会把它带上，排队只会互相等（也避免 webhook 风暴把机器堆满）。
    """
    from ..service.daemon import FissueService

    try:
        out = await FissueService(settings).run_once()
        log.info("webhook 触发的一轮完成：%s", out)
    except FissueError as exc:
        log.info("webhook 触发的一轮跳过（已有写进程在跑）：%s", exc)
    except Exception:
        log.exception("webhook 触发的一轮失败")


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。"""
    st = settings or load_settings()
    setup_logging(st.app.log_level)
    if st.api.enabled and not st.api.auth_required:
        # 为什么告警在这里发：这里才是「即将对外提供 API」的那一刻，而 `Settings._validate`
        # 是每条命令都会走的构造期路径。（这条告警曾经打坏过 `fissue status --json` 的输出，
        # 根因是日志写到了 stdout；已在 `logging_setup` 修正为一律 stderr。）
        #
        # 为什么必须提醒：启动检查只能看**绑定地址**，看不见反向代理——`web.host=127.0.0.1`
        # + 同机 nginx 转到本端口时，无鉴权的写控制面（打平台标签、推进修复队列）照样对外。
        # 拦不了（拦了「绑本机、不开 token」这个默认用法就没了），但必须说清。
        log.warning(
            "未启用 API 鉴权（未设 FISSUE_API_TOKEN）：写操作入口（%s/queues/flush、"
            "%s/actions/*）对本机任何进程开放；若在本端口前挂了反向代理，等于把它一并"
            "开放出去。要对外提供服务请务必设置 FISSUE_API_TOKEN。",
            st.api.prefix,
            st.api.prefix,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动时确保表存在，并持有数据库连接
        from ..store.db import build_database
        from ..store.repository import Repository

        database = build_database(st)
        database.create_all()
        app.state.settings = st
        app.state.db = database
        app.state.repo = Repository(database)
        log.info("Web/API 已就绪（只读=%s，API 前缀=%s）", st.web.read_only, st.api.prefix)
        yield
        log.info("Web/API 关闭")

    application = FastAPI(
        title="Fissue",
        description="Issue/PR 的 AI 评测、沙盒验证与自动修复",
        version=__import__("fissue").__version__,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    application.state.templates = templates

    _register_api(application, st)
    _register_web(application, st, templates)
    return application


# ---------------------------------------------------------------------------
# 鉴权与工具
# ---------------------------------------------------------------------------


def _auth_dependency(settings: Settings):
    async def check(request: Request) -> None:
        if not settings.api.auth_required:
            return
        header = request.headers.get("authorization", "")
        token = settings.api.token
        if not token:
            raise HTTPException(status_code=500, detail="服务端未配置 FISSUE_API_TOKEN")
        if not header.lower().startswith("bearer ") or header[7:].strip() != token:
            raise HTTPException(status_code=401, detail="未授权")

    return check


def _require_writable(settings: Settings) -> None:
    if settings.web.read_only:
        raise HTTPException(status_code=403, detail="当前为只读模式（web.read_only=true）")


#: 反向代理常见的转发头。无鉴权时见到它们就拒绝写请求——见 :func:`_reject_proxied_write`。
_FORWARD_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
)


def _reject_proxied_write(request: Request, settings: Settings) -> None:
    """未启用鉴权时，拒绝「看起来经过了反向代理」的写请求。

    ``Settings._validate`` 只能看**绑定地址**，因此 ``web.host=127.0.0.1`` + 同机 nginx
    反代到本端口时，无鉴权的写操作控制面（去平台打标签、把条目推进修复队列）其实已经
    对外开放，而启动检查看不见这一半。这里见到转发头就拒。

    必须说清楚边界：**这是补刀，不是安全边界**。边界是 ``FISSUE_API_TOKEN``——同机代理
    若一个转发头都不设，请求与「本机用户直接访问」在 HTTP 层无法区分，只有令牌能真正
    兜住。转发头命中只是让常见配置（nginx 的 ``proxy_set_header``）从「静默开放」变成
    「响亮拒绝」。对端来源见 :func:`_reject_remote_write`，那是另一道。
    """
    if settings.api.auth_required:
        return          # 有鉴权，经不经过代理都无所谓
    hit = [h for h in _FORWARD_HEADERS if request.headers.get(h)]
    if hit:
        raise HTTPException(
            status_code=403,
            detail=(
                f"拒绝写操作：请求带有反向代理转发头（{'、'.join(hit)}），"
                "而当前未启用 API 鉴权。请设置 FISSUE_API_TOKEN 后再经代理访问。"
            ),
        )


def _reject_remote_write(request: Request, settings: Settings) -> None:
    """未启用鉴权时，**来源不是回环**的写请求一律拒绝。

    为什么还需要这一道：「对外可达却没鉴权」本来在启动时就会被拒（见 ``Settings._validate``），
    但那个检查读的是 ``web.host``——**把 ``create_app()`` 嵌进别人的服务器**（或换个
    绑定地址、或经 `docker -p` 之类暴露出去）时，配置里写的仍是 `127.0.0.1`，检查就看不见了。
    这一道直接看**对端地址**，把那种情况兜住。

    拿不到可解析的对端地址时（测试客户端 ``testclient``、Unix socket 等）不在这里判——
    「不知道」不等于「可疑」，不能因此把本机正常用法也拦掉。
    """
    if settings.api.auth_required:
        return
    client = request.client
    host = (client.host if client else "") or ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return
    if not addr.is_loopback:
        raise HTTPException(
            status_code=403,
            detail=(
                f"拒绝写操作：请求来自 {host}，不是本机回环地址，而当前未启用 API 鉴权。"
                "要对外开放请设置 FISSUE_API_TOKEN。"
            ),
        )


def _exclusive_write(fn):
    """写端点的互斥包装：进程内闸门 + 跨进程 flock；拿不到一律 409。

    为什么写端点必须加：``/queues/flush``、``/actions/*`` 会真去平台上打标签、把条目推进
    修复队列，而它们原先**完全不在互斥保护内**——独立 ``fissue web`` / ``serve`` 进程不持锁，
    同进程（``fissue serve --with-web`` 把 uvicorn 起在同一事件循环里）又走 `_hold_depth` 的重入旁路。

    两道闸门缺一不可：

    * ``WRITE_GATE`` 管同进程：uvicorn 的请求任务与 daemon 的 flush/fix 循环；
    * ``single_flight`` 管跨进程：独立 web 进程与 CLI / daemon 之间。
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        request = kwargs.get("request")
        if request is None:
            request = next((a for a in args if isinstance(a, Request)), None)
        if request is None:  # pragma: no cover —— 只会在端点签名被改坏时走到
            return await fn(*args, **kwargs)
        settings = _settings(request)
        _reject_proxied_write(request, settings)
        _reject_remote_write(request, settings)
        try:
            async with WRITE_GATE.hold_or_fail(action="Web/API 写操作"):
                with single_flight(settings, action="Web/API 写操作"):
                    return await fn(*args, **kwargs)
        except FissueError as exc:
            # 409 而不是 500：这是「此刻有别处在写，你稍后再来」，不是服务故障
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return wrapper


def _ensure_state(request: Request) -> None:
    """惰性初始化 app.state。

    正常情况下由 lifespan 完成；但测试用 ``TestClient`` 若未作为上下文管理器使用，
    lifespan 不会执行，这里做一次兜底，避免 500。
    """
    state = request.app.state
    if getattr(state, "repo", None) is not None:
        return
    from ..store.db import build_database
    from ..store.repository import Repository

    settings = getattr(state, "settings", None) or load_settings()
    database = build_database(settings)
    database.create_all()
    state.settings = settings
    state.db = database
    state.repo = Repository(database)


def _repo(request: Request):
    _ensure_state(request)
    return request.app.state.repo


def _db(request: Request):
    _ensure_state(request)
    return request.app.state.db


def _settings(request: Request) -> Settings:
    _ensure_state(request)
    return request.app.state.settings


def _ctx(request: Request):
    """为写操作临时构造 RuntimeContext（复用 app.state 里的 db）。"""
    from ..pipeline.context import RuntimeContext

    _ensure_state(request)
    settings: Settings = request.app.state.settings
    ctx = RuntimeContext.create(settings)
    # 复用已有引擎，避免重复连接池
    ctx.db = request.app.state.db
    ctx.repo = request.app.state.repo
    ctx.evaluator.repo = ctx.repo
    ctx.verifier.repo = ctx.repo
    ctx.generator  # noqa: B018  —— 触发构造
    return ctx


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


def _register_api(app: FastAPI, settings: Settings) -> None:
    if not settings.api.enabled:
        # api.enabled=false：不注册任何 REST 路由（页面仍在，只是没有 /api/v1/*）。
        log.info("api.enabled=false：不注册 REST API 路由")
        return

    prefix = settings.api.prefix
    auth = _auth_dependency(settings)

    # -- 统计 -------------------------------------------------------------

    @app.get(f"{prefix}/stats")
    async def stats(request: Request, _: None = Depends(auth)) -> dict[str, Any]:
        from ..store import export as exp

        repo = _repo(request)
        data = exp.summarize(repo)
        data["queues"] = repo.queue_stats()
        data["llm_usage_today"] = repo.usage_today()
        data["repos"] = [r.slug for r in _settings(request).enabled_repos()]
        return data

    # -- 条目 -------------------------------------------------------------

    @app.get(f"{prefix}/items")
    async def list_items(
        request: Request,
        repo_slug: str | None = Query(None),
        item_type: str | None = Query(None, description="issue / pr"),
        status: str | None = Query(None),
        category: str | None = Query(None),
        priority: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _: None = Depends(auth),
    ) -> dict[str, Any]:
        from ..store import export as exp

        r = _repo(request)
        items = r.list_items(
            repo_slug=repo_slug,
            item_type=_enum_param(item_type, ItemType, name="item_type"),
            status=_enum_param(status, ItemStatus, name="status"),
            category=_enum_param(category, Category, name="category"),
            priority=_enum_param(priority, Priority, name="priority"),
            limit=limit,
            offset=offset,
        )
        evals = r.evaluations_for([i.key for i in items])
        # 列表路径不加载评论（`list_items`）→ `len(i.comments)` 恒为 0，这里补一条聚合计数
        counts = r.comment_counts([i.key for i in items])
        return {
            "count": len(items),
            "items": [
                exp.item_to_dict(i, evals.get(i.key), extra={"comments": counts.get(i.key, 0)})
                for i in items
            ],
        }

    @app.get(f"{prefix}/items/{{key:path}}")
    async def get_item(request: Request, key: str, _: None = Depends(auth)) -> dict[str, Any]:
        from ..store import export as exp

        r = _repo(request)
        item = r.get_item(key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"条目不存在：{key}")
        evaluation = r.latest_evaluation(key)
        data = exp.item_to_dict(item, evaluation, batch=r.latest_batch_conclusion(key))
        data["detail_url"] = f"/item/{quote(key, safe='')}"
        verifier = r.latest_verifier(key)
        data["verifier"] = (
            {
                "kind": verifier[1].kind.value,
                "name": verifier[1].name,
                "command": verifier[1].command,
                "files": list(verifier[1].files),
                "checklist": verifier[1].checklist,
                "notes": verifier[1].notes,
            }
            if verifier
            else None
        )
        data["verifier_runs"] = [
            {
                "stage": run.stage,
                "outcome": run.outcome.value,
                "exit_code": run.exit_code,
                "duration_seconds": run.duration_seconds,
                "f2p_ok": run.f2p_ok,
                "created_at": run.created_at.isoformat(),
            }
            for run in r.verifier_runs(key, limit=20)
        ]
        data["regression"] = exp.regression_summary(
            r.verifier_runs(key, limit=20),
            mode=request.app.state.settings.verifier.regression_gate,
        )
        data["fix_attempts"] = [
            {
                "outcome": a.outcome.value,
                "branch": a.branch,
                "pr_url": a.pr_url,
                "rounds": a.rounds,
                "error": a.error,
                "report_path": a.report_path,
                "created_at": a.created_at.isoformat(),
            }
            for a in r.fix_attempts(key)
        ]
        return data

    # -- 队列 -------------------------------------------------------------

    @app.get(f"{prefix}/queues")
    async def queues(request: Request, _: None = Depends(auth)) -> dict[str, Any]:
        from ..pipeline.queue import QueueManager

        ctx = _ctx(request)
        qm = QueueManager(ctx)
        return {
            "stats": qm.stats(),
            "pending": {q.value: qm.pending_count(q) for q in QueueName},
            "decisions": {
                q.value: {
                    "should_flush": (d := qm.should_flush(q)).should_flush,
                    "reason": d.reason,
                    "done_count": d.done_count,
                }
                for q in QueueName
            },
        }

    @app.post(f"{prefix}/queues/flush")
    @_exclusive_write
    async def flush_queue(
        request: Request, body: FlushRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        from ..pipeline.flush import FlushProcessor
        from ..pipeline.queue import QueueManager

        ctx = _ctx(request)
        qm = QueueManager(ctx)
        processor = FlushProcessor(ctx)
        handler = processor.make_handler()

        targets = [QueueName(body.queue)] if body.queue else list(QueueName)
        results: list[dict[str, Any]] = []
        for q in targets:
            decision = qm.should_flush(q, force=body.force)
            if not decision.should_flush:
                results.append({"queue": q.value, "flushed": 0, "reason": decision.reason})
                continue
            outcome = await qm.flush(q, handler=handler, force=body.force)
            results.append(
                {
                    "queue": q.value,
                    "flushed": outcome.flushed,
                    "conclusions": len(outcome.conclusions),
                    "error": outcome.error,
                }
            )
        return {"results": results}

    # -- 动作 -------------------------------------------------------------

    @app.post(f"{prefix}/actions/evaluate")
    @_exclusive_write
    async def action_evaluate(
        request: Request, body: EvalRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        from ..pipeline.stages import EvalStage

        ctx = _ctx(request)
        if body.keys:
            items = ctx.repo.items_by_keys(body.keys)
        else:
            items = ctx.repo.items_needing_eval(limit=body.limit)
        if not items:
            return {"evaluated": 0, "results": []}
        result = await EvalStage(ctx).run_batch(items, deep=body.deep)
        return {
            "evaluated": result.evaluated,
            "failed": result.failed,
            "results": [
                {
                    "key": r.key,
                    "category": r.evaluation.category.value if r.evaluation else None,
                    "priority": r.evaluation.priority.value if r.evaluation else None,
                    "error": r.error,
                }
                for r in result.results
            ],
        }

    @app.post(f"{prefix}/actions/verify")
    @_exclusive_write
    async def action_verify(
        request: Request, body: EvalRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        from ..pipeline.stages import PRVerifyStage, VerifyStage

        ctx = _ctx(request)
        items = ctx.repo.items_by_keys(body.keys) if body.keys else ctx.repo.list_items(
            status=ItemStatus.EVALUATED, limit=body.limit
        )
        stage_issue, stage_pr = VerifyStage(ctx), PRVerifyStage(ctx)
        out = []
        for item in items:
            ev = ctx.repo.latest_evaluation(item.key)
            stage = stage_issue if item.item_type is ItemType.ISSUE else stage_pr
            try:
                r = await stage.run(item, ev)
            except Exception as exc:
                # 验证阶段现在把瞬时故障**上抛**（好让常驻服务那侧带退避重试，见 VerifyStage），
                # 所以这里必须逐条兜住——否则一条失败会让整批结果全丢、端点直接 500。
                out.append(
                    {
                        "key": item.key,
                        "reproduced": False,
                        "queued": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            out.append(
                {
                    "key": r.key,
                    "reproduced": bool(r.verifier_result and r.verifier_result.reproducible),
                    "queued": r.queued.value if r.queued else None,
                    "error": r.error or r.skipped_reason,
                }
            )
        return {"processed": len(out), "results": out}

    @app.post(f"{prefix}/actions/fix")
    @_exclusive_write
    async def action_fix(
        request: Request, body: FixRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        if not _settings(request).auto_fix.enabled:
            raise HTTPException(status_code=403, detail="自动修复已禁用（auto_fix.enabled=false）")
        from ..fixer.autofix import AutoFixer

        ctx = _ctx(request)
        fixer = AutoFixer(ctx)
        results = []
        if body.keys:
            for key in body.keys:
                item = ctx.repo.get_item(key)
                if item is None:
                    results.append({"key": key, "error": "不存在"})
                    continue
                a = await fixer.fix_item(item, dry_run=body.dry_run)
                results.append(_attempt_payload(a))
        else:
            report = await fixer.fix_candidates(limit=body.limit, dry_run=body.dry_run)
            results = [_attempt_payload(a) for a in report.attempts]
        return {"attempted": len(results), "dry_run": body.dry_run, "results": results}

    @app.post(f"{prefix}/actions/scan")
    @_exclusive_write
    async def action_scan(
        request: Request, body: ScanRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        from ..pipeline.fetcher import Fetcher
        from ..platforms.registry import resolve_repos

        ctx = _ctx(request)
        settings = _settings(request)
        repos = resolve_repos(settings, [body.repo] if body.repo else None)
        if not repos:
            raise HTTPException(status_code=400, detail="没有可扫描的仓库")
        stats = await Fetcher(ctx).fetch_many(repos, full=body.full, limit=body.limit)
        return {
            "fetched": stats.fetched,
            "created": stats.created,
            "updated": stats.updated,
            "unchanged": stats.unchanged,
            "error": stats.error,
        }

    @app.post(f"{prefix}/actions/approve-fix")
    @_exclusive_write
    async def action_approve_fix(
        request: Request, body: ApproveRequest, key: str = Query(...), _: None = Depends(auth)
    ) -> dict[str, Any]:
        """批准并执行某条目的自动修复（人工闸门之后的放行）。"""
        _require_writable(_settings(request))
        from ..fixer.autofix import AutoFixer

        ctx = _ctx(request)
        item = ctx.repo.get_item(key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"条目不存在：{key}")
        attempt = await AutoFixer(ctx).fix_item(item, dry_run=False)
        return _attempt_payload(attempt)

    @app.post(f"{prefix}/actions/approve-merge")
    @_exclusive_write
    async def action_approve_merge(
        request: Request, body: ApproveRequest, key: str = Query(...), _: None = Depends(auth)
    ) -> dict[str, Any]:
        """人工确认「可以合并」——打标签并记录，**不自动合并**（Q1 要求人工合并）。"""
        _require_writable(_settings(request))
        from ..platforms.registry import parse_repo_ref

        ctx = _ctx(request)
        item = ctx.repo.get_item(key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"条目不存在：{key}")
        if item.item_type is not ItemType.PR:
            raise HTTPException(status_code=400, detail="只有 PR 可以批准合并")

        adapter = ctx.adapter_for_platform(item.platform)
        ref = parse_repo_ref(item.repo, item.platform)
        try:
            await adapter.ensure_labels(ref, ["merge-approved"])
            await adapter.add_labels(ref, item.number, ["merge-approved"], ItemType.PR)
            if body.note:
                await adapter.comment(ref, item.number, f"✅ 维护者已确认可合并：{body.note}", ItemType.PR)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"打标签/评论失败：{exc}") from exc

        ctx.repo.set_item_status(key, ItemStatus.LABELED)
        return {"key": key, "approved": True, "note": "已打 merge-approved 标签，请人工在平台上合并"}

    # -- 导出 -------------------------------------------------------------

    # -- 入站 webhook：平台主动推「新 Issue / 新 PR」 ---------------------------
    #
    # 这个端点**刻意不挂** `Depends(auth)`、`_require_writable` 与两道写补刀：平台既不会带
    # `Authorization`，也不会带我们的转发头，它就带自己的签名——把那些守卫挂上来，在真实部署
    # （尤其挂了反向代理时）这个端点会永远收不到事件。边界因此换成了：
    # **签名/密钥 + 只认已登记且启用的仓库**（配置层与 web/hooks.py 各有一道 fail-closed）。

    @app.post(f"{prefix}/webhooks/{{platform}}", status_code=202)
    async def webhook(
        platform: str, request: Request, background: BackgroundTasks
    ) -> dict[str, Any]:
        if not settings.webhooks.enabled:
            # 没启用就当它不存在（不告诉外面「这里有个端点」）
            raise HTTPException(status_code=404, detail="webhook 未启用")

        try:
            plat = Platform(platform)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"未知平台：{platform}") from exc

        body = await request.body()
        secret = settings.webhooks.secrets.get(plat)
        if not signature_ok(plat, secret, body, request.headers):
            # 日志里只说「不匹配或未配置」，不回显签名与密钥
            log.warning("webhook 校验失败（%s）：签名不匹配，或该平台未配置密钥", plat.value)
            raise HTTPException(status_code=403, detail="签名校验失败")

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"事件体不是合法 JSON：{exc}") from exc

        event = parse_event(plat, payload)
        if not event.usable:
            log.info("webhook（%s）不处理：%s", plat.value, event.reason)
            return {"accepted": False, "reason": event.reason}

        cfg = next(
            (
                r
                for r in settings.enabled_repos()
                if r.platform is plat and r.slug == event.repo_slug
            ),
            None,
        )
        if cfg is None:
            log.info("webhook（%s）忽略未登记/未启用的仓库：%s", plat.value, event.repo_slug)
            return {"accepted": False, "reason": f"仓库未登记或未启用：{event.repo_slug}"}

        # 立刻回 202，重活交给后台：平台对回调有超时与重试，不能让它们等着我们跑完一轮。
        background.add_task(_run_round_after_hook, settings)
        log.info(
            "webhook（%s）%s %s#%s 已接受，后台触发一轮",
            plat.value, event.action or "-", event.repo_slug, event.number,
        )
        return {
            "accepted": True,
            "repo": event.repo_slug,
            "number": event.number,
            "item_type": event.item_type.value,
            "action": event.action,
            "note": "已在后台触发一轮（抓取 → 评测 → 验证 → flush → 修复）",
        }

    @app.get(f"{prefix}/export")
    async def export_endpoint(
        request: Request,
        fmt: str = Query("json", alias="format"),
        repo_slug: str | None = Query(None),
        limit: int = Query(1000, ge=1, le=10000),
        _: None = Depends(auth),
    ) -> PlainTextResponse:
        from ..store import export as exp

        r = _repo(request)
        if fmt.lower() in ("markdown", "md"):
            content = exp.export_markdown(r, repo_slug=repo_slug, limit=limit)
            media = "text/markdown; charset=utf-8"
        else:
            content = exp.export_json(r, repo_slug=repo_slug, limit=limit)
            media = "application/json; charset=utf-8"
        return PlainTextResponse(content, media_type=media)

    @app.get(f"{prefix}/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __import__("fissue").__version__}


def _attempt_payload(attempt) -> dict[str, Any]:
    return {
        "key": attempt.item_key,
        "outcome": attempt.outcome.value,
        "rounds": attempt.rounds,
        "branch": attempt.branch,
        "pr_url": attempt.pr_url,
        "patch_path": attempt.patch_path,
        "report_path": attempt.report_path,
        "changed_files": attempt.changed_files,
        "error": attempt.error,
    }


# ---------------------------------------------------------------------------
# Web 仪表盘
# ---------------------------------------------------------------------------


def _register_web(app: FastAPI, settings: Settings, templates: Jinja2Templates) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        repo_slug: str | None = Query(None),
        status: str | None = Query(None),
        category: str | None = Query(None),
        priority: str | None = Query(None),
        item_type: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ) -> HTMLResponse:
        from ..store import export as exp

        r = _repo(request)
        items = r.list_items(
            repo_slug=repo_slug,
            status=_enum_param(status, ItemStatus, name="status"),
            category=_enum_param(category, Category, name="category"),
            priority=_enum_param(priority, Priority, name="priority"),
            item_type=_enum_param(item_type, ItemType, name="item_type"),
            limit=limit,
        )
        evals = r.evaluations_for([i.key for i in items])
        counts = r.comment_counts([i.key for i in items])
        rows = [
            exp.item_to_dict(i, evals.get(i.key), extra={"comments": counts.get(i.key, 0)})
            for i in items
        ]
        summary = exp.summarize(r, repo_slug=repo_slug)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": rows,
                "summary": summary,
                "settings": _settings(request),
                "filters": {
                    "repo_slug": repo_slug or "",
                    "status": status or "",
                    "category": category or "",
                    "priority": priority or "",
                    "item_type": item_type or "",
                    "limit": limit,
                },
                "repos": [x.slug for x in _settings(request).enabled_repos()],
                "read_only": _settings(request).web.read_only,
            },
        )

    @app.get("/item/{key:path}", response_class=HTMLResponse)
    async def item_detail(request: Request, key: str) -> HTMLResponse:
        from ..store import export as exp

        r = _repo(request)
        item = r.get_item(key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"条目不存在：{key}")
        evaluation = r.latest_evaluation(key)
        verifier = r.latest_verifier(key)
        return templates.TemplateResponse(
            request,
            "item.html",
            {
                "data": exp.item_to_dict(item, evaluation, batch=r.latest_batch_conclusion(key)),
                "item": item,
                "evaluation": evaluation,
                "conclusion": r.latest_batch_conclusion(key),
                "verifier": verifier[1] if verifier else None,
                "runs": r.verifier_runs(key, limit=20),
                "fixes": r.fix_attempts(key),
                "read_only": _settings(request).web.read_only,
            },
        )


# ---------------------------------------------------------------------------
# 模块级 app（``uvicorn fissue.web.app:app``）
# ---------------------------------------------------------------------------


def _lazy_app() -> FastAPI:  # pragma: no cover
    return create_app()
