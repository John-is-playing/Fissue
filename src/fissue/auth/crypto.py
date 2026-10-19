"""口令 / TOTP / API Key 的密码学原语（纯标准库）。

设计约束见 ``docs/AUTH.md`` §2 决策 1：**零新 Python 依赖**。所以这里是用
``hashlib.pbkdf2_hmac`` 与 ``hmac`` 手写的那几样，而不是 ``passlib`` / ``pyotp``。

分工
----
* 本模块只做**无状态**的密码学运算（哈希、比对、TOTP 计算、Key 生成）。
* 有状态的那部分（谁是谁、会话有没有过期）在 :mod:`fissue.auth.store`。
* 「口令强度」「限速退避」这类策略在 :mod:`fissue.auth.throttle` 与调用方。

一条贯穿全文件的规矩：**所有比对都走** ``hmac.compare_digest``。
``==`` 会在第一个不同的字节上返回，攻击者能按耗时逐字节猜出口令/TOTP——这不是理论问题，
是标准的时序侧信道。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

#: PBKDF2-HMAC-SHA256 迭代次数。
#:
#: 60 万次是 OWASP 对 PBKDF2-SHA256 的现行建议值。代价是**每次登录约 0.2~0.4 秒**——
#: 这正是它该有的样子（brute-force 也要付同样的钱）。它只在登录/改口令时走，
#: 不在每个请求上跑，所以不影响接口延迟。
PBKDF2_ITERATIONS = 600_000

#: 口令哈希的存储格式：``pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>``。
#: 迭代次数**存进哈希里**：将来加码时老口令仍能验证（用它们各自记录的次数）。
PASSWORD_SCHEME = "pbkdf2_sha256"
SALT_BYTES = 16

#: TOTP（RFC 6238）：SHA1 / 6 位 / 30 秒，密钥 20 字节（160 bit，RFC 4226 建议）。
TOTP_ALGORITHM = "SHA1"
TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_SECRET_BYTES = 20

#: API Key 的形状：``fissue_<prefix>_<secret>``。
#:
#: * ``prefix`` 是**公开**的标识（列表里显示、按它查表），与密钥本身无关；
#: * ``secret`` 是**私密**部分，只在创建时回一次明文。
#:
#: secret 用十六进制（不是 url-safe base64）是刻意的：base64 的字母表里含 ``_``，
#: 而 ``_`` 正是本格式的分隔符——一个 ``fissue_a_bc_d`` 就能被切出两种解释。
API_KEY_MARKER = "fissue"
API_KEY_PREFIX_BYTES = 4       # → 8 个十六进制字符
API_KEY_SECRET_BYTES = 24      # → 48 个十六进制字符

#: 旧 ``FISSUE_API_TOKEN`` 导入时占用的 prefix（见 docs/AUTH.md §7）。
#: 取成一个十六进制不可能产生的值，免得与真 Key 撞上。
LEGACY_KEY_PREFIX = "legacy"

#: 恢复码：10 个，一次性。
RECOVERY_CODE_COUNT = 10


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    """url-safe base64（去 padding）：能安全地放进 cookie 与 URL。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """``_b64e`` 的逆运算（容忍被去掉的 padding）。"""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def constant_time_equals(left: str, right: str) -> bool:
    """常数时间字符串比对（长度不同也走到比对里，别用 ``==`` 短路）。"""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """把口令编成可入库的字符串（随机 salt，每次结果都不同）。

    空口令直接拒绝：``web/auth`` 那层也会拦，但这里是最内层——真让它存下去，
    就是一个「空口令可登录」的账号。
    """
    if not password:
        raise ValueError("口令不能为空")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PASSWORD_SCHEME}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """校验口令。任何格式问题一律**返回 False**（不抛异常）。"""
    if not password or not encoded:
        return False
    try:
        scheme, raw_iterations, salt_b64, hash_b64 = encoded.split("$")
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(raw_iterations)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, TypeError):
        # 库里的哈希被人手改坏 / 是另一种 scheme：当作「验不过」，而不是 500。
        return False
    if iterations <= 0:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def password_needs_rehash(encoded: str, *, iterations: int = PBKDF2_ITERATIONS) -> bool:
    """库里的哈希是否低于当前标准（登录成功后顺手升级）。"""
    try:
        scheme, raw_iterations, _, _ = encoded.split("$")
    except ValueError:
        return True
    return scheme != PASSWORD_SCHEME or int(raw_iterations) < iterations


# ---------------------------------------------------------------------------
# TOTP（RFC 6238 / RFC 4226）
# ---------------------------------------------------------------------------


def generate_totp_secret(*, length: int = TOTP_SECRET_BYTES) -> str:
    """随机 base32 密钥（去 padding）——正是认证器 App 期待的形状。"""
    return base64.b32encode(os.urandom(length)).decode("ascii").rstrip("=")


