"""鉴权内核：口令 / TOTP / API Key / 会话（`docs/AUTH.md` 的落地实现）。

这一层**不依赖 FastAPI**，是一个纯 Python 的库：

* CLI（``fissue auth set-password`` / ``reset``）要在**不起 Web 服务**的情况下改口令；
* Web 层（``fissue.web.auth``）负责 cookie、CSRF、路由与来源判定；
* 两者共用这里的原语与存储，避免「CLI 重置的口令 Web 认不了」这类分叉。

模块分工：

===================  ==========================================================
:mod:`crypto`        无状态密码学：口令哈希、TOTP、API Key、签名
:mod:`store`         有状态读写：``auth_users`` / ``sessions`` / ``api_keys``
:mod:`files`         ``data/`` 下的签名密钥与一次性引导口令（0600）
:mod:`throttle`      登录失败退避（进程内内存态）
===================  ==========================================================
"""

from __future__ import annotations

from .crypto import (
    API_KEY_MARKER,
    LEGACY_KEY_PREFIX,
    PBKDF2_ITERATIONS,
    constant_time_equals,
    generate_api_key,
    generate_recovery_codes,
    generate_totp_secret,
    hash_password,
    hash_token,
    new_csrf_token,
    new_session_id,
    normalize_recovery_code,
    otpauth_uri,
    parse_api_key,
    password_needs_rehash,
    read_challenge,
    sign_challenge,
    sign_value,
    totp_code,
    unsign_value,
    verify_password,
    verify_token,
    verify_totp,
)
from .files import (
    bootstrap_token_path,
    consume_bootstrap_token,
    issue_bootstrap_token,
    read_bootstrap_token,
    read_or_create_server_secret,
    server_secret_path,
)
from .store import AuthStore, as_utc
from .throttle import LoginThrottle

__all__ = [
    "API_KEY_MARKER",
    "LEGACY_KEY_PREFIX",
    "PBKDF2_ITERATIONS",
    "AuthStore",
    "LoginThrottle",
    "as_utc",
    "bootstrap_token_path",
    "constant_time_equals",
    "consume_bootstrap_token",
    "generate_api_key",
    "generate_recovery_codes",
    "generate_totp_secret",
    "hash_password",
    "hash_token",
    "issue_bootstrap_token",
    "new_csrf_token",
    "new_session_id",
    "normalize_recovery_code",
    "otpauth_uri",
    "parse_api_key",
    "password_needs_rehash",
    "read_bootstrap_token",
    "read_challenge",
    "read_or_create_server_secret",
    "server_secret_path",
    "sign_challenge",
    "sign_value",
    "totp_code",
    "unsign_value",
    "verify_password",
    "verify_token",
    "verify_totp",
]
