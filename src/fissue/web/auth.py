"""Web 层的鉴权：身份判定、会话 cookie、CSRF、``/api/auth/*`` 与 ``/login``。

这一层把 :mod:`fissue.auth`（纯 Python 内核）接到 HTTP 上。完整设计见 ``docs/AUTH.md``；
这里只记录「为什么这么接」。

三种身份来源（互为「或」）
--------------------------
============================  ==============================================
回环（loopback）               永远免登录——保住本机自用的手感
可信网段（``web.trusted_networks``）  等同回环：免登录、可写配置
会话 / API Key                 其余来源（含对外）：必须持有有效身份
============================  ==============================================

判定顺序：**先看来源能不能免身份**，不能才去看 cookie 与 Bearer。反过来的话，
本机浏览器会先被自己的 401 拦住，白折腾一轮。

一条容易踩的规矩：`web.auth.enabled=false` **不等于**「谁都能进」。它只是关掉了
「会话这个机制」——来源仍然必须是回环或可信网。把它理解成「关闭鉴权」是把控制面
直接送出去，那正是 ``Settings._validate`` 要拦的组合。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..auth import (
    LEGACY_KEY_PREFIX,
    AuthStore,
    LoginThrottle,
    constant_time_equals,
    consume_bootstrap_token,
    generate_api_key,
    generate_recovery_codes,
    generate_totp_secret,
    hash_password,
    hash_token,
    issue_bootstrap_token,
    new_csrf_token,
    new_session_id,
    normalize_recovery_code,
    otpauth_uri,
    parse_api_key,
    password_needs_rehash,
    read_bootstrap_token,
    read_challenge,
    read_or_create_server_secret,
    sign_challenge,
    sign_value,
    unsign_value,
    verify_password,
    verify_token,
    verify_totp,
)
from ..auth.files import bootstrap_token_path
from ..config import Settings
from ..logging_setup import get_logger
from ..store.tables import AuthUserRow

#: 反代转发头的判据**只有一份**（定义在 setup.py）：两处各留一份迟早分叉，
#: 于是同一件事在两处得到相反结论。
from .setup import FORWARD_HEADERS

log = get_logger(__name__)

#: 会话 cookie 名。
COOKIE_NAME = "fissue_session"
#: 鉴权端点前缀。**刻意不挂** ``api.prefix``：登录是 Web 应用自己的事，而 `api.prefix`
#: 是可配的业务 API 版本前缀，且 `api.enabled=false` 时业务 API 干脆不注册——
#: 那时登录要是跟着一起消失，页面就永远进不去了。
AUTH_PREFIX = "/api/auth"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: 口令最短长度。8 位是「别再设 123456」的底线；真正防爆破靠的是退避锁定。
MIN_PASSWORD_LENGTH = 8
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

SOURCE_LOOPBACK = "loopback"
SOURCE_TRUSTED = "trusted"
SOURCE_SESSION = "session"
SOURCE_API_KEY = "api_key"
SOURCE_NONE = "none"

SOURCE_LABELS = {
    SOURCE_LOOPBACK: "本机回环（免登录）",
    SOURCE_TRUSTED: "可信网段（免登录）",
    SOURCE_SESSION: "浏览器会话",
    SOURCE_API_KEY: "API Key",
    SOURCE_NONE: "未认证",
}

BOOTSTRAP_HINT = (
    "服务端尚未创建管理员账号。请在运行服务的机器上打开 /login 完成初始化"
    "（本机访问可直接初始化；从网络访问需要控制台打印的引导口令，"
    "它同时写在 data/auth-bootstrap-token）。"
)


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """一次请求的身份判定结果。"""

    source: str = SOURCE_NONE
    username: str = ""
    user_id: int | None = None
    csrf_token: str = ""
    session_id: str = ""
    key_id: int | None = None
    key_name: str = ""
    network: str = ""

    @property
    def authenticated(self) -> bool:
        """持有了显式凭据（会话或 Key）。"""
        return self.source in (SOURCE_SESSION, SOURCE_API_KEY)

    @property
    def trusted_source(self) -> bool:
        """来源本身就免登录（回环 / 可信网段）。"""
        return self.source in (SOURCE_LOOPBACK, SOURCE_TRUSTED)

    @property
    def allowed(self) -> bool:
        return self.trusted_source or self.authenticated

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "authenticated": self.authenticated,
            "trusted_source": self.trusted_source,
            "username": self.username,
            "network": self.network,
            "key_name": self.key_name,
            "csrf_token": self.csrf_token,
        }


# ---------------------------------------------------------------------------
# 取「每次请求当下」的状态
# ---------------------------------------------------------------------------
#
# 一律现取，不缓存在闭包里：设置页改完配置会热重载（web/setup.py::reload_state），
# 闭包里那份是启动时的旧对象，拿它判身份会让「刚改的配置不生效」。


def settings_of(request: Request) -> Settings:
    state = request.app.state
    if getattr(state, "settings", None) is None:
        from .app import _ensure_state

        _ensure_state(request)
    return state.settings  # type: ignore[no-any-return]


def store_of(request: Request) -> AuthStore:
    state = request.app.state
    if getattr(state, "db", None) is None:
        from .app import _ensure_state

        _ensure_state(request)
    return AuthStore(state.db)


def server_secret(request: Request) -> bytes:
    """服务器签名密钥（进程内按路径缓存）。

    缓存挂在 ``app.state`` 上而不是模块级：同一个进程里可能被构造出多个应用（测试就是这么做的），
    模块级缓存会让两个应用的密钥互相污染。按**路径**做键，换 ``data_dir`` 时自动重新读。
    """
    settings = settings_of(request)
    path = str(settings.data_dir)
    cache: dict[str, bytes] | None = getattr(request.app.state, "auth_secrets", None)
    if cache is None:
        cache = {}
        request.app.state.auth_secrets = cache
    secret = cache.get(path)
    if secret is None:
        secret = read_or_create_server_secret(settings)
        cache[path] = secret
    return secret


def throttle_of(request: Request) -> LoginThrottle:
    throttle: LoginThrottle | None = getattr(request.app.state, "login_throttle", None)
    if throttle is None:
        throttle = LoginThrottle()
        request.app.state.login_throttle = throttle
    return throttle


def peer_host(request: Request) -> str:
    """**直接对端**的地址（TCP 层连上来的那一跳，不看任何转发头）。"""
    client = request.client
    return (client.host if client else "") or ""


def _xff_hops(raw: str) -> list[str]:
    """把 ``X-Forwarded-For`` 拆成地址列表；**出现无法解析的段就返回空列表**。

    为什么垃圾值要整条作废、而不是「跳过那一段继续找」：跳过等于让中间某段的垃圾把链路
    整体左移，攻击者只要塞一个坏段就能让判定落到更左边那个**他自己写**的地址上。
    前缀（``[2001:db8::1]:443`` / ``1.2.3.4:5678``）会先剥端口再判。
    """
    hops: list[str] = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        if text.startswith("["):
            text = text[1:].split("]", 1)[0]
        elif text.count(":") == 1:
            text = text.split(":", 1)[0]
        try:
            hops.append(str(ipaddress.ip_address(text)))
        except ValueError:
            return []
    return hops


def forwarded_address(request: Request, settings: Settings) -> str:
    """从转发头里取出**真实客户端**地址；取不到返回空串。

    取法**不是**「最右一跳」，而是从右往左、跳过我们声明的代理（``web.trusted_proxies``），
    第一个不属于代理的地址才是客户端。

    为什么不能只取最右一跳：``X-Forwarded-For`` 是每一跳代理**追加自己的直接对端**，链路形如
    ``客户端, 代理1, 代理2``。在同机双 nginx / cloudflared / ``ssh -L`` 这类两跳形态下，
    最右一跳是**上一层代理自己的地址**——它与本服务同机就是 ``127.0.0.1``，被判成回环来源后
    **免登录**；落在 ``trusted_networks`` 里则可写配置。那正是这个开关本来要堵的提权面。

    ``X-Real-IP`` 只在**没有** XFF 时作为回退（它没有「追加」语义，是代理自己写的单值）：
    两者都不可信时返回空串，交给调用方回退到对端地址——这里的失败必须是「不采信」。
    """
    hops = _xff_hops(request.headers.get("x-forwarded-for", ""))
    for hop in reversed(hops):
        if settings.web.proxy_network_of(hop) is not None:
            continue  # 我们自己声明的代理追加的那一跳：继续往左找
        return hop
    if hops:
        # 整条链路都是声明的代理（客户端地址没被写进来）：不猜，回退对端。
        return ""
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            return ""
    return ""


def client_host(request: Request, settings: Settings) -> str:
    """**视为真实客户端**的地址——转发头只在两层声明都满足时才采信。

    判定顺序（缺一不可）：

    1. ``web.trust_proxy`` 打开（部署形态是「反代 + 真实客户端地址」）；
    2. 请求的**直接对端**落在 ``web.trusted_proxies`` 里（这一跳确实是我们自己的代理）。

    两条都满足才用转发头里的地址；否则一律用对端地址。少了第 2 条的后果很直接：
    任何人直连本服务、加一行 ``X-Forwarded-For: 127.0.0.1`` 就会被当成回环来源，
    而回环是**免登录**的——等于鉴权形同虚设。
    """
    immediate = peer_host(request)
    if not settings.web.trust_proxy:
        return immediate
    if settings.web.proxy_network_of(immediate) is None:
        # 这一跳不是我们声明过的代理：它送来的转发头没有意义（很可能是伪造）。
        return immediate
    return forwarded_address(request, settings) or immediate


def _peer_address(request: Request, settings: Settings | None = None):
    host = client_host(request, settings) if settings is not None else peer_host(request)
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def source_of(request: Request, settings: Settings) -> tuple[str, str]:
    """来源本身能不能免身份 → ``(source, 命中的网段)``。

    对端地址解析不出来时（测试客户端 ``testclient``、Unix socket）按**回环**处理。
    这与 ``web/app.py::_reject_remote_write`` 是同一个口径：「不知道对端是谁」不等于
    「可疑」，拿它去拦会把本机正常用法一起挡掉。
    """
    addr = _peer_address(request, settings)
    if addr is None:
        return SOURCE_LOOPBACK, ""
    if addr.is_loopback:
        return SOURCE_LOOPBACK, ""
    network = settings.web.network_of(str(addr))
    if network is not None:
        return SOURCE_TRUSTED, str(network)
    return SOURCE_NONE, ""


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _identity_of_key(request: Request, token: str) -> Identity | None:
    """把 ``Authorization: Bearer <key>`` 换成身份；不是有效 Key 则 ``None``。"""
    store = store_of(request)
    row = None
    parsed = parse_api_key(token)
    if parsed is not None:
        prefix, secret = parsed
        candidate = store.key_by_prefix(prefix)
        # `revoked_at is None` 与哈希比对缺一不可：吊销是软删（保留审计行），
        # 忘了判它就会让「已吊销的 Key 照旧能用」。
        if (
            candidate is not None
            and candidate.revoked_at is None
            and verify_token(secret, candidate.key_hash)
        ):
            row = candidate
    if row is None:
        return None
    store.mark_key_used(row.id)
    return Identity(source=SOURCE_API_KEY, key_id=row.id, key_name=row.name)


def forwarded_without_declaration(
    request: Request, settings: Settings, identity: Identity
) -> list[str]:
    """请求带了反代转发头、但没有任何**声明**来支撑这种部署时，返回命中的头名。

    为什么这道补刀不能因为「来源看着是回环」就跳过——这正是它的主场：

        `web.host=127.0.0.1` + 同机 nginx 把公网接进来时，对端地址**就是** 127.0.0.1，
        与「用户自己的浏览器」在 HTTP 层完全无法区分。只看来源的话，这个控制面
        （改密钥、打平台标签、推修复队列）等于对全世界开放。

    所以判据不是「来源可不可信」，而是「这种部署有没有被**显式声明**」：

    * 已认证（会话 / API Key）→ 放行：身份是实打实核对过的，经不经代理都无所谓；
    * 声明了 ``web.trusted_networks`` → 放行：这是 docs/AUTH.md §6 给「反代 + 可信网」
      规定的显式配置方式（判定只看真实对端，不信任转发头本身）；
    * 声明了「反代 + 代理网段」（``web.trust_proxy`` + ``trusted_proxies``，且这一跳
      确实是那个代理）→ 放行：这种部署下转发头**本来就是**来源判定的一部分；
    * 其余 → 命中转发头就拒，把「静默开放」变成「响亮拒绝」。

    必须说清边界：**这是补刀，不是安全边界**。同机代理一个转发头都不设时，它与本机直连
    在协议上无法区分——真正的边界是身份，以及「反代部署要显式声明」这条约定。
    """
    if identity.authenticated or settings.web.trusted_networks:
        return []
    if settings.web.trust_proxy and settings.web.proxy_network_of(peer_host(request)) is not None:
        return []
    return [name for name in FORWARD_HEADERS if request.headers.get(name)]


def resolve_identity(request: Request) -> Identity:
    """判定请求身份。**每个请求调一次**（别跨请求缓存结果）。"""
    settings = settings_of(request)
    source, network = source_of(request, settings)
    if source != SOURCE_NONE:
        return Identity(source=source, network=network)
    if not settings.web.auth.enabled:
        # 会话机制被关掉：来源又不是回环/可信网 → 无身份可用。
        return Identity(source=SOURCE_NONE)
    cookie = request.cookies.get(COOKIE_NAME, "")
    if cookie:
        raw_id = unsign_value(cookie, server_secret(request))
        if raw_id:
            store = store_of(request)
            row = store.get_session(raw_id)
            if row is not None:
                user = store.user_by_id(row.user_id)
                store.touch_session(raw_id)
                return Identity(
                    source=SOURCE_SESSION,
                    username=user.username if user else "",
                    user_id=row.user_id,
                    csrf_token=row.csrf_token,
                    session_id=raw_id,
                )
    token = bearer_token(request)
    if token:
        identity = _identity_of_key(request, token)
        if identity is not None:
            return identity
    return Identity(source=SOURCE_NONE)


def enforce_csrf(request: Request, identity: Identity) -> None:
    """会话认证的**写**请求必须带 ``X-CSRF-Token``（docs/AUTH.md §3.5）。

    为什么只有会话需要：cookie 是浏览器**自动**带的，攻击者的页面能借用户的手发出请求
    （CSRF）；而 Bearer 头不会自动带，攻击者拿不到就伪造不了——所以 Key 请求豁免。
    回环/可信网来源也豁免：它们的身份本来就不来自 cookie。
    """
    if identity.source != SOURCE_SESSION:
        return
    if request.method.upper() in _SAFE_METHODS:
        return
    sent = request.headers.get("x-csrf-token", "")
    if not sent or not constant_time_equals(sent, identity.csrf_token):
        raise HTTPException(
            status_code=403,
            detail="CSRF 校验未通过：写请求需要带上当前会话的 X-CSRF-Token（刷新页面即可重新获取）。",
        )


def require_auth_dependency():
    """``Depends`` 用的鉴权闸门：所有 ``/api/*`` 与配置写入端点都过它。"""

    async def require_auth(request: Request) -> Identity:
        settings = settings_of(request)
        identity = resolve_identity(request)
        if identity.allowed:
            enforce_csrf(request, identity)
            return identity
        if not settings.web.auth.enabled:
            raise HTTPException(
                status_code=403,
                detail=(
                    "web.auth.enabled=false 且来源不是回环/可信网段：本服务不接受未声明的身份。"
                    "请在 config.yaml 打开 web.auth.enabled，"
                    "或把来源网段写进 web.trusted_networks。"
                ),
            )
        if store_of(request).user_count() == 0:
            # 尚无管理员：登录也不可能成功，所以给 403 + 明确的获取引导口令指引，
            # 而不是一个「让你去登录、登了也没账号」的 401（docs/AUTH.md §3.4）。
            raise HTTPException(status_code=403, detail=BOOTSTRAP_HINT)
        raise HTTPException(
            status_code=401,
            detail="未认证：请先登录（浏览器访问 /login），或带上 Authorization: Bearer <API Key>。",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return require_auth


#: **模块级**的依赖单例，端点用 ``Annotated[Identity, Depends(REQUIRE_AUTH)]`` 挂它。
#:
#: 为什么非要模块级、不能像 app.py 那样在 ``register()`` 里取一个局部变量：本模块有
#: ``from __future__ import annotations``，注解在运行时是**字符串**，FastAPI 要求值它
#: 才能认出这是依赖——而闭包里的局部名不在模块命名空间里，求值会失败，于是
#: `identity` 被当成一个**查询参数**，接口回 422「Field required」。
#: 这个坑在测试里表现为「所有带 Depends 的端点都 422」，非常容易被误读成路由没注册。
REQUIRE_AUTH = require_auth_dependency()


# ---------------------------------------------------------------------------
# 会话 cookie
# ---------------------------------------------------------------------------


def _set_session_cookie(response: Response, settings: Settings, signed_value: str) -> None:
    """下发会话 cookie。

    `HttpOnly` —— 脚本读不到它（XSS 也就偷不走会话）；
    `SameSite=Lax` —— 跨站发起的写请求不带它，CSRF 的第一道；
    `Secure` 由 ``web.auth.secure_cookie`` 控制（反代有 TLS 时才开，否则纯 HTTP 下
    浏览器会**整个丢掉**这个 cookie，表现为「登录了但立刻又要求登录」）。
    """
    response.set_cookie(
        COOKIE_NAME,
        signed_value,
        httponly=True,
        samesite="lax",
        secure=settings.web.auth.secure_cookie,
        max_age=settings.web.auth.session_ttl_minutes * 60,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _issue_session(
    request: Request, response: Response, settings: Settings, store: AuthStore, user: AuthUserRow
) -> str:
    """建会话、下发 cookie，返回 CSRF token（前端要立刻拿到它才能发写请求）。"""
    session_id = new_session_id()
    csrf_token = new_csrf_token()
    store.create_session(
        user_id=user.id,
        session_id=session_id,
        csrf_token=csrf_token,
        ttl_minutes=settings.web.auth.session_ttl_minutes,
        user_agent=request.headers.get("user-agent", ""),
        # 审计要记「谁在用」，所以在反代部署（web.trust_proxy）下必须记真实客户端地址；
        # 记成代理 IP 的话，这一列在反代场景里全是同一个值，等于没有审计价值。
        peer_ip=client_host(request, settings),
    )
    _set_session_cookie(response, settings, sign_value(session_id, server_secret(request)))
    return csrf_token


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class LoginIn(BaseModel):
    username: str = ""
    password: str = Field(default="", repr=False)


class TwoFactorIn(BaseModel):
    challenge: str = ""
    code: str = ""


class BootstrapIn(BaseModel):
    bootstrap_token: str = Field(default="", repr=False)
    username: str = "admin"
    password: str = Field(default="", repr=False)


class PasswordIn(BaseModel):
    current_password: str = Field(default="", repr=False)
    new_password: str = Field(default="", repr=False)


class CodeIn(BaseModel):
    code: str = ""


class DisableTwoFactorIn(BaseModel):
    password: str = Field(default="", repr=False)
    code: str = ""


class ApiKeyIn(BaseModel):
    name: str = "默认"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _ensure_auth_enabled(settings: Settings) -> None:
    if not settings.web.auth.enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "web.auth.enabled=false：服务端只接受回环/可信网来源，浏览器登录已关闭。"
                "要重新启用登录，请在 config.yaml 打开 web.auth.enabled。"
            ),
        )


def _qr_svg(uri: str) -> str:
    """把 ``otpauth://`` URI 渲成内联 SVG；失败时回空串。

    为什么失败不抛：二维码只是「让用户少手抄一遍」的便利，而**手抄密钥那条路永远在**
    （页面上密钥与 URI 都照旧显示）。为了一张图把整个绑定流程变成 500，
    是把便利做成了单点故障。

    为什么自带编码器而不是调外部服务：见 ``web/qr.py`` 的模块说明——把 TOTP 密钥
    交给第三方生成图片，等于把二次验证的密钥交给别人。
    """
    from .qr import encode, to_svg

    try:
        return to_svg(encode(uri, ecc="M"))
    except ValueError as exc:  # pragma: no cover - 只有用户名异常长才会走到
        log.warning("生成 2FA 二维码失败，退回手抄密钥：%s", exc)
        return ""


def _acting_admin(request: Request, identity: Identity) -> AuthUserRow:
    """当前操作作用于哪个账号。

    * 会话身份 → 那个账号；
    * 回环/可信网来源（免登录）→ 管理员账号本身。

    后者是刻意的：本机打开页面改口令/建 Key 是**主要**使用方式，
    要求先登录一次反而是多余的一步（而且首次引导时根本没有账号可登）。
    """
    store = store_of(request)
    if identity.user_id is not None:
        user = store.user_by_id(identity.user_id)
        if user is not None:
            return user
    admin = store.admin()
    if admin is None:
        raise HTTPException(status_code=409, detail=BOOTSTRAP_HINT)
    return admin


def _check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"口令至少 {MIN_PASSWORD_LENGTH} 位（当前 {len(password)} 位）。"
        )


def _check_username(username: str) -> str:
    name = (username or "").strip()
    if not _USERNAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="用户名只能用字母、数字、下划线、点、连字符，长度 1~64（例如 admin）。",
        )
    return name


def _throttle_keys(request: Request, username: str) -> list[str]:
    """限速的两个维度：来源 IP + 用户名（见 ``fissue.auth.throttle`` 的说明）。

    来源一律取 :func:`client_host` 而不是直接对端：反代部署下（``web.trust_proxy``）所有
    请求的对端都是代理，用对端做键会把**全部客户端压进同一个桶**——一个人被锁，所有人一起
    被锁；反过来，限速也就失去了「按来源分摊」的意义。真实客户端地址在那种部署里是可信的
    （转发头只在「开关打开 + 对端是声明的代理」时才采信，见 ``client_host``）。
    """
    ip = client_host(request, settings_of(request)) or "unknown"
    return [f"ip:{ip}", f"user:{(username or '').strip().lower()}"]


def _too_many_attempts(seconds: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=f"尝试过于频繁：已连续失败多次，请 {seconds} 秒后再试。",
        headers={"Retry-After": str(seconds)},
    )


def warn_if_unprovisioned(settings: Settings, store: AuthStore) -> str:
    """没有管理员账号时生成引导口令并打印。返回口令（已存在则返回原值，空串表示不需要）。"""
    if store.user_count() > 0:
        return ""
    existing = read_bootstrap_token(settings)
    token = existing or issue_bootstrap_token(settings)
    log.warning(
        "尚无管理员账号。请在浏览器打开 http://%s:%s/login 完成初始化；"
        "若从其它机器访问，需要这条一次性引导口令（也写在 %s）：\n    %s",
        settings.web.host,
        settings.web.port,
        bootstrap_token_path(settings),
        token,
    )
    return token


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def register(app: FastAPI, settings: Settings) -> None:
    """注册 ``/api/auth/*`` 与 ``/login`` / ``/api-keys`` 两个页面。

    按域拆开注册。原先这里是一个 385 行的单函数：路由一多，就没人能一眼看出「哪个域有
    哪些端点、各自的闸门挂在哪」。切分只按域移动代码，**注册顺序保持原样**。
    """
    _register_login_page(app, settings)
    _register_session(app)
    _register_bootstrap(app)
    _register_account(app)
    _register_two_factor(app)
    _register_api_keys(app)
    _register_api_keys_page(app)


def _templates(request: Request) -> Any:
    return request.app.state.templates


def _page_ctx(request: Request, *, nav: str = "", **extra: Any) -> dict[str, Any]:
    from .app import _page_context

    return _page_context(request, nav=nav, **extra)


def _register_login_page(app: FastAPI, settings: Settings) -> None:
    """登录页：一个页面里走完「用户名+口令」→「2FA」两步。"""
    # -- 登录页 ----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        """登录页：一个页面里走完「用户名+口令」→「2FA」两步。

        独立渲染（不套 base.html）：登录前那个左侧导航没有任何一条可用链接
        （点哪都是 401），套上去只会把用户引到死路。
        """
        identity = resolve_identity(request)
        if identity.allowed:
            return RedirectResponse("/", status_code=303)
        store = store_of(request)
        return _templates(request).TemplateResponse(
            request,
            "login.html",
            {
                "version": __import__("fissue").__version__,
                "static_url": request.url_for("static", path="app.css"),
                "script_url": request.url_for("static", path="app.js"),
                "api_prefix": settings.api.prefix,
                "auth_prefix": AUTH_PREFIX,
                "no_admin": store.user_count() == 0,
                "bootstrap_token_required": not identity.trusted_source,
                "trusted_source": identity.trusted_source,
                "next": request.query_params.get("next", "/"),
            },
        )

def _register_session(app: FastAPI) -> None:
    """会话：登录 → （2FA 校验）→ 建会话 → 登出，以及「当前是谁」。"""
    prefix = AUTH_PREFIX

    # -- 会话 ------------------------------------------------------------

    @app.post(f"{prefix}/login")
    async def login(
        request: Request, response: Response, body: LoginIn
    ) -> dict[str, Any]:
        current = settings_of(request)
        _ensure_auth_enabled(current)
        store = store_of(request)
        throttle = throttle_of(request)
        keys = _throttle_keys(request, body.username)
        if (locked := throttle.locked_seconds(keys)):
            raise _too_many_attempts(locked)
        user = store.admin()
        if user is None:
            raise HTTPException(status_code=409, detail=BOOTSTRAP_HINT)
        # 用户名与口令**都要**比：只比口令会让「账号不存在」这条路径提前返回，
        # 攻击者据此就能枚举用户名（单管理员场景下意义不大，但别养这个习惯）。
        username_ok = constant_time_equals(user.username, body.username.strip())
        password_ok = verify_password(body.password, user.password_hash)
        if not (username_ok and password_ok):
            wait = throttle.record_failure(keys, max_failures=current.web.auth.login_max_failures)
            detail = "用户名或口令不正确。"
            if wait:
                detail = f"用户名或口令不正确；连续失败过多，已锁定 {wait} 秒。"
            raise HTTPException(status_code=401, detail=detail)
        throttle.reset(keys)
        if user.password_hash and password_needs_rehash(user.password_hash):
            # 迭代次数加码后，登录一次就把老哈希升级掉（用户无感）。
            store.set_password(user.id, hash_password(body.password))
        if user.totp_enabled and user.totp_secret:
            return {
                "ok": True,
                "requires_2fa": True,
                "challenge": sign_challenge(user.id, server_secret(request)),
            }
        csrf_token = _issue_session(request, response, current, store, user)
        return {"ok": True, "requires_2fa": False, "username": user.username, "csrf_token": csrf_token}

    @app.post(f"{prefix}/2fa")
    async def two_factor(
        request: Request, response: Response, body: TwoFactorIn
    ) -> dict[str, Any]:
        current = settings_of(request)
        _ensure_auth_enabled(current)
        user_id = read_challenge(body.challenge, server_secret(request))
        if user_id is None:
            raise HTTPException(status_code=400, detail="验证步骤已过期，请重新登录。")
        store = store_of(request)
        user = store.user_by_id(user_id)
        if user is None or not user.totp_enabled or not user.totp_secret:
            raise HTTPException(status_code=400, detail="该账号未开启两步验证，请重新登录。")
        throttle = throttle_of(request)
        keys = _throttle_keys(request, user.username)
        if (locked := throttle.locked_seconds(keys)):
            raise _too_many_attempts(locked)
        if verify_totp(user.totp_secret, body.code):
            throttle.reset(keys)
            csrf_token = _issue_session(request, response, current, store, user)
            return {"ok": True, "username": user.username, "csrf_token": csrf_token}
        # 恢复码：与 TOTP 同一条路，用掉一个就少一个。
        normalized = normalize_recovery_code(body.code)
        stored = list(user.recovery_code_hashes or [])
        for index, hashed in enumerate(stored):
            if verify_token(normalized, str(hashed)):
                remaining = stored[:index] + stored[index + 1 :]
                store.set_recovery_codes(user.id, [str(x) for x in remaining])
                throttle.reset(keys)
                csrf_token = _issue_session(request, response, current, store, user)
                log.warning("账号 %s 使用恢复码登录，剩余 %d 个", user.username, len(remaining))
                return {
                    "ok": True,
                    "username": user.username,
                    "csrf_token": csrf_token,
                    "recovery_code_used": True,
                    "recovery_codes_left": len(remaining),
                }
        wait = throttle.record_failure(keys, max_failures=current.web.auth.login_max_failures)
        detail = "验证码不正确。"
        if wait:
            detail = f"验证码不正确；连续失败过多，已锁定 {wait} 秒。"
        raise HTTPException(status_code=401, detail=detail)

    @app.post(f"{prefix}/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        cookie = request.cookies.get(COOKIE_NAME, "")
        if cookie:
            raw_id = unsign_value(cookie, server_secret(request))
            if raw_id:
                store_of(request).delete_session(raw_id)
        _clear_session_cookie(response)
        return {"ok": True}

    @app.get(f"{prefix}/me")
    async def me(request: Request) -> dict[str, Any]:
        """当前身份。**刻意不挂鉴权依赖**：前端要靠它区分「没登录」与「没有账号」，
        才能决定是显示登录表单还是引导表单。"""
        current = settings_of(request)
        identity = resolve_identity(request)
        store = store_of(request)
        admin = store.admin()
        provisioned = admin is not None
        return {
            **identity.to_dict(),
            "allowed": identity.allowed,
            "auth_enabled": current.web.auth.enabled,
            "no_admin": not provisioned,
            "two_factor": bool(admin and admin.totp_enabled),
            "sessions": len(store.list_sessions(limit=100)),
            "api_keys": store.key_count(active_only=True),
            "trusted_networks": list(current.web.trusted_networks),
        }

def _register_bootstrap(app: FastAPI) -> None:
    """首次引导：建第一个管理员账号（只走一次）。"""
    prefix = AUTH_PREFIX

    # -- 首次引导 --------------------------------------------------------

    @app.post(f"{prefix}/bootstrap")
    async def bootstrap(
        request: Request, response: Response, body: BootstrapIn
    ) -> dict[str, Any]:
        current = settings_of(request)
        store = store_of(request)
        if store.user_count() > 0:
            raise HTTPException(
                status_code=409, detail="已经有管理员账号了：引导流程只走一次，请直接登录。"
            )
        identity = resolve_identity(request)
        expected = read_bootstrap_token(current)
        supplied = body.bootstrap_token.strip()
        # 回环/可信网来源免引导口令（本机部署不该被迫去翻控制台），其余必须持有它。
        if not identity.trusted_source and (
            not expected or not supplied or not constant_time_equals(expected, supplied)
        ):
            raise HTTPException(status_code=403, detail=BOOTSTRAP_HINT)
        username = _check_username(body.username or "admin")
        _check_password_strength(body.password)
        user = store.create_user(username, hash_password(body.password))
        consume_bootstrap_token(current)
        csrf_token = _issue_session(request, response, current, store, user)
        log.info("已创建管理员账号：%s", username)
        return {"ok": True, "username": user.username, "csrf_token": csrf_token}

def _register_account(app: FastAPI) -> None:
    """账号自身：改口令（并踢掉其它会话）。"""
    prefix = AUTH_PREFIX

    # -- 账号自身 --------------------------------------------------------

    @app.post(f"{prefix}/password")
    async def change_password(
        request: Request, body: PasswordIn, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        store = store_of(request)
        user = _acting_admin(request, identity)
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=401, detail="当前口令不正确。")
        _check_password_strength(body.new_password)
        store.set_password(user.id, hash_password(body.new_password))
        # 改了口令就把**其它**会话踢下线：否则「口令泄露后改口令」这个动作用处有限。
        kicked = store.delete_other_sessions(user.id, keep=identity.session_id)
        log.info("账号 %s 已修改口令，注销其它会话 %d 个", user.username, kicked)
        return {"ok": True, "kicked_sessions": kicked}

def _register_two_factor(app: FastAPI) -> None:
    """两步验证：生成密钥 / 查待激活 / 激活 / 关闭 / 重发恢复码。"""
    prefix = AUTH_PREFIX

    # -- 两步验证 --------------------------------------------------------

    @app.post(f"{prefix}/2fa/enroll")
    async def enroll_two_factor(
        request: Request, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        """生成待激活的 TOTP 密钥（此时**还没有**生效）。"""
        store = store_of(request)
        user = _acting_admin(request, identity)
        secret = generate_totp_secret()
        store.set_totp(user.id, secret=secret, enabled=False)
        uri = otpauth_uri(secret, user.username)
        return {
            "ok": True,
            "secret": secret,
            "otpauth_uri": uri,
            "qr_svg": _qr_svg(uri),
            "note": "用认证器 App 扫码（或手抄密钥），再用一次有效验证码确认开启——不确认不会生效。",
        }

    @app.get(f"{prefix}/2fa/pending")
    async def pending_two_factor(
        request: Request, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        """把「已生成、还没确认」的那把密钥交回去（连同二维码）。

        为什么要有它：`enroll` 的结果只活在**那一次响应**里，而用户扫完码再回头一看——
        页面刷新一下就没了，只能重新生成一把（旧的就废了）。有了这个端点，刷新后
        二维码还在原处。**同一把密钥在确认前反复显示是安全的**：它还没生效，
        而且本来就只给当前这个已登录的管理员看。
        """
        user = _acting_admin(request, identity)
        if not user.totp_secret or user.totp_enabled:
            return {"ok": True, "pending": False}
        uri = otpauth_uri(user.totp_secret, user.username)
        return {
            "ok": True,
            "pending": True,
            "secret": user.totp_secret,
            "otpauth_uri": uri,
            "qr_svg": _qr_svg(uri),
        }

    @app.post(f"{prefix}/2fa/activate")
    async def activate_two_factor(
        request: Request, body: CodeIn, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        store = store_of(request)
        user = _acting_admin(request, identity)
        if not user.totp_secret:
            raise HTTPException(status_code=400, detail="请先生成密钥（POST /api/auth/2fa/enroll）。")
        if not verify_totp(user.totp_secret, body.code):
            raise HTTPException(status_code=400, detail="验证码不正确：请确认手机时间与服务器一致。")
        store.set_totp(user.id, secret=user.totp_secret, enabled=True)
        codes = generate_recovery_codes()
        store.set_recovery_codes(user.id, [hash_token(normalize_recovery_code(c)) for c in codes])
        return {
            "ok": True,
            "recovery_codes": codes,
            "note": "恢复码**只显示这一次**，请立刻抄下来：验证器丢了只能靠它登录。",
        }

    @app.post(f"{prefix}/2fa/disable")
    async def disable_two_factor(
        request: Request, body: DisableTwoFactorIn, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        store = store_of(request)
        user = _acting_admin(request, identity)
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="口令不正确。")
        if user.totp_enabled and user.totp_secret and not verify_totp(user.totp_secret, body.code):
            raise HTTPException(status_code=400, detail="验证码不正确。")
        store.set_totp(user.id, secret=None, enabled=False)
        store.set_recovery_codes(user.id, [])
        return {"ok": True}

    @app.post(f"{prefix}/2fa/recovery")
    async def regenerate_recovery_codes(
        request: Request, body: PasswordIn, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        store = store_of(request)
        user = _acting_admin(request, identity)
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=401, detail="口令不正确。")
        if not user.totp_enabled:
            raise HTTPException(status_code=400, detail="尚未开启两步验证。")
        codes = generate_recovery_codes()
        store.set_recovery_codes(user.id, [hash_token(normalize_recovery_code(c)) for c in codes])
        return {
            "ok": True,
            "recovery_codes": codes,
            "note": "旧恢复码已全部作废；新码只显示这一次。",
        }

def _key_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked": row.revoked_at is not None,
        "legacy": row.prefix == LEGACY_KEY_PREFIX,
    }


def _register_api_keys(app: FastAPI) -> None:
    """API Key：列出 / 创建 / 吊销，以及旧口令手工导入。"""
    prefix = AUTH_PREFIX

    # -- API Key ---------------------------------------------------------

    @app.get(f"{prefix}/api-keys")
    async def list_api_keys(
        request: Request, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        """列出 Key。**绝不回明文**——库里也没有，只有哈希。"""
        _acting_admin(request, identity)
        keys = store_of(request).list_keys()
        return {
            "keys": [_key_payload(row) for row in keys],
            "active": sum(1 for row in keys if row.revoked_at is None),
            "note": "明文只在创建那一刻回一次；忘记请吊销后重建。",
        }

    @app.post(f"{prefix}/api-keys", status_code=201)
    async def create_api_key(
        request: Request, body: ApiKeyIn, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        _acting_admin(request, identity)
        store = store_of(request)
        prefix, secret, plaintext = generate_api_key()
        row = store.create_key(
            name=(body.name or "默认").strip()[:255], prefix=prefix, key_hash=hash_token(secret)
        )
        log.info("已创建 API Key：%s（prefix=%s）", row.name, prefix)
        return {
            **_key_payload(row),
            "key": plaintext,
            "note": "这是**唯一一次**看到明文。请现在复制走——库里只有哈希，找回不了。",
        }

    @app.post(f"{prefix}/api-keys/{{key_id}}/revoke")
    async def revoke_api_key(
        request: Request, key_id: int, identity: Annotated[Identity, Depends(REQUIRE_AUTH)]
    ) -> dict[str, Any]:
        _acting_admin(request, identity)
        store = store_of(request)
        if not store.revoke_key(key_id):
            raise HTTPException(status_code=404, detail="没有这把 Key，或它已经被吊销。")
        log.info("已吊销 API Key：id=%s", key_id)
        return {"ok": True, "keys": [_key_payload(row) for row in store.list_keys()]}

def _register_api_keys_page(app: FastAPI) -> None:
    """API Key 页面。"""

    # -- API Key 页面 ----------------------------------------------------

    @app.get("/api-keys", response_class=HTMLResponse)
    async def api_keys_page(request: Request) -> HTMLResponse:
        identity = resolve_identity(request)
        keys = store_of(request).list_keys() if identity.allowed else []
        return _templates(request).TemplateResponse(
            request,
            "apikeys.html",
            _page_ctx(
                request,
                nav="apikeys",
                identity=identity,
                keys=[_key_payload(row) for row in keys],
                auth_prefix=AUTH_PREFIX,
            ),
        )


# ---------------------------------------------------------------------------
# 页面守卫：未认证的页面加载直接送去 /login
# ---------------------------------------------------------------------------

#: 需要身份的页面路径（前缀匹配）。`/login` 与 `/static` 不在其列。
PAGE_PREFIXES = (
    "/items",
    "/status",
    "/queues",
    "/jobs",
    "/repos",
    "/settings",
    "/setup",
    "/item/",
    "/api-keys",
)


def _needs_identity(path: str) -> bool:
    if path == "/":
        return True
    return any(path == p.rstrip("/") or path.startswith(p) for p in PAGE_PREFIXES)


async def page_guard_middleware(request: Request, call_next: Any) -> Any:
    """未认证的页面加载 → 303 到 ``/login?next=...``。

    为什么放在中间件而不是每个路由上：页面有 11 条，逐条挂依赖迟早会漏一条——
    而漏掉的那条就是一个「进得去但处处报错」的页面。中间件只认路径前缀，
    一眼能看出覆盖范围。**只拦页面**：`/api/*` 由各自的依赖给出 401/403，
    `/static/*` 必须放行（登录页的样式与脚本就在这里）。
    """
    if request.method in _SAFE_METHODS and _needs_identity(request.url.path):
        identity = resolve_identity(request)
        if not identity.allowed:
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return await call_next(request)


__all__ = [
    "AUTH_PREFIX",
    "BOOTSTRAP_HINT",
    "COOKIE_NAME",
    "Identity",
    "SOURCE_API_KEY",
    "SOURCE_LOOPBACK",
    "SOURCE_NONE",
    "SOURCE_SESSION",
    "SOURCE_TRUSTED",
    "bearer_token",
    "enforce_csrf",
    "page_guard_middleware",
    "peer_host",
    "register",
    "require_auth_dependency",
    "resolve_identity",
    "settings_of",
    "source_of",
    "store_of",
    "warn_if_unprovisioned",
]
