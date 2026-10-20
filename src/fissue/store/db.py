"""数据库连接与 schema 管理。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import DatabaseConfig, Settings
from ..errors import FissueError
from ..logging_setup import get_logger
from .tables import Base

log = get_logger(__name__)

#: SQLite 的 busy 超时（秒）。
#:
#: 实测（sqlite 3.54 / Python 3.11）：驱动**本来就**有 5s 的 busy 超时，写锁被占 0.8s 时
#: 第二个写者能等过去；但把超时压到 0.5s 就立刻抛 ``OperationalError: database is locked``
#: ——也就是说真正的上限是那 5s，且**没有开 WAL**：回滚日志模式下长**读**事务（导出、看板）
#: 会挡住写者的提交，一超过 5s 写者就失败。这里把超时明确调到 30s 并开 WAL。
SQLITE_BUSY_TIMEOUT_SECONDS = 30.0

#: PostgreSQL 的连接超时（秒）。psycopg 直接认 ``connect_timeout``。
#: 见 `Database.__init__`：非 sqlite 才用得上。
POSTGRES_CONNECT_TIMEOUT_SECONDS = 10


def ensure_sqlite_parent_dir(url: str) -> None:
    """SQLite 文件库：把父目录建出来。

    默认连接串指向 ``./data/fissue.db``——那是一个**目录里的文件**，而 SQLite 只会建文件、
    **不会建目录**。此前默认是 PostgreSQL（要用户自己建库），现在默认 SQLite，若不建目录，
    ``fissue db init`` 在全新机器上会直接 ``unable to open database file``，
    而报错离原因很远。

    只在「有文件路径」时动手：``:memory:`` 与空库名（``sqlite://``）跳过。
    """
    from sqlalchemy.engine import make_url

    database = make_url(url).database
    if not database or database == ":memory:":
        return
    parent = Path(database).expanduser().parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)


class Database:
    """封装 Engine 与 Session 生命周期。

    支持 PostgreSQL（生产）与 SQLite（本地/测试）两套 URL。
    """

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 5, max_overflow: int = 10) -> None:
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite:
            ensure_sqlite_parent_dir(url)
        # 「本进程内表已确保存在」的标记，供 ensure_schema() 做零开销短路
        self._schema_ready = False
        kwargs: dict[str, Any] = {"echo": echo, "future": True, "pool_pre_ping": True}
        if not self.is_sqlite:
            kwargs.update(pool_size=pool_size, max_overflow=max_overflow, pool_recycle=1800)
            # 连接超时：不给的话，一个不可路由的 PG 地址会一直等到操作系统的默认超时
            # （几十秒到两分钟），而「测试连接」按钮与初始化向导都直接连它——用户是站在页面前等的。
            kwargs["connect_args"] = {"connect_timeout": POSTGRES_CONNECT_TIMEOUT_SECONDS}
        else:
            kwargs["connect_args"] = {
                "check_same_thread": False,
                # 见 SQLITE_BUSY_TIMEOUT_SECONDS：默认 5s 太短，读事务一长就把写者顶掉。
                "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
            }
        try:
            self.engine: Engine = create_engine(url, **kwargs)
        except Exception as exc:  # pragma: no cover
            raise FissueError(f"无法创建数据库引擎（{url}）：{exc}") from exc
        if self.is_sqlite:
            self._enable_sqlite_concurrency()
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, class_=Session
        )

    def _enable_sqlite_concurrency(self) -> None:
        """给每条新连接开 WAL 并把 busy 超时落到 PRAGMA 上。

        两条都冲着实测出来的那个 5s 上限：

        * **WAL**：默认的回滚日志模式下读写互相挡——一个长读事务（导出、看板）会让写者的
          提交等不到锁，超过 busy 超时就失败。WAL 下读者不再挡写者。
        * **busy_timeout**：显式调大（驱动默认 5s），与 ``connect_args["timeout"]`` 同值，
          两条路都设上，免得将来只改一处。

        ``:memory:`` 上这两条是无害的：``journal_mode`` 改不动、会返回 ``memory``。
        """
        from sqlalchemy import event

        @event.listens_for(self.engine, "connect")
        def _apply_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
                # SQLite **默认不强制**外键，而且这个开关是**按连接**生效的（不是建库时定下的），
                # 所以必须每条新连接都开。不开的后果：表里 6 条 `ondelete="CASCADE"` 与所有
                # 外键在本地的 SQLite 库上**形同虚设**——插得进指向不存在父行的子行、删父行也
                # 不级联，等换成 PostgreSQL 才发现库里早就有脏数据。
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    # -- schema -----------------------------------------------------------

    def create_all(self) -> None:
        """建表（幂等）。

        ``checkfirst=True``（默认）会对每张表先做一次存在性检查，
        所以重复调用是安全的，但会在一个进程里反复产生额外的查询。
        高频路径请改用 :meth:`ensure_schema`。
        """
        Base.metadata.create_all(self.engine)
        # Web/常驻服务的启动路径走这里（而不是 ensure_schema）——迁移必须两个入口都跑，
        # 否则旧库上任何带新列的查询都会 no such column（树莓派部署时实炸过）。
        self._migrate_items_force_fix()
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
        self._migrate_items_force_fix()
        self._schema_ready = True
        log.debug("数据库表已确保存在：%s", self._safe_url())

    def _migrate_items_force_fix(self) -> None:
        """给旧库（建表时还没有 ``force_fix`` 列）补列。

        ``create_all`` 只建缺失的表、**不改**已存在的表——常驻服务的库是长跑的，
        不加这一刀，旧库上任何带这列的查询都会 ``no such column`` 直接崩。
        SQLite 用 PRAGMA 查列再 ALTER；PostgreSQL 原生支持 ``ADD COLUMN IF NOT EXISTS``。
        全新库（列已随 create_all 建出）两条路径都是空操作。
        """
        with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(
                    text(
                        "ALTER TABLE items ADD COLUMN IF NOT EXISTS "
                        "force_fix BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                )
            else:
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(items)"))}
                if "force_fix" not in cols:
                    conn.execute(
                        text("ALTER TABLE items ADD COLUMN force_fix BOOLEAN NOT NULL DEFAULT 0")
                    )

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
