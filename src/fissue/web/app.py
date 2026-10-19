"""FastAPI 应用入口（Web 仪表盘 + REST API）。

形态（Q14 选 B：只读 + 操作）
----------------------------
* **只读**：列表、筛选、详情、评分理由、统计、导出。
* **操作**：手动重评、flush 队列、批准提 PR、批准合并、触发扫描。
* 仓库与密钥管理界面（Q14:C）**尚未实现**。原先这里写着「默认关闭，需显式打开
  ``web.allow_repo_management``」，但那个开关背后没有任何路由——属于「配置写了却
  不生效」，已随 P2 清理一并删除（要做得另开一个特性，而不是留个空开关）。

鉴权（docs/AUTH.md）
--------------------
三种来源互为「或」：**回环**、**可信网段**（``web.trusted_networks``）免登录；
其余来源必须持有会话 cookie 或 ``Authorization: Bearer <API Key>``。
判定与路由都在 :mod:`fissue.web.auth`，本模块只负责把它接到应用上，
并把「写操作的两道补刀」更新成按身份判定。
"""

from __future__ import annotations

import functools
import ipaddress
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .. import __version__ as FISSUE_VERSION
from ..config import Settings, load_settings
from ..errors import FissueError
from ..lockfile import WRITE_GATE, single_flight
from ..logging_setup import get_logger, setup_logging
from ..models import Category, ItemStatus, ItemType, Platform, Priority, QueueName
from .auth import AUTH_PREFIX, page_guard_middleware
from .auth import register as register_auth
from .hooks import parse_event, signature_ok
from .jobs import Job, JobRegistry
from .status import collect as collect_status
from .status import queue_panel

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class FlushRequest(BaseModel):
    queue: str | None = Field(default=None, description="verify / fix_bug / fix_feature，留空表示全部")
    force: bool = True


class JobRequest(BaseModel):
    """长任务（作业）的参数。

    各 kind 只读自己关心的字段，其余留默认即可——这样控制面板可以用一个模型提交所有动作，
    不必为每个 kind 再定义一个模型（也会让「加一种新动作」变成改两处）。
    """

    keys: list[str] = Field(default_factory=list, description="只处理这些条目；留空表示按 kind 自行挑选")
    repo: str | None = Field(default=None, description="scan 用：只扫这个仓库")
    queue: str | None = Field(default=None, description="flush 用：verify / fix_bug / fix_feature")
    limit: int | None = Field(default=None, ge=1, le=500)
    deep: bool = Field(default=False, description="evaluate 用：深度重评")
    full: bool = Field(default=False, description="scan 用：忽略游标全量抓")
    force: bool = Field(default=True, description="flush 用：忽略阈值强制刷")
    dry_run: bool = Field(default=True, description="fix 用：只产 patch 不提 PR")


class EvalRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, description="要重评的条目 key；留空则取未评测条目")
    deep: bool = False
    # `ge=1`：`limit` 是「做多少条」，0 在语义上等于「一条都不做」。原先它是裸 int，
    # 而处理函数写的是 `int(limit or 20)`——显式传 0 会被 `or` 当成「没传」而**反而**去评 20 条。
    limit: int = Field(default=20, ge=1)


class FixRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    dry_run: bool = True
    # 与 EvalRequest 同理：0 会被 `or 5` 当成没传，于是「我不想修」变成「去修 5 条并提 PR」。
    limit: int = Field(default=5, ge=1)


class ScanRequest(BaseModel):
    repo: str | None = None
    full: bool = False
    limit: int | None = None


class ApproveRequest(BaseModel):
    note: str = ""


