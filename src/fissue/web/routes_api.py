"""REST API 路由注册（``/api/<prefix>/*``）。

从 :mod:`fissue.web.app` 拆出来：那个模块同时装着应用工厂、写闸门、长任务实现体
与全部路由，改任何一个端点都得在近两千行的文件里翻。这里只做一件事——**把端点挂到
应用上**，共享的工具与工作体仍留在 app.py（它们同时服务 Web 页面与后台作业）。

导入方向是单向的：本模块 ``from .app import ...``，而 app 只在 ``create_app`` 内部
惰性导入本模块，因此不存在循环导入。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..errors import FissueError
from ..logging_setup import get_logger
from ..models import Category, ItemStatus, ItemType, Platform, Priority, QueueName
from .app import (
    _JOB_WORK,
    ApproveRequest,
    EvalRequest,
    FixRequest,
    FlushRequest,
    ForceRequest,
    JobRequest,
    PurgeRequest,
    ScanRequest,
    _aggregates_of,
    _attempt_payload,
    _auth_dependency,
    _bind_job,
    _ctx,
    _ensure_state,
    _enum_param,
    _exclusive_write,
    _quiet_report,
    _reject_proxied_write,
    _reject_remote_write,
    _repo,
    _require_writable,
    _run_round_after_hook,
    _settings,
    _work_evaluate,
    _work_fix,
    _work_flush,
    _work_scan,
    _work_verify,
)
from .hooks import parse_event, signature_ok
from .markdown import render_job_event
from .status import collect as collect_status

log = get_logger(__name__)


def register_api(app: FastAPI, settings: Any) -> None:
    if not settings.api.enabled:
        log.info("api.enabled=false：不注册 REST API 路由")
        return

    prefix = settings.api.prefix
    auth = _auth_dependency()
    # 按域拆开注册。原先这里是一个 566 行的函数：路由一多，就没人能一眼看出「哪个域有
    # 哪些端点、各自的闸门挂在哪」。切分只按域移动代码，注册顺序保持原样。
    _register_api_observability(app, prefix, auth)
    _register_api_items(app, prefix, auth)
    _register_api_item_admin(app, prefix, auth)
    _register_api_queues(app, prefix, auth)
    _register_api_actions(app, prefix, auth)
    _register_api_jobs(app, prefix, auth)
    _register_api_webhooks(app, prefix, auth)
    _register_api_misc(app, prefix, auth)


def _register_api_observability(app: FastAPI, prefix: str, auth: Any) -> None:
    """统计 / 状态：只读聚合（状态页每 2 秒轮询的就是它）。"""
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

    @app.get(f"{prefix}/status")
    async def status_endpoint(
        request: Request,
        sandbox: bool = Query(
            False, description="是否真的探测一次沙盒（会调用 Docker，较慢）；状态页默认不来打扰"
        ),
        _: None = Depends(auth),
    ) -> dict[str, Any]:
        """实时状态快照：写闸门 / 数据库 / 沙盒 / 队列 / 作业 / 用量 / 最近抓取 / 告警。

        「状态监测」页每 2 秒轮询的就是它。拼装逻辑在 ``web/status.py``——`fissue status`
        将来也读同一份口径，不各拼一遍。
        """
        _ensure_state(request)
        return collect_status(
            request.app,
            _settings(request),
            _repo(request),
            with_sandbox=sandbox,
            aggregates=_aggregates_of(request.app),
        )


def _register_api_items(app: FastAPI, prefix: str, auth: Any) -> None:
    """条目：列表 / 详情（只读）。"""
    # -- 条目 -------------------------------------------------------------

    @app.get(f"{prefix}/items")
    async def list_items(
        request: Request,
        repo_slug: str | None = Query(None),
        item_type: str | None = Query(None, description="issue / pr"),
        status: str | None = Query(None),
        category: str | None = Query(None),
        priority: str | None = Query(None),
        q: str | None = Query(None, description="模糊搜索：标题 / 正文 / 作者"),
        sort: str = Query("updated_at", description="排序键，见 ITEM_SORT_COLUMNS"),
        order: str = Query("desc", description="asc / desc"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _: None = Depends(auth),
    ) -> dict[str, Any]:
        from ..store import export as exp

        r = _repo(request)
        r_type = _enum_param(item_type, ItemType, name="item_type")
        r_status = _enum_param(status, ItemStatus, name="status")
        r_category = _enum_param(category, Category, name="category")
        r_priority = _enum_param(priority, Priority, name="priority")
        desc = order.lower() != "asc"
        items = r.list_items(
            repo_slug=repo_slug,
            item_type=r_type,
            status=r_status,
            category=r_category,
            priority=r_priority,
            query=q,
            limit=limit,
            offset=offset,
            order_by=sort,
            desc=desc,
        )
        evals = r.evaluations_for([i.key for i in items])
        # 列表路径不加载评论（`list_items`）→ `len(i.comments)` 恒为 0，这里补一条聚合计数
        counts = r.comment_counts([i.key for i in items])
        return {
            "count": len(items),
            # 总数与明细同口径（同一个 _item_filtered）：`count` 是**这一页**的条数，
            # 分页要靠 `total` 才知道后面还有没有。`offset + count >= total` 即到底。
            "total": r.count_items(
                repo_slug=repo_slug,
                item_type=r_type,
                status=r_status,
                category=r_category,
                priority=r_priority,
                query=q,
            ),
            "offset": offset,
            "limit": limit,
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


def _register_api_item_admin(app: FastAPI, prefix: str, auth: Any) -> None:
    """条目管理：删除本地数据 / 强制修复 / 取消强制（破坏性或旁路类操作）。"""
    # -- 删除条目（本地数据） ---------------------------------------------
    #
    # 这是**破坏性且不可撤销**的操作，所以刻意比只读接口严格两道：
    # * `_require_writable`：`web.read_only=true` 的部署连本地数据也不该被删；
    # * `_exclusive_write`：拿到写闸门再动手——流水线此刻可能正在对同一条跑验证/修复，
    #   并发删掉它的行会让那一轮在下一次写库时报出莫名其妙的错（「行不存在」）。

    @app.delete(f"{prefix}/items/{{key:path}}")
    @_exclusive_write
    async def delete_item(request: Request, key: str, _: None = Depends(auth)) -> dict[str, Any]:
        """删除一个条目及其全部关联数据（评测 / 验证记录 / 修复记录 / 队列条目）。

        **只删本地数据**：平台上的 Issue/PR 不会被动，`data/` 下的产物文件（patch、报告、
        克隆缓存）也都保留——要一起清得手工处理。这一条在响应里明说了，免得用户以为
        「删了就干净了」。
        """
        _require_writable(_settings(request))
        repo = _repo(request)
        if repo.get_item(key) is None:
            raise HTTPException(status_code=404, detail=f"条目不存在：{key}")
        counts = repo.delete_items([key])
        return {
            "deleted": [key],
            "counts": counts,
            "note": "本地数据已删除（平台上的条目未受影响，data/ 下的产物文件也保留）。",
        }

    @app.post(f"{prefix}/items/purge")
    @_exclusive_write
    async def purge_items(
        request: Request, body: PurgeRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """批量删除条目（条目页勾选后用）。

        **不存在的 key 不报错**：批量场景里「其中几条已经被别处删掉了」是正常的，
        为此整批失败会让用户反复重试。响应里区分 ``deleted`` 与 ``missing``，
        让人能核对到底删掉了什么。
        """
        _require_writable(_settings(request))
        repo = _repo(request)
        wanted = list(dict.fromkeys(str(k) for k in body.keys))
        if not wanted:
            raise HTTPException(status_code=400, detail="keys 不能为空")
        existing = [item.key for item in repo.items_by_keys(wanted)]
        counts = repo.delete_items(existing)
        return {
            "deleted": existing,
            "missing": [k for k in wanted if k not in set(existing)],
            "counts": counts,
            "note": "本地数据已删除（平台上的条目未受影响，data/ 下的产物文件也保留）。",
        }

    @app.post(f"{prefix}/items/unforce")
    @_exclusive_write
    async def unforce_items(
        request: Request, body: ForceRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """取消强制修复：清除标记；仍在待修队列的条目回退 needs_manual。

        与 purge 同口径：不存在的 key 不报错，响应里区分 ``unforced`` 与 ``missing``。
        """
        _require_writable(_settings(request))
        repo = _repo(request)
        wanted = list(dict.fromkeys(str(k) for k in body.keys))
        if not wanted:
            raise HTTPException(status_code=400, detail="keys 不能为空")
        unforced: list[str] = []
        missing: list[str] = []
        for key in wanted:
            item = repo.unforce_item(key)
            (unforced if item is not None else missing).append(key)
        return {"unforced": unforced, "missing": missing}


def _register_api_queues(app: FastAPI, prefix: str, auth: Any) -> None:
    """队列：查看当前积压 / 手动 flush。"""
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
        # 与作业共用同一个工作体：两条路径不可能给出不一样的结果。
        return await _work_flush(
            request.app, {"queue": body.queue, "force": body.force}, _quiet_report
        )


def _register_api_actions(app: FastAPI, prefix: str, auth: Any) -> None:
    """同步动作：评测 / 验证 / 修复 / 扫描 / 审批（薄封装；点按钮走 jobs 那套）。"""
    # -- 动作 -------------------------------------------------------------
    #
    # 每个动作都只是「薄薄一层」：鉴权/只读闸门 → 调工作体（见上方 `_work_*`）。
    # 页面点按钮走的是 `/jobs/{kind}`（能看进度、不阻塞页面），这里保留同步语义给 REST
    # 消费方与既有契约用——两种入口共用同一份实现，不会分叉。

    @app.post(f"{prefix}/actions/evaluate")
    @_exclusive_write
    async def action_evaluate(
        request: Request, body: EvalRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        return await _work_evaluate(
            request.app,
            {"keys": body.keys, "deep": body.deep, "limit": body.limit},
            _quiet_report,
        )

    @app.post(f"{prefix}/actions/verify")
    @_exclusive_write
    async def action_verify(
        request: Request, body: EvalRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        return await _work_verify(
            request.app, {"keys": body.keys, "limit": body.limit}, _quiet_report
        )

    @app.post(f"{prefix}/actions/fix")
    @_exclusive_write
    async def action_fix(
        request: Request, body: FixRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        return await _work_fix(
            request.app,
            {"keys": body.keys, "dry_run": body.dry_run, "limit": body.limit},
            _quiet_report,
        )

    @app.post(f"{prefix}/actions/scan")
    @_exclusive_write
    async def action_scan(
        request: Request, body: ScanRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _require_writable(_settings(request))
        return await _work_scan(
            request.app,
            {"repo": body.repo, "full": body.full, "limit": body.limit},
            _quiet_report,
        )


def _register_api_jobs(app: FastAPI, prefix: str, auth: Any) -> None:
    """作业（长任务）：提交后立刻返回，前端轮询进度。"""
    # -- 作业（长任务）：提交后立刻返回，前端轮询进度 -------------------------

    @app.get(f"{prefix}/jobs")
    async def list_jobs(request: Request, _: None = Depends(auth)) -> dict[str, Any]:
        return request.app.state.jobs.to_dict()

    @app.get(f"{prefix}/jobs/{{job_id}}")
    async def get_job(request: Request, job_id: str, _: None = Depends(auth)) -> dict[str, Any]:
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"作业不存在：{job_id}")
        payload = job.to_dict()
        # render=1：事件附带服务端渲染好的安全 html（详情页时间线用，前端只拼接）。
        if request.query_params.get("render") == "1":
            payload["events"] = [render_job_event(ev) for ev in payload["events"]]
        return payload

    @app.post(f"{prefix}/jobs/{{kind}}", status_code=202)
    async def submit_job(
        request: Request, kind: str, body: JobRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        """提交一个长任务，立刻拿回 job id。

        **刻意不挂 `_exclusive_write`**：写闸门要由作业**自己**在它的任务里全程握着。若在这里
        握（然后请求返回就放），作业还在跑而闸门已经空了，并发的第二次写就能进来——重复调
        LLM、重复写平台标签，正是 `WRITE_GATE` 要防的事。所以这里只做「能不能写」的前置检查，
        互斥交给 `web/jobs.py::JobRegistry._run`。
        """
        settings = _settings(request)
        _require_writable(settings)
        _reject_proxied_write(request, settings)
        _reject_remote_write(request, settings)

        work = _JOB_WORK.get(kind)
        if work is None:
            allowed = "、".join(_JOB_WORK)
            raise HTTPException(
                status_code=400, detail=f"未知的任务类型：{kind!r}；可选：{allowed}"
            )
        params = body.model_dump()
        try:
            job = request.app.state.jobs.submit(kind, params, _bind_job(request.app, work, params))
        except FissueError as exc:
            # 409：已有作业在跑。这是「稍后再来」，不是服务故障。
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job": job.to_dict()}

    @app.post(f"{prefix}/jobs/{{job_id}}/cancel")
    async def cancel_job(request: Request, job_id: str, _: None = Depends(auth)) -> dict[str, Any]:
        """请求取消一个仍在跑的作业。

        **刻意不挂 `_exclusive_write`**：写闸门此刻正被那个作业自己握着，去抢它只会立刻拿到
        409，取消就永远做不成。这里只做「能不能写」的前置检查，真正的互斥仍然由作业自己维持。

        回答的是「已请求取消」而不是「已取消」：``task.cancel()`` 要等任务在下一个 ``await``
        点让出控制权才生效（作业可能正卡在一次 LLM 调用或沙盒执行上）。前端据状态轮询到
        ``canceled`` 即完成。
        """
        settings = _settings(request)
        _require_writable(settings)
        _reject_proxied_write(request, settings)
        _reject_remote_write(request, settings)

        registry = request.app.state.jobs
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"作业不存在：{job_id}")
        if not job.live:
            raise HTTPException(
                status_code=409, detail=f"任务已经结束（{job.status}），没有可取消的东西"
            )
        if job.kind == "round":
            # 轮次任务没有自己的 task 可取消（它的 task 在常驻服务的循环里），
            # 走「停止当前这一轮」的入口——与任务页的停止按钮同一逻辑。
            from ..service.control import ROUNDS

            stopped = ROUNDS.request_stop()
            if stopped is None:
                raise HTTPException(status_code=409, detail="本进程没有正在跑的轮次，无需停止")
            job.canceled_by = "user"
            return {"ok": True, "message": f"已请求停止轮次：{stopped.name}"}
        registry.cancel(job)
        return {
            "job": job.to_dict(),
            "note": "已请求取消；任务会在下一个可中断点退出（可能还要几秒），请稍候刷新。",
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


def _register_api_webhooks(app: FastAPI, prefix: str, auth: Any) -> None:
    """入站 webhook：平台推事件时后台跑一轮（边界 = 签名 + 仓库白名单）。"""
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
        settings = _settings(request)
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


def _register_api_misc(app: FastAPI, prefix: str, auth: Any) -> None:
    """导出 / 停止当前轮次 / 健康检查。"""
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

    @app.post(f"{prefix}/round/stop")
    async def stop_round(request: Request, _: None = Depends(auth)) -> dict[str, Any]:
        """停止**当前这一轮**（常驻服务的定时轮次）。

        为什么**不能**套 `_exclusive_write`：那一层会先去抢写闸门——而轮次正握着它，
        等于「想停它就得先等它结束」，正是这次要解决的死结。所以这里只要求身份
        （``Depends(auth)``，非回环来源仍需会话/Key），**完全不碰**写闸门。

        停不掉的情形如实回答（409）：本进程没有在跑的轮次——可能刚结束，也可能跑在另一个
        进程里（跨进程那份 task 在别人的内存里，谁都停不了它）。
        """
        from ..service.control import ROUNDS

        stopped = ROUNDS.request_stop()
        if stopped is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "本进程当前没有在跑的轮次：它可能已经结束，或跑在另一个进程里"
                    "（那种情况下谁都停不了它，只能去那个进程停服务）。"
                ),
            )
        log.warning("已请求停止当前轮次：%s（已跑 %.0f 秒）", stopped.name, stopped.seconds)
        return {
            "ok": True,
            "stopped": stopped.name,
            "seconds": stopped.seconds,
            "note": (
                "已请求停止：它会在最近的等待点退出。正在进行的单条操作"
                "（例如正在建 PR）可能只完成一半。"
            ),
        }

    @app.get(f"{prefix}/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __import__("fissue").__version__}