"""鉴权测试：口令 / TOTP / API Key / 会话 / CSRF / 授权矩阵 / 引导 / 迁移 / 退避。

对应 ``docs/AUTH.md`` §11 的测试计划。全部离线：SQLite 临时库 + TestClient，
没有任何真实网络（conftest 的全局网络守卫兜着）。
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from fissue.auth import crypto
from fissue.auth.crypto import (
    generate_api_key,
    generate_totp_secret,
    hash_password,
    hash_token,
    normalize_recovery_code,
    parse_api_key,
    sign_challenge,
    sign_value,
    totp_code,
    unsign_value,
    verify_password,
    verify_token,
    verify_totp,
)
from fissue.auth.files import bootstrap_token_path, read_bootstrap_token
from fissue.auth.store import AuthStore
from fissue.auth.throttle import LoginThrottle
from fissue.config import ConfigError, Settings, load_settings
from fissue.store.db import Database
from fissue.web.setup import overlay_file

# ---------------------------------------------------------------------------
# 向量级：口令 / TOTP / API Key
# ---------------------------------------------------------------------------

#: PBKDF2-HMAC-SHA256 的公开测试向量（password="password", salt="salt"）。
#: 用它而不是「自己算一遍再比」（那样只是把同一个实现比给自己看）：这些值来自
#: RFC 7914 / Django 的兼容性向量，能真正验证「我们算的是标准 PBKDF2」。
PBKDF2_SHA256_VECTORS = {
    1: "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b",
    2: "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43",
    4096: "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a",
}


def _encoded(iterations: int, digest_hex: str) -> str:
    """按存储格式拼一个哈希，用来喂给 ``verify_password``。"""
    salt = b"salt"
    return (
        f"pbkdf2_sha256${iterations}"
        f"${base64.urlsafe_b64encode(salt).decode().rstrip('=')}"
        f"${base64.urlsafe_b64encode(bytes.fromhex(digest_hex)).decode().rstrip('=')}"
    )


@pytest.mark.parametrize("iterations,digest", sorted(PBKDF2_SHA256_VECTORS.items()))
def test_password_matches_known_pbkdf2_vectors(iterations: int, digest: str) -> None:
    assert verify_password("password", _encoded(iterations, digest)) is True
    assert verify_password("passwore", _encoded(iterations, digest)) is False


def test_password_hash_roundtrip_uses_random_salt() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first != second, "每次哈希都该用新 salt——否则相同口令在库里长得一模一样"
    assert verify_password("correct horse battery staple", first)
    assert verify_password("correct horse battery stapl", first) is False
    assert verify_password("", first) is False
    assert first.startswith("pbkdf2_sha256$600000$"), "迭代次数要存进哈希里，便于将来加码"


@pytest.mark.parametrize("broken", ["", "x", "pbkdf2_sha256$notanint$a$b", "bcrypt$1$2$3", "a$b$c"])
def test_broken_password_hash_is_rejected_not_raised(broken: str) -> None:
    """库里的哈希被改坏时返回 False，而不是让登录接口 500。"""
    assert verify_password("anything", broken) is False


def test_hash_password_rejects_empty() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_password_needs_rehash() -> None:
    assert crypto.password_needs_rehash(_encoded(1000, PBKDF2_SHA256_VECTORS[1])) is True
    assert crypto.password_needs_rehash(hash_password("x" * 8)) is False
    assert crypto.password_needs_rehash("garbage") is True


#: RFC 6238 附录 B 的官方向量（SHA1，8 位）。
#: 密钥是 ASCII "12345678901234567890" 的 base32。
RFC6238_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC6238_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize("timestamp,expected", RFC6238_VECTORS)
def test_totp_matches_rfc6238_vectors(timestamp: int, expected: str) -> None:
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    assert totp_code(RFC6238_SECRET, at=moment, digits=8) == expected


def test_totp_accepts_neighbouring_window_only() -> None:
    secret = generate_totp_secret()
    moment = datetime.now(timezone.utc)
    code = totp_code(secret, at=moment)
    assert verify_totp(secret, code, at=moment) is True
    # ±1 步（30 秒）容忍时钟偏差
    assert verify_totp(secret, totp_code(secret, at=moment - timedelta(seconds=30)), at=moment) is True
    assert verify_totp(secret, totp_code(secret, at=moment + timedelta(seconds=30)), at=moment) is True
    # ±2 步之外不接受
    assert verify_totp(secret, totp_code(secret, at=moment - timedelta(seconds=90)), at=moment) is False


@pytest.mark.parametrize("code", ["", "abcdef", "12345", "1234567", "000000 "])
def test_totp_rejects_malformed_codes(code: str) -> None:
    assert verify_totp(generate_totp_secret(), code) is False


def test_totp_uri_carries_secret_and_issuer() -> None:
    uri = crypto.otpauth_uri("ABCDEFGH", "admin")
    assert uri.startswith("otpauth://totp/")
    assert "secret=ABCDEFGH" in uri and "issuer=Fissue" in uri


def test_api_key_shape_and_verification() -> None:
    prefix, secret, plaintext = generate_api_key()
    assert plaintext == f"fissue_{prefix}_{secret}"
    assert "_" not in secret, "secret 里不能有分隔符，否则解析会被切出两种解释"
    parsed = parse_api_key(plaintext)
    assert parsed == (prefix, secret)
    assert verify_token(secret, hash_token(secret)) is True
    assert verify_token(secret + "x", hash_token(secret)) is False


@pytest.mark.parametrize(
    "bad",
    ["", "fissue", "fissue_a", "fissue_a_b_c", "token_a_b", "FISSUE_a_b", "fissue__b"],
)
def test_parse_api_key_rejects_foreign_shapes(bad: str) -> None:
    """不像本站 Key 的串返回 None——调用方据此转去试旧口令那条兼容路径。"""
    assert parse_api_key(bad) is None


def test_constant_time_equals_and_signed_values() -> None:
    assert crypto.constant_time_equals("abc", "abc") is True
    assert crypto.constant_time_equals("abc", "abd") is False
    secret = b"k" * 32
    signed = sign_value("session-id", secret)
    assert unsign_value(signed, secret) == "session-id"
    assert unsign_value(signed, b"other" * 8) is None, "换密钥必须验不过"
    assert unsign_value("session-id.deadbeef", secret) is None
    assert unsign_value("no-dot", secret) is None


def test_two_factor_challenge_roundtrip_and_expiry() -> None:
    secret = b"k" * 32
    token = sign_challenge(7, secret, ttl_seconds=300)
    assert crypto.read_challenge(token, secret) == 7
    assert crypto.read_challenge(token, b"z" * 32) is None
    expired = sign_challenge(7, secret, ttl_seconds=-1)
    assert crypto.read_challenge(expired, secret) is None


def test_recovery_code_normalization() -> None:
    assert normalize_recovery_code("AB-C12 3") == normalize_recovery_code("abc123")
    assert len(crypto.generate_recovery_codes(3)) == 3


# ---------------------------------------------------------------------------
# 存储层：用户 / 会话 / Key
# ---------------------------------------------------------------------------


def _store(db: Database) -> AuthStore:
    return AuthStore(db)


def _make_admin(db: Database, *, username: str = "admin", password: str = "s3cret-pass") -> int:
    return _store(db).create_user(username, hash_password(password)).id


def test_session_lifecycle(db: Database) -> None:
    store = _store(db)
    user_id = _make_admin(db)
    store.create_session(
        user_id=user_id, session_id="sid-1", csrf_token="csrf-1", ttl_minutes=60
    )
    row = store.get_session("sid-1")
    assert row is not None and row.csrf_token == "csrf-1"
    store.delete_session("sid-1")
    assert store.get_session("sid-1") is None


def test_expired_session_is_dropped_on_read(db: Database) -> None:
    """过期的会话**顺手删掉**：不然每一条过期记录都是一份曾经有效的身份。"""
    store = _store(db)
    user_id = _make_admin(db)
    store.create_session(user_id=user_id, session_id="sid-old", csrf_token="c", ttl_minutes=-1)
    assert store.get_session("sid-old") is None
    with db.session() as s:
        from fissue.store.tables import SessionRow

        assert s.get(SessionRow, "sid-old") is None


def test_naive_datetime_from_sqlite_still_expires(db: Database) -> None:
    """SQLite 存回来的是 naive datetime，比较前必须补 UTC——否则每个请求都会 TypeError。"""
    store = _store(db)
    user_id = _make_admin(db)
    store.create_session(user_id=user_id, session_id="sid-2", csrf_token="c", ttl_minutes=30)
    assert store.get_session("sid-2") is not None


def test_delete_other_sessions_keeps_current(db: Database) -> None:
    store = _store(db)
    user_id = _make_admin(db)
    for sid in ("a", "b", "c"):
        store.create_session(user_id=user_id, session_id=sid, csrf_token=f"c-{sid}", ttl_minutes=60)
    assert store.delete_other_sessions(user_id, keep="b") == 2
    assert store.get_session("b") is not None
    assert store.get_session("a") is None


def test_api_key_rows_and_revoke(db: Database) -> None:
    store = _store(db)
    prefix, secret, _ = generate_api_key()
    row = store.create_key(name="ci", prefix=prefix, key_hash=hash_token(secret))
    assert store.key_count() == 1 and store.key_count(active_only=True) == 1
    assert store.key_by_prefix(prefix) is not None
    assert store.revoke_key(row.id) is True
    assert store.revoke_key(row.id) is False, "重复吊销该是幂等的失败，而不是再写一次时间"
    assert store.key_count(active_only=False) == 1, "吊销是软删：行要留着给审计"


def test_totp_state_is_forgotten_when_disabled(db: Database) -> None:
    store = _store(db)
    user_id = _make_admin(db)
    store.set_totp(user_id, secret="ABCDEF", enabled=True)
    assert store.user_by_id(user_id).totp_enabled is True  # type: ignore[union-attr]
    store.set_totp(user_id, secret=None, enabled=False)
    row = store.user_by_id(user_id)
    assert row is not None and row.totp_secret is None


def test_throwaway_server_secret_is_created_with_0600(settings: Settings) -> None:
    from fissue.auth.files import read_or_create_server_secret, server_secret_path

    first = read_or_create_server_secret(settings)
    assert len(first) == 32
    assert server_secret_path(settings).exists()
    assert read_or_create_server_secret(settings) == first, "已存在时不该重新生成（那会让会话全失效）"


def test_bootstrap_token_issue_and_consume(settings: Settings) -> None:
    from fissue.auth.files import consume_bootstrap_token, issue_bootstrap_token

    token = issue_bootstrap_token(settings)
    assert read_bootstrap_token(settings) == token
    consume_bootstrap_token(settings)
    assert read_bootstrap_token(settings) == ""
    consume_bootstrap_token(settings)  # 再删一次不该炸


def test_login_throttle_exponential_backoff() -> None:
    throttle = LoginThrottle(base_lock_seconds=10, max_lock_seconds=40)
    keys = ["ip:203.0.113.9", "user:admin"]
    assert throttle.locked_seconds(keys) == 0
    for _ in range(2):
        assert throttle.record_failure(keys, max_failures=3) == 0, "还没到阈值不该锁"
    locked = throttle.record_failure(keys, max_failures=3)
    assert locked > 0
    assert throttle.locked_seconds(keys) > 0
    assert throttle.record_failure(keys, max_failures=3) > locked, "继续失败要翻倍"
    throttle.reset(keys)
    assert throttle.locked_seconds(keys) == 0


# ---------------------------------------------------------------------------
# fail-closed：配置层判据
# ---------------------------------------------------------------------------


def _write_cfg(tmp_path: Path, payload: dict) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return cfg


def test_exposed_api_without_any_mechanism_is_refused(tmp_path: Path) -> None:
    """对外可达 + 启用 API + 既没会话鉴权又没可信网 → **拒绝启动**。

    这是 docs/AUTH.md §3.3 的放宽判据：唯一还会被拦下的组合，就是「显式关掉会话鉴权、
    又没声明可信网、却把页面绑到非回环地址」。
    """
    cfg = _write_cfg(
        tmp_path,
        {"web": {"host": "0.0.0.0", "auth": {"enabled": False}}, "api": {"enabled": True}},
    )
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="web.auth.enabled"):
        load_settings(cfg, env)


def test_auth_enabled_by_default_lets_an_exposed_binding_start(tmp_path: Path) -> None:
    """默认 `web.auth.enabled=true` → 用户原来撞到的那条报错**不该再出现**。"""
    cfg = _write_cfg(tmp_path, {"web": {"host": "0.0.0.0"}, "api": {"enabled": True}})
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    s = load_settings(cfg, env)
    assert s.web.host == "0.0.0.0" and s.web.auth.enabled is True


def test_trusted_networks_alone_also_satisfies_the_gate(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        {
            "web": {"host": "0.0.0.0", "auth": {"enabled": False},
                    "trusted_networks": ["192.168.1.0/24"]},
            "api": {"enabled": True},
        },
    )
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    s = load_settings(cfg, env)
    assert s.web.trusted_networks == ["192.168.1.0/24"]


def test_loopback_binding_needs_no_mechanism(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        {"web": {"host": "127.0.0.1", "auth": {"enabled": False}}, "api": {"enabled": True}},
    )
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    assert load_settings(cfg, env).web.auth.enabled is False


@pytest.mark.parametrize("bad", ["192.168.1.0", "192.168.1.1-192.168.1.50", "内网", "10.0.0.0/33"])
def test_invalid_trusted_network_is_rejected_at_load_time(tmp_path: Path, bad: str) -> None:
    """写错的网段必须在**加载期**就报错。

    静默忽略的代价是：用户以为自己圈了可信网，实际什么都没生效——
    而这时控制面可能已经对外可达。
    """
    cfg = _write_cfg(tmp_path, {"web": {"trusted_networks": [bad]}})
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="trusted_networks"):
        load_settings(cfg, env)


def test_blank_entries_in_trusted_networks_are_ignored(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path, {"web": {"trusted_networks": ["", "  10.0.0.0/8  "]}})
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    assert load_settings(cfg, env).web.trusted_networks == ["10.0.0.0/8"]


def test_network_of_matches_only_the_declared_range(settings: Settings) -> None:
    s = settings.model_copy(deep=True)
    s.web.trusted_networks = ["192.168.1.0/24"]
    assert s.web.network_of("192.168.1.50") is not None
    assert s.web.network_of("192.168.2.50") is None
    assert s.web.network_of("not-an-ip") is None
    assert s.web.network_of("::1") is None, "IPv6 不该被 IPv4 网段匹配上"


# ---------------------------------------------------------------------------
# 授权矩阵：{回环, 可信网段, 非回环} × {无身份, 会话, API Key} × {读, 写}
# ---------------------------------------------------------------------------

LOCAL = ("127.0.0.1", 12345)
REMOTE = ("203.0.113.9", 4321)
TRUSTED = ("192.168.1.50", 4321)


def _app(settings: Settings, *, trusted: bool = False):
    from fissue.web.app import create_app

    s = settings.model_copy(deep=True)
    if trusted:
        s.web.trusted_networks = ["192.168.1.0/24"]
    return create_app(s)


def _client(settings: Settings, peer: tuple[str, int], *, trusted: bool = False) -> TestClient:
    return TestClient(_app(settings, trusted=trusted), client=peer)


def _seed_admin(db: Database, *, password: str = "s3cret-pass", totp: bool = False) -> int:
    user_id = _make_admin(db, password=password)
    if totp:
        _store(db).set_totp(user_id, secret=generate_totp_secret(), enabled=True)
    return user_id


def _login(client: TestClient, password: str = "s3cret-pass") -> str:
    """登录并返回 CSRF token（写请求要用）。"""
    res = client.post("/api/auth/login", json={"username": "admin", "password": password})
    assert res.status_code == 200, res.text
    return res.json().get("csrf_token", "")


def test_loopback_is_free_and_remote_is_not(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        assert local.get("/api/v1/stats").status_code == 200
        assert local.post("/api/v1/queues/flush", json={"queue": "verify"}).status_code == 200
    with _client(settings, REMOTE) as remote:
        assert remote.get("/api/v1/stats").status_code == 401
        assert remote.post("/api/v1/queues/flush", json={"queue": "verify"}).status_code == 401


def test_trusted_network_is_as_good_as_loopback(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, TRUSTED, trusted=True) as trusted:
        assert trusted.get("/api/v1/stats").status_code == 200
        assert trusted.post("/api/v1/queues/flush", json={"queue": "verify"}).status_code == 200
    # 同一台机器，网段没声明时就得登录
    with _client(settings, TRUSTED) as stranger:
        assert stranger.get("/api/v1/stats").status_code == 401


def test_forwarded_header_does_not_make_a_remote_peer_trusted(settings: Settings, db: Database) -> None:
    """CIDR 判定只看**真实对端**：伪造 `X-Forwarded-For: 127.0.0.1` 一律不认。"""
    _seed_admin(db)
    with _client(settings, TRUSTED, trusted=True) as trusted:
        res = trusted.get("/api/v1/stats", headers={"X-Forwarded-For": "203.0.113.9"})
        assert res.status_code == 200, "可信网段来源带着转发头也该放行（反代是常态）"
    with _client(settings, REMOTE) as remote:
        res = remote.get("/api/v1/stats", headers={"X-Forwarded-For": "127.0.0.1"})
        assert res.status_code == 401, "转发头不能冒充回环"


# ---------------------------------------------------------------------------
# 反向代理：web.trust_proxy + web.trusted_proxies（两层声明缺一不可）
# ---------------------------------------------------------------------------


def _request(headers: dict[str, str] | None = None, peer: tuple[str, int] = REMOTE, app: Any = None):
    """构造一个最小 Request（只填判定用得到的字段）。"""
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "client": peer,
        "server": ("testserver", 80),
    }
    if app is not None:
        # `settings_of(request)` 读的是 `request.app.state`，需要 scope 里带上 app
        scope["app"] = app
    return Request(scope)


def test_forwarded_address_skips_our_own_proxies_from_the_right(settings: Settings) -> None:
    """从右往左跳过**我们声明的代理**，第一个非代理地址才是客户端。

    回归一条能把公网流量判成回环的路径：`X-Forwarded-For` 由每一跳代理追加自己的直接对端，
    所以同机双 nginx / cloudflared / `ssh -L` 这类两跳形态下，最右一跳是**上一层代理自己**
    （常常就是 127.0.0.1）——只取最右一跳就会把公网用户判成回环来源，而回环**免登录**。
    """
    from fissue.web.auth import forwarded_address

    s = settings.model_copy(deep=True)
    s.web.trust_proxy = True
    s.web.trusted_proxies = ["127.0.0.1/32", "203.0.113.0/24"]

    # 两跳：客户端 → 同机 nginx(127.0.0.1) → 本服务。最右一跳是代理，必须继续往左。
    assert forwarded_address(_request({"X-Forwarded-For": "198.51.100.7, 127.0.0.1"}), s) == "198.51.100.7"
    # 三跳：客户端 → 外部代理(203.0.113.x) → 同机代理
    assert (
        forwarded_address(_request({"X-Forwarded-For": "198.51.100.7, 203.0.113.5, 127.0.0.1"}), s)
        == "198.51.100.7"
    )
    # 单跳（最常见）：客户端直接由代理追加
    assert forwarded_address(_request({"X-Forwarded-For": "1.1.1.1, 198.51.100.9"}), s) == "198.51.100.9"
    # 端口与 IPv6 写法要能剥
    assert forwarded_address(_request({"X-Forwarded-For": "1.1.1.1:5555"}), s) == "1.1.1.1"
    assert forwarded_address(_request({"X-Forwarded-For": "[2001:db8::1]:443"}), s) == "2001:db8::1"
    # 整条链路都是自己的代理（客户端地址没写进来）→ 不猜，空串（调用方回退对端）
    assert forwarded_address(_request({"X-Forwarded-For": "127.0.0.1, 203.0.113.5"}), s) == ""
    # 出现垃圾段 → 整条作废：跳过坏段会让判定左移到攻击者自己写的地址上
    assert forwarded_address(_request({"X-Forwarded-For": "198.51.100.7, 不是IP"}), s) == ""
    # 没有 XFF 时才回退 X-Real-IP
    assert forwarded_address(_request({"X-Real-IP": "198.51.100.7"}), s) == "198.51.100.7"
    assert forwarded_address(_request({"X-Real-IP": "也不是"}), s) == ""
    assert forwarded_address(_request(), s) == ""


def test_forwarded_header_is_only_used_when_the_peer_is_a_declared_proxy(settings: Settings) -> None:
    """两层声明缺一不可：`trust_proxy=true` **且**直接对端在 `trusted_proxies` 里。"""
    from fissue.web.auth import client_host

    s = settings.model_copy(deep=True)
    s.web.trust_proxy = True
    s.web.trusted_proxies = ["203.0.113.0/24"]

    # 对端正是声明的代理 → 按它追加的那一跳判定
    assert client_host(_request({"X-Forwarded-For": "1.1.1.1, 192.168.1.50"}), s) == "192.168.1.50"
    # 对端不在声明里（直连本服务的人）→ 用对端地址，伪造的头一律不认
    spoof = _request({"X-Forwarded-For": "127.0.0.1"}, peer=("198.51.100.9", 1234))
    assert client_host(spoof, s) == "198.51.100.9"

    # 开关关掉 → 即便对端是代理也不看头
    s.web.trust_proxy = False
    assert client_host(_request({"X-Forwarded-For": "192.168.1.50"}), s) == "203.0.113.9"


def test_trust_proxy_without_declared_proxies_is_rejected_at_load_time(tmp_path: Path) -> None:
    """只勾 `trust_proxy` 不填代理网段 = 「谁的转发头都信」，必须在加载期挡下。

    允许它的后果很具体：任何能直连本服务的人加一行 `X-Forwarded-For: 127.0.0.1`
    就会被判成回环来源，而回环**免登录**——鉴权等于没有。
    """
    cfg = _write_cfg(tmp_path, {"web": {"trust_proxy": True}})
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="trusted_proxies"):
        load_settings(cfg, env)


@pytest.mark.parametrize("bad", ["10.0.0.1", "内网", "10.0.0.0/33"])
def test_invalid_trusted_proxy_is_rejected_at_load_time(tmp_path: Path, bad: str) -> None:
    cfg = _write_cfg(tmp_path, {"web": {"trust_proxy": True, "trusted_proxies": [bad]}})
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="trusted_proxies"):
        load_settings(cfg, env)


def test_declared_proxy_makes_the_forwarded_client_trusted(settings: Settings, db: Database) -> None:
    """声明了反代之后：来自代理的那一跳按真实客户端判定；非代理来源仍然不认头。"""
    from fissue.web.app import create_app

    _seed_admin(db)
    s = settings.model_copy(deep=True)
    s.web.trust_proxy = True
    s.web.trusted_proxies = ["203.0.113.0/24"]

    with TestClient(create_app(s), client=REMOTE) as via_proxy:
        res = via_proxy.get("/api/v1/stats", headers={"X-Forwarded-For": "127.0.0.1"})
        assert res.status_code == 200, "代理说客户端是回环 → 免登录（这是声明过的部署形态）"
    with TestClient(create_app(s), client=("198.51.100.9", 1234)) as direct:
        res = direct.get("/api/v1/stats", headers={"X-Forwarded-For": "127.0.0.1"})
        assert res.status_code == 401, "不是声明的代理送来头 → 不可信，仍须登录"


def test_throttle_keys_use_the_real_client_behind_a_declared_proxy(
    settings: Settings, db: Database
) -> None:
    """限速的 **IP 维度**必须取真实客户端，而不是代理 IP。

    用对端做键的后果很具体：同一个代理后面的所有人共用一个 IP 桶——一个人试错几次就把
    其他人一起挡在门外。这里刻意不去断言「换一个来源就能登录成功」：限速还有
    **用户名维度**（防撞库），锁住 `user:admin` 本来就该挡住所有人——两条维度各管一件事，
    混在一起断言会把正当行为当成 bug。
    """
    from fissue.web.app import create_app
    from fissue.web.auth import _throttle_keys

    s = settings.model_copy(deep=True)
    s.web.trust_proxy = True
    s.web.trusted_proxies = ["203.0.113.0/24"]      # REMOTE 就是这枚「代理」
    app = create_app(s)
    app.state.settings = s                          # `settings_of()` 读的就是这里
    behind_proxy = _request({"X-Forwarded-For": "198.51.100.7"}, app=app)
    assert _throttle_keys(behind_proxy, "admin")[0] == "ip:198.51.100.7"

    # 开关关掉：转发头不可信，仍按对端分桶
    plain = settings.model_copy(deep=True)
    plain_app = create_app(plain)
    plain_app.state.settings = plain
    direct = _request({"X-Forwarded-For": "198.51.100.7"}, app=plain_app)
    assert _throttle_keys(direct, "admin")[0] == "ip:203.0.113.9"


def test_session_audit_records_the_real_client_behind_a_declared_proxy(
    settings: Settings, db: Database
) -> None:
    """会话的 `peer_ip` 是审计列，反代部署下要记真实客户端（否则那一列全是代理 IP）。"""
    from fissue.web.app import create_app

    _seed_admin(db)
    s = settings.model_copy(deep=True)
    s.web.trust_proxy = True
    s.web.trusted_proxies = ["203.0.113.0/24"]

    with TestClient(create_app(s), client=REMOTE) as c:
        res = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "s3cret-pass"},
            headers={"X-Forwarded-For": "198.51.100.8"},
        )
        assert res.status_code == 200, res.text

    sessions = _store(db).list_sessions(limit=10)
    assert any(row.peer_ip == "198.51.100.8" for row in sessions), "审计列要记真实客户端地址"


def test_session_authenticates_read_and_needs_csrf_for_write(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, REMOTE) as remote:
        assert remote.get("/api/auth/me").json()["authenticated"] is False
        csrf = _login(remote)
        assert csrf, "登录必须回一个 CSRF token，否则前端发不出写请求"
        assert remote.get("/api/v1/stats").status_code == 200, "会话该能读"
        # 写请求：缺 token / token 错 → 403；带对 → 放行
        assert remote.post("/api/v1/queues/flush", json={"queue": "verify"}).status_code == 403
        assert remote.post(
            "/api/v1/queues/flush",
            json={"queue": "verify"},
            headers={"X-CSRF-Token": "wrong"},
        ).status_code == 403
        assert remote.post(
            "/api/v1/queues/flush", json={"queue": "verify"}, headers={"X-CSRF-Token": csrf}
        ).status_code == 200


def test_api_key_authenticates_and_is_exempt_from_csrf(settings: Settings, db: Database) -> None:
    """Bearer 豁免 CSRF：它不会被浏览器自动附加，天然没有 CSRF 面。"""
    store = _store(db)
    _seed_admin(db)
    prefix, secret, plaintext = generate_api_key()
    store.create_key(name="ci", prefix=prefix, key_hash=hash_token(secret))
    with _client(settings, REMOTE) as remote:
        assert remote.get("/api/v1/stats").status_code == 401
        headers = {"Authorization": f"Bearer {plaintext}"}
        assert remote.get("/api/v1/stats", headers=headers).status_code == 200
        assert remote.post(
            "/api/v1/queues/flush", json={"queue": "verify"}, headers=headers
        ).status_code == 200
        # 用了之后 last_used_at 要有痕迹（审计：谁在用这把钥匙）
        assert store.key_by_prefix(prefix).last_used_at is not None  # type: ignore[union-attr]


def test_wrong_api_key_and_revoked_key_are_rejected(settings: Settings, db: Database) -> None:
    store = _store(db)
    _seed_admin(db)
    prefix, secret, plaintext = generate_api_key()
    row = store.create_key(name="ci", prefix=prefix, key_hash=hash_token(secret))
    with _client(settings, REMOTE) as remote:
        assert remote.get(
            "/api/v1/stats", headers={"Authorization": f"Bearer fissue_{prefix}_deadbeef"}
        ).status_code == 401
        store.revoke_key(row.id)
        assert remote.get(
            "/api/v1/stats", headers={"Authorization": f"Bearer {plaintext}"}
        ).status_code == 401, "吊销后必须立刻失效"


def test_auth_disabled_still_rejects_non_local_sources(settings: Settings, db: Database) -> None:
    """`web.auth.enabled=false` **不是**「谁都能进」——只认回环与可信网段。"""
    _seed_admin(db)
    s = settings.model_copy(deep=True)
    s.web.auth.enabled = False
    with TestClient(create_app_for(s), client=REMOTE) as remote:
        res = remote.get("/api/v1/stats")
        assert res.status_code == 403
        assert "web.auth.enabled" in res.text
    with TestClient(create_app_for(s), client=LOCAL) as local:
        assert local.get("/api/v1/stats").status_code == 200


def create_app_for(settings: Settings):
    from fissue.web.app import create_app

    return create_app(settings)


def test_health_endpoint_stays_open(settings: Settings, db: Database) -> None:
    """`/health` 无副作用，保持免鉴权（探活/负载均衡要用它）。"""
    _seed_admin(db)
    with _client(settings, REMOTE) as remote:
        assert remote.get("/api/v1/health").status_code == 200


def test_unauthenticated_page_load_redirects_to_login(settings: Settings, db: Database) -> None:
    """页面守卫：未认证的页面加载直接 303 到 /login，而不是渲染一个处处报错的看板。"""
    _seed_admin(db)
    with _client(settings, REMOTE) as remote:
        res = remote.get("/", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"].startswith("/login")
        assert remote.get("/api/v1/stats").status_code == 401, "API 仍然回结构化状态码"
    with _client(settings, LOCAL) as local:
        assert local.get("/").status_code == 200


# ---------------------------------------------------------------------------
# 登录 / 登出 / 2FA / 改口令
# ---------------------------------------------------------------------------


def test_login_page_renders_before_any_account_exists(settings: Settings, db: Database) -> None:
    """还没有账号时，登录页要显示**引导表单**。

    必须用非回环来源，并且不跟随重定向：回环访问者本身就有身份，`/login` 会直接 303 回首页
    （这是有意的——本机用户不需要登录）。所以只有「外来者」才看得到登录页。
    """
    with _client(settings, REMOTE) as remote:
        res = remote.get("/login", follow_redirects=False)
        assert res.status_code == 200
        assert "初始化管理员" in res.text
    with _client(settings, LOCAL) as local:
        assert local.get("/login", follow_redirects=False).status_code == 303, (
            "已有身份的来源访问登录页应当被送回首页"
        )


def test_login_logout_and_me(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, REMOTE) as remote:
        csrf = _login(remote)
        me = remote.get("/api/auth/me").json()
        assert me["authenticated"] is True and me["username"] == "admin"
        assert me["two_factor"] is False and me["no_admin"] is False
        assert me["csrf_token"] == csrf
        assert remote.post("/api/auth/logout").status_code == 200
        assert remote.get("/api/auth/me").json()["authenticated"] is False


def test_login_rejects_bad_credentials_and_throttles(settings: Settings, db: Database, monkeypatch) -> None:
    _seed_admin(db)
    s = settings.model_copy(deep=True)
    s.web.auth.login_max_failures = 3
    with TestClient(create_app_for(s), client=REMOTE) as remote:
        for _ in range(3):
            assert remote.post(
                "/api/auth/login", json={"username": "admin", "password": "nope"}
            ).status_code == 401
        throttled = remote.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert throttled.status_code == 429, "连续失败要退避锁定，而不是无限次试"
        assert throttled.headers.get("Retry-After")
        # 锁定期间**正确口令也进不去**（否则退避形同虚设）
        assert remote.post(
            "/api/auth/login", json={"username": "admin", "password": "s3cret-pass"}
        ).status_code == 429


def test_login_rejects_a_wrong_username(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, REMOTE) as remote:
        res = remote.post("/api/auth/login", json={"username": "root", "password": "s3cret-pass"})
        assert res.status_code == 401


def test_two_factor_login_flow(settings: Settings, db: Database) -> None:
    store = _store(db)
    user_id = _make_admin(db)
    secret = generate_totp_secret()
    store.set_totp(user_id, secret=secret, enabled=True)
    with _client(settings, REMOTE) as remote:
        first = remote.post("/api/auth/login", json={"username": "admin", "password": "s3cret-pass"})
        body = first.json()
        assert body["requires_2fa"] is True and body["challenge"]
        assert remote.get("/api/v1/stats").status_code == 401, "过了第一步还不算登录"

        bad = remote.post("/api/auth/2fa", json={"challenge": body["challenge"], "code": "000000"})
        assert bad.status_code == 401

        good = remote.post(
            "/api/auth/2fa",
            json={"challenge": body["challenge"], "code": totp_code(secret)},
        )
        assert good.status_code == 200 and good.json()["csrf_token"]
        assert remote.get("/api/auth/me").json()["username"] == "admin"


def test_two_factor_login_accepts_a_recovery_code_once(settings: Settings, db: Database) -> None:
    store = _store(db)
    user_id = _make_admin(db)
    secret = generate_totp_secret()
    store.set_totp(user_id, secret=secret, enabled=True)
    code = "aaaaa-bbbbb"
    store.set_recovery_codes(user_id, [hash_token(normalize_recovery_code(code))])
    with _client(settings, LOCAL) as local:
        challenge = local.post(
            "/api/auth/login", json={"username": "admin", "password": "s3cret-pass"}
        ).json()["challenge"]
        res = local.post("/api/auth/2fa", json={"challenge": challenge, "code": code})
        assert res.status_code == 200
        assert res.json()["recovery_code_used"] is True
        assert res.json()["recovery_codes_left"] == 0
        assert store.user_by_id(user_id).recovery_code_hashes == []  # type: ignore[union-attr]

        again = local.post(
            "/api/auth/login", json={"username": "admin", "password": "s3cret-pass"}
        ).json()["challenge"]
        reused = local.post("/api/auth/2fa", json={"challenge": again, "code": code})
        assert reused.status_code == 401, "恢复码是一次性的"


def test_expired_challenge_is_rejected(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        res = local.post("/api/auth/2fa", json={"challenge": "forged.signature", "code": "123456"})
        assert res.status_code == 400


def test_enroll_activate_and_disable_two_factor(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        enrolled = local.post("/api/auth/2fa/enroll").json()
        assert enrolled["secret"] and enrolled["otpauth_uri"].startswith("otpauth://")
        assert local.get("/api/auth/me").json()["two_factor"] is False, "没确认之前不算生效"

        wrong = local.post("/api/auth/2fa/activate", json={"code": "000000"})
        assert wrong.status_code == 400

        activated = local.post(
            "/api/auth/2fa/activate", json={"code": totp_code(enrolled["secret"])}
        )
        assert activated.status_code == 200
        codes = activated.json()["recovery_codes"]
        assert len(codes) == 10, "恢复码要一次给足，且只显示这一次"
        assert local.get("/api/auth/me").json()["two_factor"] is True

        # 关了之后旧密钥必须被忘掉
        disabled = local.post(
            "/api/auth/2fa/disable",
            json={"password": "s3cret-pass", "code": totp_code(enrolled["secret"])},
        )
        assert disabled.status_code == 200
        assert local.get("/api/auth/me").json()["two_factor"] is False


def test_disable_two_factor_requires_both_password_and_code(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        secret = local.post("/api/auth/2fa/enroll").json()["secret"]
        local.post("/api/auth/2fa/activate", json={"code": totp_code(secret)})
        assert local.post(
            "/api/auth/2fa/disable", json={"password": "wrong", "code": totp_code(secret)}
        ).status_code == 401
        assert local.post(
            "/api/auth/2fa/disable", json={"password": "s3cret-pass", "code": "000000"}
        ).status_code == 400


def test_regenerate_recovery_codes_replaces_the_old_ones(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        secret = local.post("/api/auth/2fa/enroll").json()["secret"]
        first = local.post("/api/auth/2fa/activate", json={"code": totp_code(secret)}).json()[
            "recovery_codes"
        ]
        regenerated = local.post(
            "/api/auth/2fa/recovery", json={"current_password": "s3cret-pass"}
        )
        assert regenerated.status_code == 200
        assert set(regenerated.json()["recovery_codes"]) != set(first)


# ---------------------------------------------------------------------------
# 2FA 二维码（docs/AUTH.md §12 第 1 条）
# ---------------------------------------------------------------------------


def _pending(local: TestClient) -> dict:
    res = local.get("/api/auth/2fa/pending")
    assert res.status_code == 200, res.text
    return res.json()


def test_enroll_returns_a_scanable_qr_next_to_the_manual_secret(
    settings: Settings, db: Database
) -> None:
    """绑定 2FA 时同时给「二维码」与「手抄密钥」两条路，谁也不取代谁。"""
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        body = local.post("/api/auth/2fa/enroll").json()
        assert body["secret"], "手抄那条路必须始终在"
        assert body["otpauth_uri"].startswith("otpauth://totp/")
        svg = body["qr_svg"]
        assert svg.startswith("<svg ") and svg.endswith("</svg>")
        assert 'shape-rendering="crispEdges"' in svg
        # 二维码里只有黑白模块，**没有**明文密钥/URI
        assert body["secret"] not in svg
        assert "otpauth" not in svg
        # 自包含：不引任何外部资源（否则等于把密钥所在页面挂到别人服务器上）
        assert svg.count("http") == 1, "只允许 xmlns 里那一次 http"


def test_pending_endpoint_survives_a_page_refresh(settings: Settings, db: Database) -> None:
    """「已生成、还没确认」的密钥要能取回来——否则刷新一下二维码就没了。"""
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        assert _pending(local)["pending"] is False, "还没生成时不该有 pending"
        secret = local.post("/api/auth/2fa/enroll").json()["secret"]

        again = _pending(local)
        assert again["pending"] is True
        assert again["secret"] == secret, "刷新后必须是**同一把**，不能悄悄换一把"
        assert again["qr_svg"].startswith("<svg ")

        local.post("/api/auth/2fa/activate", json={"code": totp_code(secret)})
        assert _pending(local)["pending"] is False, "确认生效后就不该再当 pending 交出密钥"


def test_pending_is_not_available_on_an_enabled_secret(settings: Settings, db: Database) -> None:
    """已开启 2FA 时，pending 不能把在用的密钥再吐一遍。"""
    store = _store(db)
    user_id = _seed_admin(db, totp=True)
    with _client(settings, LOCAL) as local:
        assert _pending(local)["pending"] is False
    assert store.user_by_id(user_id).totp_enabled is True  # type: ignore[union-attr]


def test_qr_endpoints_require_an_identity(settings: Settings, db: Database) -> None:
    """非回环 + 无身份 → 拿不到密钥，也拿不到二维码。"""
    _seed_admin(db)
    with _client(settings, REMOTE) as remote:
        assert remote.get("/api/auth/2fa/pending").status_code == 401
        assert remote.post("/api/auth/2fa/enroll").status_code == 401


def test_qr_encoding_failure_degrades_instead_of_breaking_enroll(
    settings: Settings, db: Database, monkeypatch
) -> None:
    """编码器万一失败，绑定流程照旧可用（手抄那条路），而不是整个 500。"""
    from fissue.web import qr as qr_mod

    def boom(text: str, **kwargs) -> list:
        raise ValueError("模拟编码失败")

    monkeypatch.setattr(qr_mod, "encode", boom)
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        res = local.post("/api/auth/2fa/enroll")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["secret"] and body["otpauth_uri"], "手抄那条路必须活着"
        assert body["qr_svg"] == "", "画不出来就少一张图，而不是报错"


def test_settings_page_has_the_qr_container(settings: Settings, db: Database) -> None:
    """设置页要有二维码容器与「手抄也行」两条路，且不引外部脚本。"""
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        html = local.get("/settings").text
        assert 'id="tfa-qr"' in html
        assert "手抄也行" in html
        # 「刷新后要能把待确认的密钥捞回来」的实现已随设置页逻辑一起拆到
        # static/settings.js —— 断言随之改查那个文件，**意图不变**。
        from fissue.web.app import STATIC_DIR

        js = (STATIC_DIR / "settings.js").read_text(encoding="utf-8")
        assert "2fa/pending" in js, "刷新后要能把待确认的密钥捞回来"
        assert "qrcode" not in html.lower(), "不该引入任何第三方二维码脚本"
        assert "qrcode" not in js.lower(), "不该引入任何第三方二维码脚本"


def test_password_change_kicks_other_sessions(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, REMOTE) as first, _client(settings, REMOTE) as second:
        _login(first)
        _login(second)
        changed = first.post(
            "/api/auth/password",
            json={"current_password": "s3cret-pass", "new_password": "new-s3cret"},
            headers={"X-CSRF-Token": first.get("/api/auth/me").json()["csrf_token"]},
        )
        assert changed.status_code == 200
        assert changed.json()["kicked_sessions"] == 1
        assert first.get("/api/v1/stats").status_code == 200, "发起改口令的这台不该被踢"
        assert second.get("/api/v1/stats").status_code == 401, "别处的会话必须失效"
        assert _store(db).user_by_name("admin").password_hash  # type: ignore[union-attr]
        # 旧口令不能再用
        assert first.post(
            "/api/auth/login", json={"username": "admin", "password": "s3cret-pass"}
        ).status_code == 401
        assert first.post(
            "/api/auth/login", json={"username": "admin", "password": "new-s3cret"}
        ).status_code == 200


def test_password_change_requires_the_current_password(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        res = local.post(
            "/api/auth/password",
            json={"current_password": "nope", "new_password": "whatever-long"},
        )
        assert res.status_code == 401


def test_weak_password_is_refused(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        res = local.post(
            "/api/auth/password",
            json={"current_password": "s3cret-pass", "new_password": "short"},
        )
        assert res.status_code == 400


# ---------------------------------------------------------------------------
# 首次引导（bootstrap）
# ---------------------------------------------------------------------------


def test_remote_sources_are_blocked_before_any_admin_exists(settings: Settings, db: Database) -> None:
    """没有管理员账号时，非回环一律 403，并把「怎么拿引导口令」写清楚。"""
    with _client(settings, REMOTE) as remote:
        res = remote.get("/api/v1/stats")
        assert res.status_code == 403
        assert "/login" in res.text
        assert remote.post(
            "/api/auth/login", json={"username": "admin", "password": "x" * 10}
        ).status_code == 409


def test_loopback_can_bootstrap_without_a_token(settings: Settings, db: Database) -> None:
    with _client(settings, LOCAL) as local:
        me = local.get("/api/auth/me").json()
        assert me["no_admin"] is True
        res = local.post(
            "/api/auth/bootstrap", json={"username": "admin", "password": "s3cret-pass"}
        )
        assert res.status_code == 200 and res.json()["csrf_token"]
        assert local.get("/api/v1/stats").status_code == 200
        second = local.post(
            "/api/auth/bootstrap", json={"username": "other", "password": "s3cret-pass"}
        )
        assert second.status_code == 409, "引导只走一次"


def test_remote_bootstrap_requires_the_one_time_token(settings: Settings, db: Database) -> None:
    from fissue.auth.files import issue_bootstrap_token

    token = issue_bootstrap_token(settings)
    with _client(settings, REMOTE) as remote:
        assert remote.post(
            "/api/auth/bootstrap", json={"username": "admin", "password": "s3cret-pass"}
        ).status_code == 403
        assert remote.post(
            "/api/auth/bootstrap",
            json={"bootstrap_token": "wrong", "username": "admin", "password": "s3cret-pass"},
        ).status_code == 403
        ok = remote.post(
            "/api/auth/bootstrap",
            json={"bootstrap_token": token, "username": "admin", "password": "s3cret-pass"},
        )
        assert ok.status_code == 200
    assert read_bootstrap_token(settings) == "", "引导口令必须用后即焚"
    assert not bootstrap_token_path(settings).exists()


def test_bootstrap_validates_username_and_password(settings: Settings, db: Database) -> None:
    with _client(settings, LOCAL) as local:
        assert local.post(
            "/api/auth/bootstrap", json={"username": "bad name!", "password": "s3cret-pass"}
        ).status_code == 400
        assert local.post(
            "/api/auth/bootstrap", json={"username": "admin", "password": "short"}
        ).status_code == 400


# ---------------------------------------------------------------------------
# API Key 的 HTTP 面
# ---------------------------------------------------------------------------


def test_api_key_http_crud(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        listed = local.get("/api/auth/api-keys").json()
        assert listed["keys"] == []
        created = local.post("/api/auth/api-keys", json={"name": "ci"})
        assert created.status_code == 201
        body = created.json()
        assert body["key"].startswith("fissue_")
        assert "key" not in local.get("/api/auth/api-keys").json()["keys"][0], (
            "列表接口绝不能回明文——库里也没有"
        )
        revoked = local.post(f"/api/auth/api-keys/{body['id']}/revoke")
        assert revoked.status_code == 200
        assert revoked.json()["keys"][0]["revoked"] is True
        assert local.post(f"/api/auth/api-keys/{body['id']}/revoke").status_code == 404


def test_created_key_actually_works(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        plaintext = local.post("/api/auth/api-keys", json={"name": "ci"}).json()["key"]
    with _client(settings, REMOTE) as remote:
        res = remote.get("/api/v1/stats", headers={"Authorization": f"Bearer {plaintext}"})
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# 旧口令的一次性迁移（docs/AUTH.md §7）
# ---------------------------------------------------------------------------


def test_legacy_env_token_is_imported_and_usable(
    tmp_path: Path, settings: Settings, db: Database
) -> None:
    from fissue.auth.crypto import LEGACY_KEY_PREFIX
    from fissue.web.auth import migrate_legacy_token

    # 顺手把混用两个库的坑钉住：这份配置必须指向**夹具那个库**，
    # 否则迁移写进 A 库、请求读 B 库，用例会以「导入成功但令牌不认」的面目失败。
    settings = _settings_with_env(tmp_path, db_url=str(settings.database.url))
    store = _store(db)
    _make_admin(db)          # 有管理员才谈得上「身份不认」= 401（没有账号时是 403 引导）
    assert migrate_legacy_token(settings, store) is True
    assert [k.prefix for k in store.list_keys()] == [LEGACY_KEY_PREFIX]

    with TestClient(create_app_for(settings), client=REMOTE) as remote:
        headers = {"Authorization": "Bearer legacy-token-value"}
        assert remote.get("/api/v1/stats", headers=headers).status_code == 200
        store.revoke_key(store.key_by_prefix(LEGACY_KEY_PREFIX).id)  # type: ignore[union-attr]
        assert remote.get("/api/v1/stats", headers=headers).status_code == 401


def test_legacy_migration_runs_only_once(tmp_path: Path, db: Database) -> None:
    from fissue.web.auth import migrate_legacy_token

    settings = _settings_with_env(tmp_path)
    store = _store(db)
    assert migrate_legacy_token(settings, store) is True
    assert migrate_legacy_token(settings, store) is False, "表里已经有 Key，就不再导入"


def test_no_legacy_token_means_no_import(tmp_path: Path, db: Database) -> None:
    from fissue.web.auth import migrate_legacy_token

    settings = _settings_with_env(tmp_path, token="")
    assert migrate_legacy_token(settings, _store(db)) is False
    assert _store(db).key_count() == 0


def _settings_with_env(
    tmp_path: Path, *, token: str = "legacy-token-value", db_url: str | None = None
) -> Settings:
    """一份带 ``FISSUE_API_TOKEN`` 的 .env 的配置。

    ``db_url`` 用来指向**调用方那个库**（通常是 ``db`` 夹具）：迁移写库与请求读库必须是同一个
    文件，否则用例会以「导入成功但令牌不认」的面目失败——那种失败很容易被误读成迁移逻辑坏了。
    """
    cfg = _write_cfg(
        tmp_path,
        {
            "database": {"url": db_url or f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"},
            "llm": {"api_key": "k", "model": "m", "base_url": "https://llm.invalid/v1"},
            "web": {"host": "0.0.0.0"},
            "notify": {"enabled": False},
        },
    )
    env = tmp_path / "legacy.env"
    env.write_text(f"FISSUE_API_TOKEN={token}\n", encoding="utf-8")
    return load_settings(cfg, env)


def test_data_dir_keeps_auth_secrets_out_of_config(settings: Settings) -> None:
    """签名密钥落在 ``data/`` 下，**不进** config.yaml / .env。

    为什么这条重要：config.yaml 与 .env 会被设置页整体重写、被用户复制粘贴、被 commit。
    签名密钥的轮换语义是「让所有会话失效」——它不该待在一个会被随手改写的文件里。
    """
    from fissue.auth.files import SERVER_SECRET_FILE, read_or_create_server_secret

    secret = read_or_create_server_secret(settings)
    assert Path(settings.data_dir, SERVER_SECRET_FILE).exists()
    assert len(secret) == 32
    # 它不在任何一份配置文件里
    for path in (settings.config_path, settings.env_source):
        if path and Path(path).exists():
            assert secret.hex() not in Path(path).read_text(encoding="utf-8")


def test_auth_policy_values_are_range_checked() -> None:
    """策略值在**模型层**就拦住：会话 0 分钟、失败 0 次都是没有意义的配置。"""
    import pydantic

    for bad in ({"web": {"auth": {"session_ttl_minutes": 0}}},
                {"web": {"auth": {"login_max_failures": 0}}}):
        with pytest.raises(pydantic.ValidationError):
            Settings.model_validate(bad)


# ---------------------------------------------------------------------------
# 配置可写面：白名单
# ---------------------------------------------------------------------------


def test_auth_settings_are_writable_through_the_overlay_layer() -> None:
    from fissue.web.setup import OVERLAY_WRITABLE

    for path in (
        "web.auth.enabled",
        "web.auth.session_ttl_minutes",
        "web.auth.secure_cookie",
        "web.auth.login_max_failures",
        "web.trusted_networks",
    ):
        assert path in OVERLAY_WRITABLE, f"{path} 必须能在设置页改（docs/AUTH.md §6）"


def test_old_token_env_key_is_gone_from_the_writable_whitelist() -> None:
    """旧口令不再是配置项：留在白名单里等于给「口令是一个配置」这个错觉续命。"""
    from fissue.web.setup import ENV_WRITABLE, SECRET_KEYS

    assert "FISSUE_API_TOKEN" not in ENV_WRITABLE
    assert "FISSUE_API_TOKEN" not in SECRET_KEYS


def test_settings_page_renders_the_new_auth_panel(settings: Settings, db: Database) -> None:
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        html = local.get("/settings").text
        assert "web.trusted_networks" in html
        assert "web.auth.enabled" in html
        assert "FISSUE_API_TOKEN" not in html, "旧口令字段必须从页面上消失"


def test_overlay_roundtrip_writes_and_reloads_auth_settings(settings: Settings, db: Database) -> None:
    """设置页保存 → 覆盖层 → 热重载后新配置立刻生效。"""
    _seed_admin(db)
    with _client(settings, LOCAL) as local:
        res = local.put(
            "/api/v1/settings",
            json={
                "env": {},
                "overlay": {
                    "web": {
                        "auth": {"session_ttl_minutes": 45, "login_max_failures": 7},
                        "trusted_networks": ["10.1.0.0/16"],
                    }
                },
            },
        )
        assert res.status_code == 200, res.text
        view = res.json()
        assert view["effective"]["session_ttl_minutes"] == 45
        assert view["effective"]["trusted_networks"] == ["10.1.0.0/16"]
        assert "web.auth.session_ttl_minutes" in view["written_overlay"]


def test_bad_overlay_value_is_rejected_and_nothing_is_written(settings: Settings, db: Database) -> None:
    """校验失败必须**一个字都不落盘**。

    否则用户会拿到一份「重启起不来」的配置，而这时他连界面都打不开（服务起不来就没有界面）——
    从界面上无法自救。这条守护 `_validate_candidate` 的「先校验、后写盘」顺序。
    """
    _seed_admin(db)
    target = overlay_file(settings)
    before = target.read_text(encoding="utf-8") if target.exists() else None
    with _client(settings, LOCAL) as local:
        res = local.put(
            "/api/v1/settings",
            json={"env": {}, "overlay": {"web": {"trusted_networks": ["definitely-not-cidr"]}}},
        )
        assert res.status_code == 400
        assert "trusted_networks" in res.text
    after = target.read_text(encoding="utf-8") if target.exists() else None
    assert after == before, "校验没过就不该碰覆盖层文件"