class PurgeRequest(BaseModel):
    """批量删除条目（本地数据，不可撤销）。"""

    keys: list[str] = Field(default_factory=list)


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动时确保表存在，并持有数据库连接
        from ..store.db import build_database
        from ..store.repository import Repository
        from .auth import AuthStore, migrate_legacy_token, warn_if_unprovisioned

        database = build_database(st)
        database.create_all()
        app.state.settings = st
        app.state.db = database
        app.state.repo = Repository(database)
        # 鉴权是**启动期**就要就位的：旧口令一次性迁移（docs/AUTH.md §7），
        # 以及「还没有管理员账号」时生成引导口令并打印（§3.4）。
        # 两件都放在这里而不是请求期：它们每次进程启动只该发生一次。
        store = AuthStore(database)
        migrate_legacy_token(st, store)
        warn_if_unprovisioned(st, store)
        log.info("Web/API 已就绪（只读=%s，API 前缀=%s）", st.web.read_only, st.api.prefix)
        yield
        # 先把在跑的作业收掉，再关日志：作业里可能正握着 flock 或占着沙盒目录，
        # 直接退出只会留下「Task was destroyed but it is pending」与半截的临时目录。
        await app.state.jobs.shutdown()
        log.info("Web/API 关闭")

    application = FastAPI(
        title="Fissue",
        description="Issue/PR 的 AI 评测、沙盒验证与自动修复",
        version=FISSUE_VERSION,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    application.state.templates = templates
    # 进程启动时刻：状态页显示「已运行多久」。放在这里而不是 lifespan 里，是因为
    # `_ensure_state` 那条兜底路径（测试用 TestClient 不做上下文管理器）不会跑 lifespan，
    # 状态接口就会拿到 None——显示成「已运行 —」而不是崩掉。
    application.state.started_at = datetime.now(timezone.utc)
    # 作业表：进程内一张，理由与限制见 web/jobs.py 的模块说明。
    application.state.jobs = JobRegistry(st)
    # 静态资源：样式与脚本从两个页面里抽出来放这里（原先各内联一份，改一处漏一处）。
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # 页面守卫放最外层（中间件先于路由）：未认证的页面加载直接 303 到 /login，
    # 而不是渲染一个处处报错的看板。`/api/*` 不受它影响（必须能返回结构化的 401/403，
    # 前端要靠状态码决定行为），`/static/*` 也必须放行——登录页的样式就在这里。
    @application.middleware("http")
    async def _guard_pages(request: Request, call_next: Any) -> Any:
        return await page_guard_middleware(request, call_next)

    _register_api(application, st)
    # 配置写入类端点（仓库管理 / 设置 / 初始化向导）住在 web/setup.py：
    # 它们与 /actions/* 是**两类不同的写**（后者写外部平台，前者写本机配置），
    # 所以边界也分开（见 setup.py 的 setup_guard）。鉴权依赖两者共用一份。
    from .setup import register as register_setup

    register_setup(application, st, auth=_auth_dependency())
    # 会话/API Key 的端点与 /login 页：挂在固定的 `/api/auth` 下，**不随 api.prefix**，
    # 也不受 api.enabled 影响——登录不该因为「业务 API 关了」而一起消失。
    register_auth(application, st)
    _register_web(application, st, templates)
    return application


# ---------------------------------------------------------------------------
# 鉴权与工具
# ---------------------------------------------------------------------------


def _auth_dependency():
    """鉴权依赖。

    实现整体搬到 :mod:`fissue.web.auth`（它同时服务 ``/api/v1/*`` 与配置写入端点），
    这里只保留这个入口名——`_register_api` 与 `register_setup` 都在用它，
    而「鉴权只有一处实现」这个约束比少一个转发函数重要。

    依赖内部**每次请求现取 settings 与数据库**，不缓存：设置页改完会热重载
    （web/setup.py::reload_state），闭包里那份是启动时的旧对象。
    """
    from .auth import require_auth_dependency

    return require_auth_dependency()


def _require_writable(settings: Settings) -> None:
    if settings.web.read_only:
        raise HTTPException(status_code=403, detail="当前为只读模式（web.read_only=true）")


#: 反向代理常见的转发头。见到它们就拒绝**未认证**的写请求——见 :func:`_reject_proxied_write`。
#: 判据的**唯一定义**在 setup.py（`FORWARD_HEADERS`）：两处各留一份迟早会分叉
#: （原先这里多一个 `forwarded`，于是同一件事在两处得到相反结论）。


def _reject_proxied_write(request: Request, settings: Settings) -> None:
    """拒绝「经过反向代理且未认证」的写请求。

    ``Settings._validate`` 只能看**绑定地址**，因此 ``web.host=127.0.0.1`` + 同机 nginx
    反代到本端口时，写操作控制面（去平台打标签、把条目推进修复队列）在 HTTP 层与
    「本机用户直接访问」无法区分。见到转发头就拒，把常见配置（nginx 的
    ``proxy_set_header``）从「静默开放」变成「响亮拒绝」。

    必须说清楚边界：**这是补刀，不是安全边界**。边界是**身份**——同机代理若一个转发头
    都不设，只有会话/API Key 能真正兜住。判据本身在 :func:`web.auth.forwarded_without_declaration`：
    已认证、或已声明可信网段时放行（反代正是「对外入口」的常态，要求它不能带转发头是荒谬的），
    其余情况见到转发头就拒。
    """
    from .auth import forwarded_without_declaration, resolve_identity

    identity = resolve_identity(request)
    hit = forwarded_without_declaration(request, settings, identity)
    if hit:
        raise HTTPException(
            status_code=403,
            detail=(
                f"拒绝写操作：请求带有反向代理转发头（{'、'.join(hit)}），"
                "但既没有通过认证，也没有声明可信网段。"
                "请在 /login 登录（或用 API Key），"
                "若这个控制面确实挂在反向代理后面，请把代理/内网网段写进 web.trusted_networks。"
            ),
        )


def _reject_remote_write(request: Request, settings: Settings) -> None:
    """**来源不是回环/可信网段且未认证**的写请求一律拒绝。

    为什么还需要这一道：「对外可达却没身份机制」本来在启动时就会被拒（见 ``Settings._validate``），
    但那个检查读的是 ``web.host``——**把 ``create_app()`` 嵌进别人的服务器**（或换个
    绑定地址、或经 `docker -p` 之类暴露出去）时，配置里写的仍是 `127.0.0.1`，检查就看不见了。
    这一道直接看**对端地址**，把那种情况兜住。

    拿不到可解析的对端地址时（测试客户端 ``testclient``、Unix socket 等）不在这里判——
    「不知道」不等于「可疑」，不能因此把本机正常用法也拦掉。
    """
    from .auth import resolve_identity

    identity = resolve_identity(request)
    if identity.allowed:
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
                f"拒绝写操作：请求来自 {host}，既不是本机回环/可信网段，也没有通过认证。"
                "请在该机器上访问 /login 登录（或带上 API Key）；"
                "若这台机器只有可信网络可达，可把网段写进 web.trusted_networks。"
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
            async with _write_guard(settings, action="Web/API 写操作"):
                return await fn(*args, **kwargs)
        except FissueError as exc:
            # 409 而不是 500：这是「此刻有别处在写，你稍后再来」，不是服务故障
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return wrapper


@asynccontextmanager
async def _write_guard(settings: Settings, *, action: str) -> AsyncIterator[None]:
    """写操作的互斥：进程内闸门 + 跨进程 flock。

    抽成独立上下文管理器，是因为**后台作业也要用它**：作业由请求提交，但请求返回后锁不能
    跟着放（否则作业还在跑，另一次写就能同时进来）。作业在自己的任务里进出这两道闸门，
    见 ``web/jobs.py``。
    """
    async with WRITE_GATE.hold_or_fail(action=action):
        with single_flight(settings, action=action):
            yield


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


def _settings_of(app: FastAPI) -> Settings:
    """`app` 版本的取配置（后台作业手里只有 app，没有那个已经结束的 Request）。"""
    state = app.state
    if getattr(state, "settings", None) is None:  # pragma: no cover - lifespan 已设好
        state.settings = load_settings()
    return state.settings


def _ctx_of(app: FastAPI):
    """按 app 构造 RuntimeContext（复用 app.state 里的 db）。"""
    from ..pipeline.context import RuntimeContext

    state = app.state
    settings: Settings = _settings_of(app)
    ctx = RuntimeContext.create(settings)
    # 复用已有引擎，避免重复连接池
    ctx.db = state.db
    ctx.repo = state.repo
    ctx.evaluator.repo = ctx.repo
    ctx.verifier.repo = ctx.repo
    ctx.generator  # noqa: B018  —— 触发构造
    return ctx


def _ctx(request: Request):
    """为写操作临时构造 RuntimeContext（复用 app.state 里的 db）。"""
    _ensure_state(request)
    return _ctx_of(request.app)


def _page_context(request: Request, *, nav: str = "", **extra: Any) -> dict[str, Any]:
    """渲染页面的公共上下文。

    ``api_prefix`` / ``read_only`` 这类值一律在 Python 侧定好再交给模板，模板里只做
    ``| tojson``。原先 item.html 写的是
    ``{{ settings.api.prefix if settings else '/api/v1' | tojson }}``——Jinja 的 ``|`` 只作用于
    else 分支，所以一旦有人把 settings 传进上下文，渲染出的就是裸字符串 ``/api/v1``，
    进 JS 变成**未闭合的正则字面量**、整块 script 语法错误、页面所有按钮报
    ``act is not defined``。当时只是碰巧没传 settings 才没炸。现在没有这个分支了。

    ``nav`` **必须给默认值**：base.html 里是 ``{{ nav | tojson }}``，而 Jinja 的 ``tojson``
    序列化一个 ``Undefined`` 会直接抛 ``TypeError``——某个路由忘了传 nav，整个页面就 500。
    默认空串 = 「没有高亮项」，比崩了强。

    ``csrf_token`` / ``identity`` 是 cookie 化鉴权之后新增的：前端要对所有非 GET 请求带上
    ``X-CSRF-Token``，而它对每个会话都不同（见 docs/AUTH.md §3.5）。**在 Python 侧算好再交给
    模板**，模板里只做 ``| tojson``——理由同上（``|`` 的优先级坑踩过一次）。
    """
    from .auth import resolve_identity

    settings = _settings(request)
    identity = resolve_identity(request)
    context: dict[str, Any] = {
        "settings": settings,
        "nav": nav,
        "api_prefix": settings.api.prefix,
        "read_only": settings.web.read_only,
        "repos": [r.slug for r in settings.enabled_repos()],
        "version": FISSUE_VERSION,
        "static_url": request.url_for("static", path="app.css"),
        "script_url": request.url_for("static", path="app.js"),
        "identity": identity,
        "csrf_token": identity.csrf_token,
        "auth_prefix": AUTH_PREFIX,
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# 长任务的实现体（``/actions/*`` 与 ``/jobs/*`` 共用同一份）
# ---------------------------------------------------------------------------
#
# 为什么要抽出来：这些操作既要从同步端点走（REST 消费方、老接口），也要从后台作业走
# （页面点按钮、看进度）。原先实现只写在端点函数体里，作业化时就只能复制一遍——两份实现
# 迟早会分叉（改了同步那份、忘了作业那份）。
#
# 约定：工作体在「这次请求本身不合法」时抛 ``HTTPException``（与同步端点的既有错误码一致：
# 400 / 403 / 404），作业包装层负责把它转成一条可读的失败原因（见 :func:`_bind_job`）。

#: 进度回调：工作体报告「现在在做什么」，作业把它落进 job.log，前端轮询就能看到。
Report = Callable[[str], None]


def _quiet_report(message: str) -> None:
    """同步端点没有进度界面可展示，退化成一条 debug 日志。"""
    log.debug("写操作进度：%s", message)


def _limit_param(params: dict[str, Any], default: int) -> int:
    """取「这一轮做多少条」。

    **不能写成 `params.get("limit") or default`**：显式传 0 会被 `or` 当成「没传」，于是
    「我一条都不想动」反而变成「去做 20 条」（对 fix 来说就是真去改代码并提 PR）。
    0 的语义是「一条都不做」，必须原样保留。
    """
    raw = params.get("limit")
    if raw is None:
        return default
    return max(0, int(raw))


async def _work_scan(app: FastAPI, params: dict[str, Any], report: Report) -> dict[str, Any]:
    from ..pipeline.fetcher import Fetcher
    from ..platforms.registry import resolve_repos

    settings = _settings_of(app)
    ctx = _ctx_of(app)
    repo = params.get("repo")
    repos = resolve_repos(settings, [repo] if repo else None)
    if not repos:
        raise HTTPException(status_code=400, detail="没有可扫描的仓库")
    full = bool(params.get("full"))
    report(f"抓取 {len(repos)} 个仓库（{'全量' if full else '增量'}）")
    stats = await Fetcher(ctx).fetch_many(repos, full=full, limit=params.get("limit"))
    report(
        f"抓取完成：拉取 {stats.fetched}、新增 {stats.created}、更新 {stats.updated}、"
        f"未变 {stats.unchanged}"
    )
    return {
        "fetched": stats.fetched,
        "created": stats.created,
        "updated": stats.updated,
        "unchanged": stats.unchanged,
        "error": stats.error,
    }


async def _work_evaluate(app: FastAPI, params: dict[str, Any], report: Report) -> dict[str, Any]:
    from ..pipeline.stages import EvalStage

    ctx = _ctx_of(app)
    keys = list(params.get("keys") or [])
    items = ctx.repo.items_by_keys(keys) if keys else ctx.repo.items_needing_eval(
        limit=_limit_param(params, 20)
    )
    if not items:
        report("没有需要评测的条目")
        return {"evaluated": 0, "failed": 0, "results": []}
    report(f"开始评测 {len(items)} 条（{'深度' if params.get('deep') else '常规'}）")
    result = await EvalStage(ctx).run_batch(items, deep=bool(params.get("deep")))
    report(f"评测完成：成功 {result.evaluated}、失败 {result.failed}")
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


async def _work_verify(app: FastAPI, params: dict[str, Any], report: Report) -> dict[str, Any]:
    from ..pipeline.stages import PRVerifyStage, VerifyStage

    ctx = _ctx_of(app)
    keys = list(params.get("keys") or [])
    items = (
        ctx.repo.items_by_keys(keys)
        if keys
        else ctx.repo.list_items(status=ItemStatus.EVALUATED, limit=_limit_param(params, 10))
    )
    if not items:
        report("没有待验证的条目")
        return {"processed": 0, "results": []}
    stage_issue, stage_pr = VerifyStage(ctx), PRVerifyStage(ctx)
    report(f"开始验证 {len(items)} 条")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        ev = ctx.repo.latest_evaluation(item.key)
        stage = stage_issue if item.item_type is ItemType.ISSUE else stage_pr
        report(f"[{index}/{len(items)}] 验证 {item.key}")
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
    report(f"验证完成：处理 {len(out)} 条")
    return {"processed": len(out), "results": out}


async def _work_fix(app: FastAPI, params: dict[str, Any], report: Report) -> dict[str, Any]:
    from ..fixer.autofix import AutoFixer

    settings = _settings_of(app)
    if not settings.auto_fix.enabled:
        raise HTTPException(status_code=403, detail="自动修复已禁用（auto_fix.enabled=false）")
    ctx = _ctx_of(app)
    fixer = AutoFixer(ctx)
    dry_run = bool(params.get("dry_run", True))
    keys = list(params.get("keys") or [])
    results: list[dict[str, Any]] = []
    if keys:
        for index, key in enumerate(keys, start=1):
            item = ctx.repo.get_item(key)
            if item is None:
                results.append({"key": key, "error": "不存在"})
                continue
            report(f"[{index}/{len(keys)}] 修复 {key}{'（dry-run）' if dry_run else ''}")
            attempt = await fixer.fix_item(item, dry_run=dry_run)
            results.append(_attempt_payload(attempt))
    else:
        limit = _limit_param(params, 5)
        report(f"挑选候选并修复（最多 {limit} 条，{'dry-run' if dry_run else '真提 PR'}）")
        outcome = await fixer.fix_candidates(limit=limit, dry_run=dry_run)
        results = [_attempt_payload(a) for a in outcome.attempts]
    report(f"修复完成：尝试 {len(results)} 条")
    return {"attempted": len(results), "dry_run": dry_run, "results": results}


async def _work_flush(app: FastAPI, params: dict[str, Any], report: Report) -> dict[str, Any]:
    from ..pipeline.flush import FlushProcessor
    from ..pipeline.queue import QueueManager

    ctx = _ctx_of(app)
    qm = QueueManager(ctx)
    handler = FlushProcessor(ctx).make_handler()
    # 队列名走 _enum_param：非法值给 400 + 可选值列表，而不是 `QueueName(x)` 抛 ValueError → 500。
    queue = _enum_param(params.get("queue"), QueueName, name="queue")
    force = bool(params.get("force", True))
    results: list[dict[str, Any]] = []
    for q in ([queue] if queue else list(QueueName)):
        decision = qm.should_flush(q, force=force)
        if not decision.should_flush:
            report(f"{q.value}：跳过（{decision.reason}）")
            results.append({"queue": q.value, "flushed": 0, "reason": decision.reason})
            continue
        report(f"{q.value}：开始 flush")
        outcome = await qm.flush(q, handler=handler, force=force)
        report(f"{q.value}：刷出 {outcome.flushed} 条结论")
        results.append(
            {
                "queue": q.value,
                "flushed": outcome.flushed,
                "conclusions": len(outcome.conclusions),
                "error": outcome.error,
            }
        )
    return {"results": results}


#: 作业种类 → 工作体。键直接出现在 URL 里（``POST /jobs/{kind}``），所以必须是白名单。
_JOB_WORK: dict[str, Callable[[FastAPI, dict[str, Any], Report], Awaitable[dict[str, Any]]]] = {
    "scan": _work_scan,
    "evaluate": _work_evaluate,
    "verify": _work_verify,
    "fix": _work_fix,
    "flush": _work_flush,
}


def _bind_job(
    app: FastAPI,
    work: Callable[[FastAPI, dict[str, Any], Report], Awaitable[dict[str, Any]]],
    params: dict[str, Any],
) -> Callable[[Job], Awaitable[dict[str, Any]]]:
    """把工作体包成作业体。

    工作体用 ``HTTPException`` 表达「这次请求本身不合法」（同步端点要的就是那些 400/403/404）。
    作业里没有 HTTP 响应可发，所以在这里把 ``detail`` 提出来、转成一条干净的失败原因——
    否则用户看到的会是「HTTPException: 400: 没有可扫描的仓库」这种糊着状态码的文案。
    """

    async def run(job: Job) -> dict[str, Any]:
        try:
            return await work(app, params, job.report)
        except HTTPException as exc:
            raise FissueError(str(exc.detail)) from exc

    return run


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


def _register_api(app: FastAPI, settings: Settings) -> None:
    if not settings.api.enabled:
        # api.enabled=false：不注册任何 REST 路由（页面仍在，只是没有 /api/v1/*）。
        log.info("api.enabled=false：不注册 REST API 路由")
        return

    prefix = settings.api.prefix
    auth = _auth_dependency()

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
            request.app, _settings(request), _repo(request), with_sandbox=sandbox
        )

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

    # -- 作业（长任务）：提交后立刻返回，前端轮询进度 -------------------------

    @app.get(f"{prefix}/jobs")
    async def list_jobs(request: Request, _: None = Depends(auth)) -> dict[str, Any]:
        return request.app.state.jobs.to_dict()

    @app.get(f"{prefix}/jobs/{{job_id}}")
    async def get_job(request: Request, job_id: str, _: None = Depends(auth)) -> dict[str, Any]:
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"作业不存在：{job_id}")
        return job.to_dict()

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
#
# 队列面板与状态快照都住在 ``web/status.py``：那是**同一份口径**，Web 状态页与
# `fissue status` 都读它。放在这里会让两处各拼一遍，迟早对不上。


