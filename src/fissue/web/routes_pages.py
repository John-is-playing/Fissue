"""Web 仪表盘页面路由注册（HTML 视图）。

从 :mod:`fissue.web.app` 拆出来：那里原本同时装着应用工厂、REST API、写闸门与全部
页面视图。本模块只负责**把页面挂到应用上**；页面上下文、聚合缓存等共享工具仍在
app.py（它们同时被 REST API 与后台缓存使用）。

导入方向是单向的：本模块 ``from .app import ...``，而 app 只在 ``create_app`` 内部
惰性导入本模块，因此不存在循环导入。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..models import Category, ItemStatus, ItemType, Platform, Priority
from .app import (
    _aggregates_of,
    _enum_param,
    _page_context,
    _queue_block,
    _repo,
    _settings,
)
from .markdown import render_job_event
from .status import collect as collect_status
from .status import queue_panel, writer_panel


def register_web(app: FastAPI, settings: Any, templates: Jinja2Templates) -> None:
    """注册 Web 页面路由。

    按域拆开注册（原先是一个 333 行的单函数）：总览 / 条目 / 状态与队列 / 任务 /
    配置页 / 条目详情。切分只按域移动代码，**注册顺序保持原样**。
    """
    _register_web_pipeline(app, templates)
    _register_web_overview(app, templates)
    _register_web_items(app, templates)
    _register_web_status(app, templates)
    _register_web_jobs(app, templates)
    _register_web_config(app, templates)
    _register_web_item_detail(app, templates)


def _register_web_pipeline(app: FastAPI, templates: Jinja2Templates) -> None:
    """流水线主页：漏斗 / 看板 / 泳道三视图，共用同一份计数。

    这三张视图回答的是同一个问题的三个侧面——「现在跑到哪、卡在哪」：
    漏斗看**全量吞吐**、看板看**各阶段积压与批量操作**、泳道看**单条进度**。
    计数一律来自 :meth:`Repository.pipeline_summary` 的一条 ``GROUP BY``，不在前端各算各的。
    """

    @app.get("/pipeline", response_class=HTMLResponse)
    async def pipeline_page(
        request: Request,
        repo_slug: str | None = Query(None, description="只看某个仓库"),
    ) -> HTMLResponse:
        from ..models import ItemStatus as _ItemStatus
        from . import pipeline as view

        r = _repo(request)
        model = view.summarize(r.pipeline_summary(repo_slug=repo_slug))

        # 泳道行：直接取最近的一批条目，由视图模型负责「卡住的排最前」。
        rows = view.swimlane_rows(r.list_items(repo_slug=repo_slug, limit=120), limit=60)
        # 卡点清单单独再取：泳道只显示前 60 条，而「有哪些卡住了」不该被截断。
        stuck_items = r.list_items(
            repo_slug=repo_slug, status=_ItemStatus.NEEDS_MANUAL, limit=50
        ) + r.list_items(repo_slug=repo_slug, status=_ItemStatus.FAILED, limit=50)
        stuck = sorted(
            (view.item_row(item) for item in stuck_items),
            key=lambda row: row["updated_at"] or "",
            reverse=True,
        )

        return templates.TemplateResponse(
            request,
            "pipeline.html",
            _page_context(
                request,
                nav="pipeline",
                view=model,
                rows=rows,
                stuck=stuck,
                stages=list(view.STAGES),
                repo_filter=repo_slug or "",
                **_queue_block(request),
            ),
        )


def _register_web_overview(app: FastAPI, templates: Jinja2Templates) -> None:
    """总览：告警 + 统计 + 队列 + 用量 + 控制面板 + 任务与抓取记录。"""

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        """总览：告警 + 统计 + 队列 + 用量 + 控制面板 + 任务与抓取记录。

        与「条目」页分开，是因为两者关心的事不同：总览回答「有没有问题、在不在跑、花了多少钱」，
        条目页回答「这一条是什么」。混在一页时，光筛选器就把告警挤到看不见的地方。
        """
        r = _repo(request)
        st = _settings(request)
        snapshot = collect_status(request.app, st, r, aggregates=_aggregates_of(request.app))
        return templates.TemplateResponse(
            request,
            "overview.html",
            _page_context(
                request,
                nav="overview",
                total=snapshot["database"]["items"],
                summary=snapshot["summary"],
                queues=snapshot["queues"],
                usage=snapshot["llm"]["today"],
                budget=st.llm.budget,
                jobs=snapshot["jobs"],
                scans=snapshot["scans"],
                alerts=snapshot["alerts"],
                problems=[],
            ),
        )


def _register_web_items(app: FastAPI, templates: Jinja2Templates) -> None:
    """条目列表页：筛选 + 排序 + 分页。"""

    @app.get("/items", response_class=HTMLResponse)
    async def items_page(
        request: Request,
        repo_slug: str | None = Query(None),
        status: str | None = Query(None),
        category: str | None = Query(None),
        priority: str | None = Query(None),
        item_type: str | None = Query(None),
        q: str | None = Query(None),
        stage: str | None = Query(None, description="流水线阶段（evaluate/verify/judge/fix/pr/stuck/done）"),
        lane: str | None = Query(None, description="流水线（issue-bug/issue-feature/pr-bug/pr-feature）"),
        preset: str | None = Query(None, description="预设（卡住的/待评测/待验证/待裁决/待修/已提PR/已出结论）"),
        sort: str = Query("updated_at"),
        order: str = Query("desc"),
        limit: int = Query(50, ge=1, le=500),
        page: int = Query(1, ge=1),
    ) -> HTMLResponse:
        from ..store import export as exp
        from ..store.repository import ITEM_SORT_COLUMNS
        from . import pipeline as pipe

        r = _repo(request)
        st = _settings(request)

        # 非法筛选值不该让浏览器吃到一坨 JSON：``_enum_param`` 抛的是给 API 用的 400，
        # 而 HTML 路由上用户只会看到 ``{"detail": "status 取值非法…"}``。这里把它收成一条
        # 页面内的提示，页面照常渲染（同时仍然告诉用户可选值有哪些）。
        problems: list[str] = []

        def pick(value: str | None, enum_cls: Any, name: str) -> Any:
            try:
                return _enum_param(value, enum_cls, name=name)
            except HTTPException as exc:
                problems.append(str(exc.detail))
                return None

        f_type = pick(item_type, ItemType, "item_type")
        f_status = pick(status, ItemStatus, "status")
        f_category = pick(category, Category, "category")
        f_priority = pick(priority, Priority, "priority")

        # stage / lane 是**流水线视图**的筛选（看板格子点进来带的）。
        # 阶段 → 状态集合的映射由 web/pipeline.py 提供，与看板计数同一份口径。
        #
        # preset 是 stage 的**用户友好别名**（「卡住的」「待修」）。它不另定义一套状态
        # 集合，只是换个说法——两套筛选并存却各自算一遍，是筛选器最常见的错法。
        #
        # 优先级：显式给的 stage 胜过 preset（它更精确）；两者都没给就是不过滤。
        f_preset = preset or ""
        f_stage = stage or ""
        if f_preset and not f_stage:
            resolved = pipe.preset_stage(f_preset)
            if resolved is None:
                problems.append(f"preset 取值非法：{f_preset!r}")
            else:
                f_stage = resolved

        stage_statuses: list[ItemStatus] | None = None
        if f_stage:
            known = {key for key, _ in pipe.STAGES} | {pipe.DONE, pipe.STUCK}
            if f_stage not in known:
                problems.append(f"stage 取值非法：{f_stage!r}")
            else:
                stage_statuses = pipe.statuses_of_stage(f_stage)

        f_lane = lane or ""
        lane_item_type: ItemType | None = None
        lane_category: Category | None = None
        if f_lane:
            if f_lane == "unknown":
                # 「未分类」不是一条真实流水线，而是「还没评出分类」——两类都算。
                f_category = Category.UNKNOWN
            else:
                parts = f_lane.split("-", 1)
                if len(parts) != 2 or parts[0] not in ("issue", "pr") or parts[1] not in (
                    "bug",
                    "feature",
                ):
                    problems.append(f"lane 取值非法：{f_lane!r}")
                else:
                    lane_item_type = ItemType.ISSUE if parts[0] == "issue" else ItemType.PR
                    lane_category = Category.BUG if parts[1] == "bug" else Category.FEATURE
                    # lane 已经指定了类型与分类，原来的同名字段就以 lane 为准。
                    f_type = lane_item_type
                    f_category = lane_category

        sort_key = sort if sort in ITEM_SORT_COLUMNS else "updated_at"
        if sort != sort_key:
            problems.append(
                f"sort 取值非法：{sort!r}；可选：{'、'.join(ITEM_SORT_COLUMNS)}"
            )
        # 排序键非法时**不改顺序以外的任何东西**：明细与 total 仍用同一个口径，
        # 页脚照样说得清「共几条」。
        order_desc = order.lower() != "asc"

        offset = (page - 1) * limit
        # 两个 dict 是有意的：仓储要**枚举**，URL 要**字符串**。
        # 直接把枚举喂给 urlencode 会得到 `item_type=ItemType.ISSUE`（`str(ItemType.ISSUE)`
        # 走的是 Enum 的 __str__，不是它的值），回传给服务端就成了非法取值 → 400。
        filters = {
            "repo_slug": repo_slug,
            "item_type": f_type,
            "status": f_status,
            "category": f_category,
            "priority": f_priority,
            "query": q,
        }
        # 回填表单与拼链接用原样的字符串（非法值也照原样回填，用户能看到自己填了什么）。
        shown = {
            "repo_slug": repo_slug or "",
            "item_type": item_type or "",
            "status": status or "",
            "category": category or "",
            "priority": priority or "",
            "q": q or "",
            "stage": f_stage,
            "lane": f_lane,
            "preset": f_preset,
            "sort": sort_key,
            "order": "desc" if order_desc else "asc",
            "limit": limit,
        }
        # 表头排序链接的底串：筛选 + 每页条数（sort/order 由链接自己追加）；
        # 翻页链接的底串另外带上 sort/order——否则一点「下一页」，排序就丢了。
        base_qs = urlencode({k: v for k, v in shown.items() if v and k not in ("sort", "order")})
        page_qs = urlencode({k: v for k, v in shown.items() if v})

        # stage 是**状态集合**筛选：它与 status 互斥，statuses 更具体，所以以它为准。
        # 概览卡片也用同一份筛选，否则「页脚说共 12 条、卡片说 30 条」——那种对不上
        # 正是 summarize 与明细历史上反复出的老毛病。
        query_filters: dict[str, Any] = dict(filters)
        if stage_statuses is not None:
            query_filters["statuses"] = stage_statuses
        items = r.list_items(**query_filters, limit=limit, offset=offset,
                             order_by=sort_key, desc=order_desc)
        total = r.count_items(**query_filters)
        evals = r.evaluations_for([i.key for i in items])
        counts = r.comment_counts([i.key for i in items])
        rows = [
            exp.item_to_dict(i, evals.get(i.key), extra={"comments": counts.get(i.key, 0)})
            for i in items
        ]
        pages = max(1, (total + limit - 1) // limit)
        # 背景数字（队列面板 / 今日用量）不随筛选而变 → 读后台刷新缓存。
        agg = _aggregates_of(request.app)
        queues = (agg or {}).get("queues")
        if queues is None:
            queues = queue_panel(r, st)
        usage = (agg or {}).get("usage")
        if usage is None:
            usage = r.usage_today()
        # 概览卡片：无筛选且缓存里有全局概览时直接复用（同一口径）；
        # 有筛选时必须现算——卡片必须跟着筛选动（见 export.summarize 的口径约束）。
        if any(filters.values()) or (summary := (agg or {}).get("summary")) is None:
            summary = exp.summarize(r, **filters)
        return templates.TemplateResponse(
            request,
            "items.html",
            _page_context(
                request,
                nav="items",
                rows=rows,
                total=total,
                base_qs=base_qs,
                page_qs=page_qs,
                summary=summary,
                queues=queues,
                usage=usage,
                jobs=request.app.state.jobs.to_dict(),
                problems=problems,
                page=page,
                pages=pages,
                sort=sort_key,
                order_desc=order_desc,
                sort_keys=list(ITEM_SORT_COLUMNS),
                statuses=[s.value for s in ItemStatus],
                categories=[c.value for c in Category],
                priorities=[p.value for p in Priority],
                types=[t.value for t in ItemType],
                stage_options=[
                    (key, label) for key, label in pipe.STAGES
                ] + [
                    (pipe.DONE, pipe.stage_label(pipe.DONE)),
                    (pipe.STUCK, pipe.stage_label(pipe.STUCK)),
                ],
                lane_options=list(pipe.LANES),
                preset_options=list(pipe.PRESETS),
                filters=shown,
            ),
        )


def _register_web_status(app: FastAPI, templates: Jinja2Templates) -> None:
    """状态监测页 + 队列页。"""

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(
        request: Request,
        sandbox: bool = Query(False, description="首屏是否带一次沙盒探测"),
    ) -> HTMLResponse:
        """状态监测：实时（2 秒轮询）刷新写闸门、队列、任务、抓取与告警。

        首屏那份快照**由服务端塞进页面**（`window.FISSUE_STATUS`），之后由同一个前端渲染函数
        接管轮询——渲染逻辑只有一份（在 status.html 的脚本里），不会出现「服务端渲染的样子和
        轮询后的样子不一样」。
        """
        snapshot = collect_status(
            request.app,
            _settings(request),
            _repo(request),
            with_sandbox=sandbox,
            aggregates=_aggregates_of(request.app),
        )
        return templates.TemplateResponse(
            request,
            "status.html",
            _page_context(
                request,
                nav="status",
                snapshot=snapshot,
                # 任务面板（原先独立的 /jobs 页）也在这里渲染：两者回答同一个问题
                # ——「服务现在在干什么、有没有卡住」。`writer` 用来解释「写操作进行中
                # 但表里没有对应的作业」这一类情况（常驻轮次不是网页作业）。
                jobs=request.app.state.jobs.to_dict(),
                writer=writer_panel(_settings(request)),
            ),
        )

    @app.get("/queues", response_class=HTMLResponse)
    async def queues_page(request: Request) -> Response:
        """旧入口：队列判定已并进流水线的看板视图。

        保留这一条**重定向**而不是直接删掉：`/queues` 出现在文档、书签和别人的脚本里，
        404 掉会让人以为功能没了。真正的内容现在在 `/pipeline` 的看板里（同一份部件）。
        """
        return RedirectResponse("/pipeline", status_code=307)


def _register_web_jobs(app: FastAPI, templates: Jinja2Templates) -> None:
    """任务列表页 + 任务详情页。"""

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(
        request: Request,
        kind: str | None = Query(None, description="按作业种类筛"),
        status: str | None = Query(None, description="按状态筛"),
        page: int = Query(1, ge=1),
        per_page: int = Query(20, ge=1, le=200),
    ) -> HTMLResponse:
        """任务列表：筛选 + 分页。

        为什么单独一页而不是塞在状态页里：作业表是进程内的，常驻服务每跑一轮就登记一条，
        一天下来几百条。全堆在状态页上时，「找上周那次失败的扫描」只能靠翻——而状态页
        要回答的是「**现在**在干什么」，不是「历史上都干过什么」。
        """
        from .paging import build_pager

        registry = request.app.state.jobs
        data = registry.page(page=page, per_page=per_page, kind=kind, status=status)
        pager = build_pager(
            page=data["page"],
            pages=data["pages"],
            total=data["total"],
            per_page=data["per_page"],
            base_path="/jobs",
            # 翻页要带着筛选条件，否则点「下一页」筛选就没了——用户以为被重置。
            params={"kind": kind, "status": status,
                    "per_page": per_page if per_page != 20 else None},
        )
        return templates.TemplateResponse(
            request,
            "jobs.html",
            _page_context(
                request,
                nav="jobs",
                jobs=data,
                pager=pager,
                current=registry.current().to_dict() if registry.current() else None,
                writer=writer_panel(_settings(request)),
                filters={"kind": kind or "", "status": status or "", "per_page": per_page},
            ),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail_page(request: Request, job_id: str) -> HTMLResponse:
        """任务详情：结构化事件流（LLM 流式输出 + 工具动作），运行中自动刷新。

        鉴权与同区其他页面一致，走 ``page_guard_middleware``（页面统一入口），
        不挂在 REST API 的 ``auth`` 依赖上——那是 ``_register_api`` 作用域内的局部变量。
        """
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"作业不存在或已被清理：{job_id}")
        return templates.TemplateResponse(
            request,
            "jobs_detail.html",
            _page_context(
                request,
                nav="jobs",
                job=job.to_dict(),
                events_html="".join(render_job_event(ev)["html"] for ev in job.events),
            ),
        )


def _register_web_config(app: FastAPI, templates: Jinja2Templates) -> None:
    """监控仓库页 / 设置页 / 初始化向导页。"""

    @app.get("/repos", response_class=HTMLResponse)
    async def repos_page(request: Request) -> HTMLResponse:
        """监控仓库：增删启停与各项参数，写入覆盖层（不动 config.yaml）。"""
        from .setup import repos_view

        st = _settings(request)
        return templates.TemplateResponse(
            request,
            "repos.html",
            _page_context(
                request,
                nav="repos",
                view=repos_view(request, st),
                platforms=[p.value for p in Platform],
                suggested_test_hint="python -m pytest -q",
            ),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        """设置：LLM / 数据库 / 平台 token / Web 绑定与访问口令，并标注每个值的来源。"""
        from .setup import settings_view

        return templates.TemplateResponse(
            request,
            "settings.html",
            _page_context(request, nav="settings", view=settings_view(request, _settings(request))),
        )

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> HTMLResponse:
        """初始化向导：分步配好 LLM / 数据库 / 平台凭据 / 监控仓库，最后可立刻抓第一轮。

        页面本身不写任何东西——每一步的「测试」与最后的「完成」都走
        ``/api/v1/setup/*`` 那几个端点，它们统一过 ``setup_guard`` 与白名单。
        """
        from .setup import settings_view

        return templates.TemplateResponse(
            request,
            "setup.html",
            _page_context(
                request,
                nav="setup",
                view=settings_view(request, _settings(request)),
                platforms=[p.value for p in Platform],
                suggested_test_hint="python -m pytest -q",
                setup_done=bool(_settings(request).repos) and bool(_settings(request).llm.api_key),
            ),
        )


def _register_web_item_detail(app: FastAPI, templates: Jinja2Templates) -> None:
    """条目详情页。"""

    @app.get("/item/{key:path}", response_class=HTMLResponse)
    async def item_detail(request: Request, key: str) -> HTMLResponse:
        from ..store import export as exp

        r = _repo(request)
        item = r.get_item(key)
        if item is None:
            raise HTTPException(status_code=404, detail=f"条目不存在：{key}")
        evaluation = r.latest_evaluation(key)
        runs = r.verifier_runs(key, limit=20)
        verifier = r.latest_verifier(key)

        from . import pipeline as pipe

        # 阶段进度由**同一个**纯函数算（与流水线主页共用口径）：
        # 详情页自己再推一遍「走到哪了」，迟早会与主页对不上。
        stages = [
            {"key": stage_key, "label": stage_label, "state": state}
            for (stage_key, stage_label), state in zip(
                pipe.STAGES, pipe.stage_states(item.status), strict=True
            )
        ]

        return templates.TemplateResponse(
            request,
            "item.html",
            _page_context(
                request,
                # `get_item` 会连带把评论读出来（selectinload），所以这里的
                # `comment_thread` 有内容——详情页因此能看到 Issue 正文与讨论。
                data=exp.item_to_dict(item, evaluation, batch=r.latest_batch_conclusion(key)),
                item=item,
                stages=stages,
                evaluation=evaluation,
                conclusion=r.latest_batch_conclusion(key),
                verifier=verifier[1] if verifier else None,
                runs=runs,
                regression=exp.regression_summary(
                    runs, mode=_settings(request).verifier.regression_gate
                ),
                fixes=r.fix_attempts(key),
            ),
        )