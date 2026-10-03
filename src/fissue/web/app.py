"""FastAPI 应用入口（Web 仪表盘 + REST API）。

形态（Q14 选 B：只读 + 操作）
----------------------------
* **只读**：列表、筛选、详情、评分理由、统计、导出。
* **操作**：手动重评、flush 队列、批准提 PR、批准合并、触发扫描。
* 仓库与密钥管理界面（Q14:C）默认关闭，需显式打开 ``web.allow_repo_management``。

鉴权：``api.auth_required=true`` 时所有 ``/api`` 请求必须带
``Authorization: Bearer <FISSUE_API_TOKEN>``。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..config import Settings, load_settings
from ..errors import FissueError
from ..logging_setup import get_logger, setup_logging
from ..models import Category, ItemStatus, ItemType, Priority, QueueName

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


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。"""
    st = settings or load_settings()
    setup_logging(st.app.log_level)

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
            item_type=ItemType(item_type) if item_type else None,
            status=ItemStatus(status) if status else None,
            category=Category(category) if category else None,
            priority=Priority(priority) if priority else None,
            limit=limit,
            offset=offset,
        )
        evals = r.evaluations_for([i.key for i in items])
        return {
            "count": len(items),
            "items": [exp.item_to_dict(i, evals.get(i.key)) for i in items],
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
        from ..pipeline.context import RuntimeContext
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
        results = []
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
            r = await (stage_issue.run(item, ev) if item.item_type is ItemType.ISSUE else stage_pr.run(item, ev))
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
            await adapter.ensure_label(ref, "merge-approved")
            await adapter.add_labels(ref, item.number, ["merge-approved"], ItemType.PR)
            if body.note:
                await adapter.comment(ref, item.number, f"✅ 维护者已确认可合并：{body.note}", ItemType.PR)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"打标签/评论失败：{exc}") from exc

        ctx.repo.set_item_status(key, ItemStatus.LABELED)
        return {"key": key, "approved": True, "note": "已打 merge-approved 标签，请人工在平台上合并"}

    # -- 导出 -------------------------------------------------------------

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
            status=ItemStatus(status) if status else None,
            category=Category(category) if category else None,
            priority=Priority(priority) if priority else None,
            item_type=ItemType(item_type) if item_type else None,
            limit=limit,
        )
        evals = r.evaluations_for([i.key for i in items])
        rows = [exp.item_to_dict(i, evals.get(i.key)) for i in items]
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