def _decode_base32(secret: str) -> bytes:
    """认证器 App 常把密钥显示成带空格/小写/连字符的分组，这里一律容忍。"""
    cleaned = re.sub(r"[^A-Za-z2-7]", "", (secret or "").upper())
    if not cleaned:
        raise ValueError("TOTP 密钥为空")
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def totp_code(
    secret: str,
    *,
    at: datetime | None = None,
    digits: int = TOTP_DIGITS,
    period: int = TOTP_PERIOD,
) -> str:
    """算出 ``at`` 时刻的 TOTP。``at`` 缺省取当前 UTC 时间。"""
    key = _decode_base32(secret)
    when = at or datetime.now(timezone.utc)
    counter = int(when.timestamp()) // period
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    # 动态截断（RFC 4226 §5.3）：取最后 4 bit 作偏移，从那里读 4 字节。
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify_totp(
    secret: str,
    code: str,
    *,
    at: datetime | None = None,
    window: int = 1,
    digits: int = TOTP_DIGITS,
    period: int = TOTP_PERIOD,
) -> bool:
    """校验 TOTP，容忍 ``window`` 步时钟偏差（默认 ±1 步 = ±30 秒）。

    为什么给窗口：认证器所在手机的时间与服务器差几秒是常态，不给窗口会让「明明填对了
    却提示无效」。代价是暴力空间乘以 3，但 6 位码本身只有 100 万种，真正的防线是
    登录限速与「一个码只能用一次」的会话流程。
    """
    cleaned = (code or "").strip().replace(" ", "")
    if len(cleaned) != digits or not cleaned.isdigit():
        return False
    when = at or datetime.now(timezone.utc)
    for step in range(-window, window + 1):
        moment = when + timedelta(seconds=step * period)
        if constant_time_equals(totp_code(secret, at=moment, digits=digits, period=period), cleaned):
            return True
    return False


def otpauth_uri(secret: str, account: str, *, issuer: str = "Fissue") -> str:
    """``otpauth://`` URI（手动录入用；扫码要另配二维码，见 docs/AUTH.md §12）。"""
    label = f"{issuer}:{account}"
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": TOTP_ALGORITHM,
            "digits": TOTP_DIGITS,
            "period": TOTP_PERIOD,
        }
    )
    return f"otpauth://totp/{quote(label)}?{query}"


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str, str]:
    """生成一把新 Key，返回 ``(prefix, secret, 完整明文)``。

    明文**只在这一刻存在**：调用方把它回给用户后，库里只留 prefix 与哈希。
    """
    prefix = secrets.token_hex(API_KEY_PREFIX_BYTES)
    secret = secrets.token_hex(API_KEY_SECRET_BYTES)
    return prefix, secret, f"{API_KEY_MARKER}_{prefix}_{secret}"


def parse_api_key(token: str) -> tuple[str, str] | None:
    """拆 ``fissue_<prefix>_<secret>``；不是这个形状就返回 ``None``。

    ``None`` 不代表「无效令牌」，而是「这压根不是本站的 Key 格式」——调用方据此决定
    要不要再去试旧口令那条兼容路径（docs/AUTH.md §7）。
    """
    parts = (token or "").strip().split("_")
    if len(parts) != 3 or parts[0] != API_KEY_MARKER or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def hash_token(token: str) -> str:
    """Token / API Key 的存储哈希。

    这里用**裸 SHA-256** 而不是 PBKDF2，理由是输入本身已经是高熵随机串（48 hex = 192 bit）：
    KDF 的作用是拖慢对**低熵**口令的猜测，对随机密钥毫无增益，却要在每个请求上付钱。
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def verify_token(token: str, expected_hash: str) -> bool:
    """常数时间比对 Token 与它的存储哈希。"""
    if not token or not expected_hash:
        return False
    return hmac.compare_digest(hash_token(token), expected_hash)


# ---------------------------------------------------------------------------
# 恢复码
# ---------------------------------------------------------------------------


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """生成一次性恢复码（形如 ``a1b2c-3d4e5``）。"""
    codes: list[str] = []
    for _ in range(count):
        raw = secrets.token_hex(5)
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    """去掉分隔与大小写差异，让用户「照着抄」时不必纠结格式。"""
    return re.sub(r"[^0-9a-f]", "", (code or "").lower())


# ---------------------------------------------------------------------------
# 会话 cookie / 2FA challenge：签名与拆封
# ---------------------------------------------------------------------------


def new_session_id() -> str:
    """会话 id：32 字节随机（docs/AUTH.md §4 的 ``sessions.id``）。"""
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def sign_value(value: str, secret: bytes) -> str:
    """``<value>.<hmac_b64>``——cookie 里的会话 id、2FA challenge 都走它。

    为什么 DB 里已经有会话记录了还要签名：验签是**本地**的（不发 SQL）。一个伪造的
    cookie 在签名这一步就被打回，不会让数据库白白挨一轮查询——顺手把「拿随机串狂打
    会话查询」这条路也堵上。
    """
    mac = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).digest()
    return f"{value}.{_b64e(mac)}"


def unsign_value(signed: str, secret: bytes) -> str | None:
    """验签并取出原值；签名不对/格式不对返回 ``None``。"""
    value, sep, signature = (signed or "").rpartition(".")
    if not sep or not value or not signature:
        return None
    expected = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).digest()
    try:
        raw = _b64d(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(raw, expected):
        return None
    return value


def sign_challenge(user_id: int, secret: bytes, *, ttl_seconds: int = 300) -> str:
    """2FA challenge：无状态签名串，避免为一个「输验证码」的中间态维护服务端内存。"""
    expires = int(datetime.now(timezone.utc).timestamp()) + ttl_seconds
    return sign_value(f"{user_id}:{expires}", secret)


def read_challenge(token: str, secret: bytes) -> int | None:
    """解析 challenge，返回 user_id；过期或验签失败返回 ``None``。"""
    value = unsign_value(token, secret)
    if not value:
        return None
    raw_id, _, raw_expires = value.partition(":")
    try:
        user_id = int(raw_id)
        expires = int(raw_expires)
    except ValueError:
        return None
    if expires < int(datetime.now(timezone.utc).timestamp()):
        return None
    return user_id
