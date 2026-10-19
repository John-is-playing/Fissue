"""鉴权数据的读写：``auth_users`` / ``sessions`` / ``api_keys`` 三张表。

为什么不复用 ``Repository``：那个类的职责是「条目、评测、队列」这条业务流水线，
已经有 1700 行。鉴权是**另一个世界**——它要能在「库里还没有任何业务数据」时独立工作
（首次引导就是这种状态），也需要能被 CLI（`fissue auth reset`）在**不构造整条流水线**的
情况下直接调用。塞进 Repository 只会让两边都更难改。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from ..store.db import Database
from ..store.tables import ApiKeyRow, AuthUserRow, SessionRow

#: 会话最后活跃时间的刷新间隔（秒）。
#:
#: 每个请求都 UPDATE 一行 `last_seen_at` 会让只读看板变成写入器（SQLite 下还会与
#: 真正的写操作抢锁）。按分钟级刷新足够回答「谁在用」这个问题。
TOUCH_INTERVAL_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(ts: datetime | None) -> datetime | None:
    """把库里的 datetime 补成带 UTC 时区的。

    SQLite 不存时区，读回来是 naive 的；拿它直接和 ``datetime.now(timezone.utc)``
    比较会抛 ``TypeError: can't compare offset-naive and offset-aware datetimes``。
    这个比较在**每个请求**上都跑，所以归一化必须在一处做完，而不是靠调用方自觉。
    """
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class AuthStore:
    """鉴权三张表的读写。每次调用自建 session（短事务，够用且没有长连接状态）。"""

    def __init__(self, database: Database) -> None:
        self.db = database

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------

    def user_count(self) -> int:
        with self.db.session() as session:
            return int(session.scalar(select(func.count(AuthUserRow.id))) or 0)

    def admin(self) -> AuthUserRow | None:
        """当前的管理员账号。

        按设计是**单管理员**（docs/AUTH.md §12 决策 3），所以「第一个创建的用户」
        就是管理员；表结构留了多用户的余地。
        """
        with self.db.session() as session:
            return session.scalars(
                select(AuthUserRow).order_by(AuthUserRow.id).limit(1)
            ).first()

    def user_by_name(self, username: str) -> AuthUserRow | None:
        with self.db.session() as session:
            return session.scalars(
                select(AuthUserRow).where(AuthUserRow.username == username)
            ).first()

    def user_by_id(self, user_id: int) -> AuthUserRow | None:
        with self.db.session() as session:
            return session.get(AuthUserRow, user_id)

    def create_user(self, username: str, password_hash: str) -> AuthUserRow:
        with self.db.session() as session:
            row = AuthUserRow(username=username, password_hash=password_hash)
            session.add(row)
            session.flush()
            return row

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self.db.session() as session:
            row = session.get(AuthUserRow, user_id)
            if row is None:
                return
            row.password_hash = password_hash
            row.updated_at = _utcnow()

    def set_totp(self, user_id: int, *, secret: str | None, enabled: bool) -> None:
        """设置/清空 TOTP 密钥。

        ``secret=None`` = 关闭并**忘掉密钥**：留着旧密钥等于「关了还能用旧码开」。
        """
        with self.db.session() as session:
            row = session.get(AuthUserRow, user_id)
            if row is None:
                return
            row.totp_secret = secret
            row.totp_enabled = enabled
            row.updated_at = _utcnow()

    def set_recovery_codes(self, user_id: int, hashes: list[str]) -> None:
        with self.db.session() as session:
            row = session.get(AuthUserRow, user_id)
            if row is None:
                return
            row.recovery_code_hashes = list(hashes)
            row.updated_at = _utcnow()

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def create_session(
        self,
        *,
        user_id: int,
        session_id: str,
        csrf_token: str,
        ttl_minutes: int,
        user_agent: str = "",
        peer_ip: str = "",
    ) -> datetime:
        expires_at = _utcnow() + timedelta(minutes=ttl_minutes)
        with self.db.session() as session:
            session.add(
                SessionRow(
                    id=session_id,
                    user_id=user_id,
                    csrf_token=csrf_token,
                    expires_at=expires_at,
                    last_seen_at=_utcnow(),
                    # 截断：User-Agent 可以任意长，而这两列只是给「谁在用」看的。
                    user_agent=user_agent[:255],
                    peer_ip=peer_ip[:64],
                )
            )
        return expires_at

    def get_session(self, session_id: str) -> SessionRow | None:
        """取会话；**已过期的顺手删掉并返回 None**。

        过期即删而不是只判空：不然「删了 cookie 但数据表里留一堆死会话」会一直累积，
        而它们每一行都是一份**曾经有效的身份**。
        """
        with self.db.session() as session:
            row = session.get(SessionRow, session_id)
            if row is None:
                return None
            expires = as_utc(row.expires_at)
            if expires is None or expires <= _utcnow():
                session.delete(row)
                return None
            return row

    def touch_session(self, session_id: str, *, interval_seconds: int = TOUCH_INTERVAL_SECONDS) -> None:
        """刷新 ``last_seen_at``（按间隔节流，见 ``TOUCH_INTERVAL_SECONDS``）。"""
        now = _utcnow()
        with self.db.session() as session:
            row = session.get(SessionRow, session_id)
            if row is None:
                return
            last = as_utc(row.last_seen_at)
            if last is not None and (now - last).total_seconds() < interval_seconds:
                return
            row.last_seen_at = now

    def delete_session(self, session_id: str) -> None:
        with self.db.session() as session:
            session.execute(delete(SessionRow).where(SessionRow.id == session_id))

    def delete_sessions_for_user(self, user_id: int) -> int:
        """踢下线某个用户的全部会话（关 2FA 后用）。"""
        with self.db.session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(delete(SessionRow).where(SessionRow.user_id == user_id)),
            )
        return int(result.rowcount or 0)

    def delete_other_sessions(self, user_id: int, *, keep: str = "") -> int:
        """踢掉该用户除 ``keep`` 之外的会话。

        改口令时用：发起改口令的那台浏览器**不该**被自己踢掉（否则用户看到的
        「改成功了」紧接着就是「请登录」），而别处那些用旧口令登进去的会话必须失效——
        不然「口令泄露后改口令」这个动作几乎不起作用。
        """
        with self.db.session() as session:
            stmt = delete(SessionRow).where(SessionRow.user_id == user_id)
            if keep:
                stmt = stmt.where(SessionRow.id != keep)
            result = cast(CursorResult[Any], session.execute(stmt))
        return int(result.rowcount or 0)

    def purge_expired_sessions(self) -> int:
        with self.db.session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(delete(SessionRow).where(SessionRow.expires_at <= _utcnow())),
            )
        return int(result.rowcount or 0)

    def list_sessions(self, *, limit: int = 50) -> list[SessionRow]:
        with self.db.session() as session:
            return list(
                session.scalars(
                    select(SessionRow).order_by(SessionRow.last_seen_at.desc()).limit(limit)
                ).all()
            )

    # ------------------------------------------------------------------
    # API Key
    # ------------------------------------------------------------------

    def list_keys(self) -> list[ApiKeyRow]:
        """列全部 Key（含已吊销）。

        含已吊销是有意的：页面上要能看见「这把已经废了」，否则用户会以为它还在生效。
        返回的行里**只有 prefix 与哈希**，绝无明文——明文在创建那一刻之后就没了。
        """
        with self.db.session() as session:
            return list(session.scalars(select(ApiKeyRow).order_by(ApiKeyRow.id)).all())

    def key_count(self, *, active_only: bool = False) -> int:
        stmt = select(func.count(ApiKeyRow.id))
        if active_only:
            stmt = stmt.where(ApiKeyRow.revoked_at.is_(None))
        with self.db.session() as session:
            return int(session.scalar(stmt) or 0)

    def key_by_prefix(self, prefix: str) -> ApiKeyRow | None:
        with self.db.session() as session:
            return session.scalars(
                select(ApiKeyRow).where(ApiKeyRow.prefix == prefix)
            ).first()

    def create_key(self, *, name: str, prefix: str, key_hash: str) -> ApiKeyRow:
        with self.db.session() as session:
            row = ApiKeyRow(name=name, prefix=prefix, key_hash=key_hash)
            session.add(row)
            session.flush()
            return row

    def revoke_key(self, key_id: int) -> bool:
        with self.db.session() as session:
            row = session.get(ApiKeyRow, key_id)
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = _utcnow()
            return True

    def mark_key_used(self, key_id: int, *, interval_seconds: int = TOUCH_INTERVAL_SECONDS) -> None:
        now = _utcnow()
        with self.db.session() as session:
            row = session.get(ApiKeyRow, key_id)
            if row is None:
                return
            last = as_utc(row.last_used_at)
            if last is not None and (now - last).total_seconds() < interval_seconds:
                return
            row.last_used_at = now
