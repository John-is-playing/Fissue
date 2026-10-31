"""FastAPI 应用入口（Web 仪表盘 + REST API）。

形态（Q14 选 B：只读 + 操作）
----------------------------
* **只读**：列表、筛选、详情、评分理由、统计、导出。
* **操作**：手动重评、flush 队列、批准提 PR、批准合并、触发扫描。
* **仓库与密钥管理**：仓库侧在 `/repos` 页（列出 / 加入 / 改参数 / 启停 / 移出 + 抓取前探测，
  写进覆盖层）；密钥侧在 `/settings` 页（LLM Key、四平台 Token、**入站 webhook 签名密钥**、
  数据库连接串），只回「有没有配」与掩码，**绝不回显原值**。
  原先这里写着「尚未实现」并把责任推给已删除的 ``web.allow_repo_management`` 开关——
  那个开关确实从没有过路由（属于「配置写了却不生效」，已随 P2 清理删除），
  但仓库页与密钥面板早已在 ``web/setup.py`` 落地，注释留在这里只会误导下一个读代码的人。
  真正的写操作闸门是 ``setup_guard``（身份 + 转发头）与 ``web.read_only``。

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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .. import __version__ as FISSUE_VERSION
from ..config import Settings, load_settings
from ..errors import FissueError
from ..lockfile import WRITE_GATE, single_flight
from ..logging_setup import get_logger, setup_logging
from ..models import ItemStatus, ItemType, QueueName
from .auth import AUTH_PREFIX, page_guard_middleware
from .auth import register as register_auth
from .job_results import summarize_result
from .jobs import Job, JobRegistry
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


class ForceRequest(BaseModel):
    """取消强制修复（清除 force 标记；仍在待修队列的条目回退 needs_manual）。"""

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
        out = await FissueService(settings).run_once(round_name="webhook 触发的轮次")
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
        from .auth import AuthStore, warn_if_unprovisioned

        database = build_database(st)
        database.create_all()
        app.state.settings = st
        app.state.db = database
        app.state.repo = Repository(database)
        # 聚合状态缓存：后台线程每 2 秒重算一整套「不带过滤」的聚合数字，看板
        # 前台只读缓存——远端数据库的往返不再落在切换页面的路径上（见
        # web/cache.py 的模块说明）。build 闭包每次现取 app.state：设置页热重载
        # 会替换 state.settings，捕获在闭包里的旧对象不能用。
        from .cache import StatsCache
        from .status import collect_aggregates

        stats_cache = StatsCache(
            lambda: collect_aggregates(app, app.state.settings, app.state.repo)
        )
        app.state.stats_cache = stats_cache
        stats_cache.start()
        app.state.jobs.on_complete = stats_cache.bump
        # 鉴权是**启动期**就要就位的：还没有管理员账号时生成引导口令并打印（§3.4）。
        # 放在这里而不是请求期：它每次进程启动只该发生一次。
        store = AuthStore(database)
        warn_if_unprovisioned(st, store)
        log.info("Web/API 已就绪（只读=%s，API 前缀=%s）", st.web.read_only, st.api.prefix)
        yield
        # 先停聚合缓存的后台线程（它可能正握着数据库会话），再把在跑的作业收掉：
        # 作业里可能正握着 flock 或占着沙盒目录，直接退出只会留下「Task was
        # destroyed but no pending」与半截的临时目录。
        stats_cache.stop()
        await app.state.jobs.shutdown()
        log.info("Web/API 关闭")

    application = FastAPI(
        title="Fissue",
        description="Issue/PR 的 AI 评测、沙盒验证与自动修复",
        version=FISSUE_VERSION,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # 作业结果的「可读化」是一个纯函数（web/job_results.py）：模板里直接调用，
    # 不必每个路由各自把摘要塞进上下文——任务面板与任务详情页两处都要用。
    templates.env.globals["job_result_summary"] = summarize_result
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

    # REST API 与页面路由分别住在 routes_api / routes_pages：这里只负责按配置决定
    # 要不要挂，以及把共享的鉴权依赖与模板传下去。**惰性导入**是刻意的——那两个模块
    # 反过来要用本模块的 `_work_*` / `_page_context`，模块级互导会成环。
    from .routes_api import register_api
    from .routes_pages import register_web

    register_api(application, st)
    # 配置写入类端点（仓库管理 / 设置 / 初始化向导）住在 web/setup.py：
    # 它们与 /actions/* 是**两类不同的写**（后者写外部平台，前者写本机配置），
    # 所以边界也分开（见 setup.py 的 setup_guard）。鉴权依赖两者共用一份。
    from .setup import register as register_setup

    register_setup(application, st, auth=_auth_dependency())
    # 会话/API Key 的端点与 /login 页：挂在固定的 `/api/auth` 下，**不随 api.prefix**，
    # 也不受 api.enabled 影响——登录不该因为「业务 API 关了」而一起消失。
    register_auth(application, st)
    register_web(application, st, templates)
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
                "若这个控制面确实挂在反向代理后面：把代理网段写进 web.trusted_proxies 并打开 "
                "web.trust_proxy（此后按真实客户端地址判定）；"
                "只想放行内网，则把内网网段写进 web.trusted_networks。"
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
    from .auth import client_host, resolve_identity

    identity = resolve_identity(request)
    if identity.allowed:
        return
    host = client_host(request, settings)
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
                "若这台机器只有可信网络可达，可把网段写进 web.trusted_networks；"
                "若中间隔着反向代理，见 web.trust_proxy 与 web.trusted_proxies。"
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
            # 名字带上方法 + 路径：状态页上显示「网页写操作 POST /api/v1/queues/flush」
            # 比一句「Web/API 写操作」有用得多——用户能立刻对上自己刚才点了什么。
            async with _write_guard(
                settings, action=f"网页写操作 {request.method} {request.url.path}"
            ):
                return await fn(*args, **kwargs)
        except FissueError as exc:
            # 409 而不是 500：这是「此刻有别处在写，你稍后再来」，不是服务故障
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return wrapper


@asynccontextmanager
async def _write_guard(
    settings: Settings, *, action: str, kind: str = "request"
) -> AsyncIterator[None]:
    """写操作的互斥：进程内闸门 + 跨进程 flock。

    抽成独立上下文管理器，是因为**后台作业也要用它**：作业由请求提交，但请求返回后锁不能
    跟着放（否则作业还在跑，另一次写就能同时进来）。作业在自己的任务里进出这两道闸门，
    见 ``web/jobs.py``。

    ``action`` / ``kind`` 会一路传到状态页（见 ``lockfile.GATE_KINDS``），于是页面上
    能说清「现在是谁在写、写了多久」，而不是只报一个 pid。
    """
    async with WRITE_GATE.hold_or_fail(action=action, kind=kind):
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


def _aggregates_of(app: FastAPI) -> dict[str, Any] | None:
    """后台刷新缓存里的聚合快照。

    ``None`` 表示缓存还没就绪（TestClient 没跑 lifespan 的兜底路径、或启动后
    首份快照未算完）——调用方回退到**现算**，页面功能不受影响，只是那一次
    多付几个数据库往返。
    """
    cache = getattr(app.state, "stats_cache", None)
    if cache is None:
        return None
    return cache.get() or None


def _queue_block(request: Request) -> dict[str, Any]:
    """队列块的数据：积压计数 + 权威 flush 判定。

    为什么抽成函数：流水线页（看板要显示「该刷了吗」并给批量入口）与独立的队列页
    都要这份数据。两处各拼一遍，迟早出现「看板说该刷、队列页说不用刷」——而 flush
    会真调 LLM 出结论、并往平台上打标签，口径不一致的代价不是刷新一下就能抹掉的。

    「该不该 flush」**只读** ``QueueManager.should_flush``，页面不重算（同一个理由：
    第二份实现必然漂移）。
    """
    from ..pipeline.queue import QueueManager

    st = _settings(request)
    r = _repo(request)
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
    agg = _aggregates_of(request.app)
    queues = (agg or {}).get("queues")
    if queues is None:
        queues = queue_panel(r, st)
    return {
        "queues": queues,
        "decisions": decisions,
        # 明细统计就是面板里那份 counts（同一口径）——不再为它单开一次 queue_stats 查询。
        "queue_stats": {q["name"]: q["counts"] for q in queues},
    }


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


async def _work_force(app: FastAPI, params: dict[str, Any], report: Report) -> dict[str, Any]:
    """强制修复：无论评测结论（分类 / 刷量 / 优先级 / 结论）如何，把指定条目推进修复流程。

    每个 key：置 force 标记 → 过一遍验证阶段（标记会旁路分类 / 刷量 / 可复现闸门，
    顺带补齐验证器）→ 直落「待修 tier1」，由修复循环（常驻服务 / CLI / 「修复选中」）
    接力提 PR。已在修复中 / 已提 PR 的条目保持原状。
    """
    from ..pipeline.stages import VerifyStage

    ctx = _ctx_of(app)
    keys = list(params.get("keys") or [])
    if not keys:
        raise HTTPException(status_code=400, detail="请用 keys 指定要强制修复的条目")
    stage = VerifyStage(ctx)
    results: list[dict[str, Any]] = []
    for index, key in enumerate(keys, start=1):
        item = ctx.repo.get_item(key)
        if item is None:
            report(f"[{index}/{len(keys)}] {key}：条目不存在，跳过")
            results.append({"key": key, "error": "不存在"})
            continue
        if item.item_type is not ItemType.ISSUE:
            report(f"[{index}/{len(keys)}] {key}：PR 没有自动修复流程，不能强制")
            results.append({"key": key, "error": "PR 没有自动修复流程"})
            continue
        if item.status in (ItemStatus.FIXING, ItemStatus.PR_CREATED):
            report(f"[{index}/{len(keys)}] {key}：已在修复（或已提 PR），保持状态")
            results.append(
                {"key": key, "status": item.status.value, "note": "已在修复流程中（或已完成）"}
            )
            continue
        ctx.repo.set_force_fix(key, True)
        item = ctx.repo.get_item(key)
        if item is None:  # 理论上不会消失（刚在上面读过）；防御
            results.append({"key": key, "error": "处理中消失"})
            continue
        report(f"[{index}/{len(keys)}] 强制修复 {key}（进验证/修复流程）")
        try:
            r = await stage.run(item, ctx.repo.latest_evaluation(key))
        except Exception as exc:
            # 与 _work_verify 同口径：逐条兜住，一条失败不该让整批结果全丢。
            report(f"{key}：强制修复失败：{type(exc).__name__}: {exc}")
            results.append({"key": key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        fresh = ctx.repo.get_item(key)
        results.append(
            {
                "key": key,
                "status": fresh.status.value if fresh else (r.status.value if r.status else ""),
                "note": "；".join(r.notes) or r.skipped_reason or "",
            }
        )
    report(f"强制修复完成：处理 {len(results)} 条")
    return {"processed": len(results), "results": results}


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
    "force": _work_force,
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
# REST API 与 Web 页面路由
# ---------------------------------------------------------------------------
#
# 注册函数住在 `web/routes_api.py` 与 `web/routes_pages.py`：它们只依赖本模块的
# `_work_*` / `_page_context` 等共享工具，而本模块不反向依赖它们（create_app 里惰性
# 导入）。**共享工具留在这里**，是因为同一份实现同时服务 REST、页面与后台作业——
# 拆成两处必然漂移。


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
# 模块级 app（``uvicorn fissue.web.app:app``）
# ---------------------------------------------------------------------------


def _lazy_app() -> FastAPI:  # pragma: no cover
    return create_app()