def _register_web(app: FastAPI, settings: Settings, templates: Jinja2Templates) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        """总览：告警 + 统计 + 队列 + 用量 + 控制面板 + 任务与抓取记录。

        与「条目」页分开，是因为两者关心的事不同：总览回答「有没有问题、在不在跑、花了多少钱」，
        条目页回答「这一条是什么」。混在一页时，光筛选器就把告警挤到看不见的地方。
        """
        from ..store import export as exp

        r = _repo(request)
        st = _settings(request)
        snapshot = collect_status(request.app, st, r)
        return templates.TemplateResponse(
            request,
            "overview.html",
            _page_context(
                request,
                nav="overview",
                total=r.count_items(),
                summary=exp.summarize(r),
                queues=snapshot["queues"],
                usage=snapshot["llm"]["today"],
                budget=st.llm.budget,
                jobs=snapshot["jobs"],
                scans=snapshot["scans"],
                alerts=snapshot["alerts"],
                problems=[],
            ),
        )

    @app.get("/items", response_class=HTMLResponse)
    async def items_page(
        request: Request,
        repo_slug: str | None = Query(None),
        status: str | None = Query(None),
        category: str | None = Query(None),
        priority: str | None = Query(None),
        item_type: str | None = Query(None),
        q: str | None = Query(None),
        sort: str = Query("updated_at"),
        order: str = Query("desc"),
        limit: int = Query(50, ge=1, le=500),
        page: int = Query(1, ge=1),
    ) -> HTMLResponse:
        from ..store import export as exp
        from ..store.repository import ITEM_SORT_COLUMNS

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
            "sort": sort_key,
            "order": "desc" if order_desc else "asc",
            "limit": limit,
        }
        # 表头排序链接的底串：筛选 + 每页条数（sort/order 由链接自己追加）；
        # 翻页链接的底串另外带上 sort/order——否则一点「下一页」，排序就丢了。
        base_qs = urlencode({k: v for k, v in shown.items() if v and k not in ("sort", "order")})
        page_qs = urlencode({k: v for k, v in shown.items() if v})

        items = r.list_items(**filters, limit=limit, offset=offset,
                             order_by=sort_key, desc=order_desc)
        total = r.count_items(**filters)
        evals = r.evaluations_for([i.key for i in items])
        counts = r.comment_counts([i.key for i in items])
        rows = [
            exp.item_to_dict(i, evals.get(i.key), extra={"comments": counts.get(i.key, 0)})
            for i in items
        ]
        pages = max(1, (total + limit - 1) // limit)
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
                summary=exp.summarize(r, **filters),
                queues=queue_panel(r, st),
                usage=r.usage_today(),
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
                filters=shown,
            ),
        )

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
            request.app, _settings(request), _repo(request), with_sandbox=sandbox
        )
        return templates.TemplateResponse(
            request,
            "status.html",
            _page_context(request, nav="status", snapshot=snapshot),
        )

    @app.get("/queues", response_class=HTMLResponse)
    async def queues_page(request: Request) -> HTMLResponse:
        """队列页：积压计数 + 权威的 flush 判定（来自 QueueManager，不在这里重算）。"""
        st = _settings(request)
        r = _repo(request)
        from ..pipeline.queue import QueueManager

        ctx = _ctx_of(request.app)
        qm = QueueManager(ctx)
        decisions = {
            q.value: {
                "should_flush": (d := qm.should_flush(q)).should_flush,
                "reason": d.reason,
                "done_count": d.done_count,
            }
            for q in QueueName
        }
        return templates.TemplateResponse(
            request,
            "queues.html",
            _page_context(
                request,
                nav="queues",
                queues=queue_panel(r, st),
                decisions=decisions,
                stats=r.queue_stats(),
                jobs=request.app.state.jobs.to_dict(),
            ),
        )

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request) -> HTMLResponse:
        """任务页：作业历史（含日志）。提交入口在总览的控制面板。"""
        return templates.TemplateResponse(
            request,
            "jobs.html",
            _page_context(request, nav="jobs", jobs=request.app.state.jobs.to_dict()),
        )

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
        return templates.TemplateResponse(
            request,
            "item.html",
            _page_context(
                request,
                # `get_item` 会连带把评论读出来（selectinload），所以这里的
                # `comment_thread` 有内容——详情页因此能看到 Issue 正文与讨论。
                data=exp.item_to_dict(item, evaluation, batch=r.latest_batch_conclusion(key)),
                item=item,
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


# ---------------------------------------------------------------------------
# 模块级 app（``uvicorn fissue.web.app:app``）
# ---------------------------------------------------------------------------


def _lazy_app() -> FastAPI:  # pragma: no cover
    return create_app()
