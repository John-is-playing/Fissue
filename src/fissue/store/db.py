"""数据库连接与 schema 管理。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import DatabaseConfig, Settings
from ..errors import FissueError
from ..logging_setup import get_logger
from .tables import Base

log = get_logger(__name__)


class Database:
    """封装 Engine 与 Session 生命周期。

    支持 PostgreSQL（生产）与 SQLite（本地/测试）两套 URL。
    """

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 5, max_overflow: int = 10) -> None:
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        # 「本进程内表已确保存在」的标记，供 ensure_schema() 做零开销短路
        self._schema_ready = False
        kwargs: dict[str, Any] = {"echo": echo, "future": True, "pool_pre_ping": True}
        if not self.is_sqlite:
            kwargs.update(pool_size=pool_size, max_overflow=max_overflow, pool_recycle=1800)
        else:
            kwargs["connect_args"] = {"check_same_thread": False}
        try:
            self.engine: Engine = create_engine(url, **kwargs)
        except Exception as exc:  # pragma: no cover
            raise FissueError(f"无法创建数据库引擎（{url}）：{exc}") from exc
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, class_=Session
        )

    # -- schema -----------------------------------------------------------

    def create_all(self) -> None:
        """建表（幂等）。

        ``checkfirst=True``（默认）会对每张表先做一次存在性检查，
        所以重复调用是安全的，但会在一个进程里反复产生额外的查询。
        高频路径请改用 :meth:`ensure_schema`。
        """
        Base.metadata.create_all(self.engine)
        self._schema_ready = True
        log.info("数据库表已就绪：%s", self._safe_url())

    def ensure_schema(self) -> None:
        """确保表存在；**每个进程只真正执行一次**，之后是零开销的空操作。

        为什么要它：``status`` / ``report`` / ``export`` 这类只读命令也需要表存在。
        如果它们各自无条件调 ``create_all()``，在一个长驻进程（如 Web/常驻服务）
        里每来一次请求就白跑一轮 DDL 存在性检查；而不调又会像现在这样在全新库上
        直接 ``no such table`` 崩掉。用实例级标记把「首次建表」与「后续空转」分开。
        """
        if self._schema_ready:
            return
        Base.metadata.create_all(self.engine)
        self._schema_ready = True
        log.debug("数据库表已确保存在：%s", self._safe_url())

    def has_schema(self) -> bool:
        """表是否已存在（用于给用户提示「请先执行 fissue db init」）。"""
        from sqlalchemy import inspect

        try:
            names = set(inspect(self.engine).get_table_names())
        except Exception as exc:  # pragma: no cover
            log.debug("检查表结构失败：%s", exc)
            return False
        return "items" in names and "llm_usage" in names

    def drop_all(self) -> None:
        """删表（危险，仅测试/重置用）。"""
        Base.metadata.drop_all(self.engine)
        log.warning("已删除所有表：%s", self._safe_url())

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.error("数据库连接失败：%s", exc)
            return False

    # -- session ----------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Session]:
        """事务化 session：异常回滚，正常提交。"""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _safe_url(self) -> str:
        """去掉 URL 中的密码后用于日志。"""
        if "@" not in self.url:
            return self.url
        scheme, _, rest = self.url.partition("://")
        if "@" not in rest:
            return self.url
        creds, _, host = rest.rpartition("@")
        user = creds.split(":")[0]
        return f"{scheme}://{user}:***@{host}"


def build_database(settings: Settings) -> Database:
    return build_database_from_config(settings.database)


def build_database_from_config(cfg: DatabaseConfig) -> Database:
    return Database(
        cfg.url,
        echo=cfg.echo,
        pool_size=cfg.pool_size,
        max_overflow=cfg.max_overflow,
    )
