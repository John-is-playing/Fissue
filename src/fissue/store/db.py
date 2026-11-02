"""数据库连接与 schema 管理。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple

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

#: 网络型后端（PostgreSQL / MySQL）的连接超时（秒）。
#:
#: psycopg 与 pymysql 都直接认 ``connect_timeout`` 这个名字（语义一致：秒）。
#: 见 `Database.__init__`：只有 sqlite 用不上它。
DB_CONNECT_TIMEOUT_SECONDS = 10


def force_fix_migration(dialect: str) -> tuple[str | None, str]:
    """``items.force_fix`` 补列在各方言上的 ``(探针 SQL, ALTER SQL)``。

    探针为 ``None`` 表示「这个方言一条语句就够」——PostgreSQL 的
    ``ADD COLUMN IF NOT EXISTS`` 本身幂等，先查一遍只是多一次往返。
    其余方言返回一条**只查一列**的探针：结果集按「是否出现 ``force_fix``」判断
    （见 :meth:`Database._migrate_items_force_fix`），所以 SQLite 用的是
    ``pragma_table_info`` 表值函数（返回一行一列 ``name``），而不是
    ``PRAGMA table_info``——后者的第一列是 ``cid``，无法与列名比对。

    为什么把这几个字符串抽成纯函数：这是全项目**唯一**必须有方言差异的 DDL。
    抽出来就能用单测钉住三种写法，而不必真的起三个数据库；写错方言（比如给
    MySQL 发 ``ADD COLUMN IF NOT EXISTS``）会在真机第一次 `db init` 时才炸。
    """
    if dialect == "postgresql":
        return None, (
            "ALTER TABLE items ADD COLUMN IF NOT EXISTS force_fix BOOLEAN NOT NULL DEFAULT FALSE"
        )
    if dialect == "mysql":
        # MySQL 既没有 ADD COLUMN IF NOT EXISTS，也没有 PRAGMA table_info；
        # 表结构只能问 information_schema（DATABASE() = 当前库）。
        return (
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'items'",
            "ALTER TABLE items ADD COLUMN force_fix BOOLEAN NOT NULL DEFAULT FALSE",
        )
    # 兜底按 SQLite 写：内存测试库与将来的其它方言都落到这里。
    return (
        "SELECT name FROM pragma_table_info('items')",
        "ALTER TABLE items ADD COLUMN force_fix BOOLEAN NOT NULL DEFAULT 0",
    )


def evaluation_extras_migration(dialect: str) -> tuple[str | None, str]:
    """``evaluations.extras`` 补列在各方言上的 ``(探针 SQL, ALTER SQL)``。

    这一列承载评测告警与旁路打分器读数（见 ``ai/system_one.py``）。与其它补列
    同一套口径；这里刻意**不加 NOT NULL**——MySQL 8 之前不允许给 JSON 列设默认值，
    而 ORM 侧该列本来就是可选的。
    """
    if dialect == "postgresql":
        return None, "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS extras JSON"
    if dialect == "mysql":
        return (
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'evaluations'",
            "ALTER TABLE evaluations ADD COLUMN extras JSON",
        )
    return (
        "SELECT name FROM pragma_table_info('evaluations')",
        "ALTER TABLE evaluations ADD COLUMN extras JSON",
    )


def llm_usage_estimated_migration(dialect: str) -> tuple[str | None, str]:
    """``llm_usage.estimated`` 补列在各方言上的 ``(探针 SQL, ALTER SQL)``。

    与 :func:`force_fix_migration` 同一套口径（探针只查一列、结果按列名判断；
    PostgreSQL 直接靠 ``IF NOT EXISTS``）。单独一个函数而不是把表名参数化：
    两条迁移各自只有一处调用，参数化反而让「哪张表漏了迁移」更难看出来。
    """
    if dialect == "postgresql":
        return None, (
            "ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS estimated BOOLEAN NOT NULL DEFAULT FALSE"
        )
    if dialect == "mysql":
        return (
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'llm_usage'",
            "ALTER TABLE llm_usage ADD COLUMN estimated BOOLEAN NOT NULL DEFAULT FALSE",
        )
    return (
        "SELECT name FROM pragma_table_info('llm_usage')",
        "ALTER TABLE llm_usage ADD COLUMN estimated BOOLEAN NOT NULL DEFAULT 0",
    )


class ColumnMigration(NamedTuple):
    """一次「补列」迁移的登记项。

    为什么要有这张登记表：此前每加一列就写一个 ``_migrate_xxx``，并在 ``create_all``
    与 ``ensure_schema`` 两个入口各手抄一遍调用——漏抄一处就是「旧库上 no such column」
    （树莓派部署时真炸过）。现在迁移是**一张有序表**，入口只调一次
    :meth:`Database._apply_migrations`，顺序与幂等由 :data:`SCHEMA_VERSION` 兜住。
    """

    #: 版本号，从 1 开始连续递增；只能追加，不能改号（改号等于给旧库重放一次 DDL）。
    version: int
    #: 人类可读的名字，进日志与 ``fissue_meta``，排障时能对上「上次是哪一步没跑完」。
    name: str
    #: 探针要检查的列名（见 :meth:`Database._apply_column_migration`）。
    column: str
    #: 该迁移在各方言下的 ``(探针 SQL, ALTER SQL)``，纯函数、可单测。
    ddl: Callable[[str], tuple[str | None, str]]


#: 迁移登记表。**只追加、只改末尾**：版本号是「跑到哪了」的唯一依据，
#: 中间插一条或改号，已经上线的库就会跳过或重放某一步。
MIGRATIONS: tuple[ColumnMigration, ...] = (
    ColumnMigration(1, "items.force_fix", "force_fix", force_fix_migration),
    ColumnMigration(2, "llm_usage.estimated", "estimated", llm_usage_estimated_migration),
    ColumnMigration(3, "evaluations.extras", "extras", evaluation_extras_migration),
)

#: 当前 schema 版本 = 最后一条迁移的版本号（空表时为 0）。
SCHEMA_VERSION = MIGRATIONS[-1].version if MIGRATIONS else 0

#: 迁移版本表。刻意用裸 DDL 而不是 ORM 表：老库升级上来时 ``items`` 这些表早就有了，
#: 这张表必须**先于**任何列探针存在；而它只有两列短文本，用不上 ORM 的类型变体
#: （MySQL 侧键列要 511 字符那套约束在这里不适用——64 字符 × utf8mb4 只有 256 字节）。
_META_DDL = (
    "CREATE TABLE IF NOT EXISTS fissue_meta ("
    "k VARCHAR(64) NOT NULL PRIMARY KEY, v VARCHAR(255) NOT NULL)"
)


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
            kwargs["connect_args"] = {"connect_timeout": DB_CONNECT_TIMEOUT_SECONDS}
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
        self._apply_migrations()
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
        self._apply_migrations()
        self._schema_ready = True
        log.debug("数据库表已确保存在：%s", self._safe_url())

    def _apply_migrations(self) -> None:
        """按版本号把还没跑过的补列迁移依序应用，然后记下新版本。

        为什么留着「探针」而不是只信版本号：版本号只能说明**我们**跑到哪一步了，
        说明不了这个库有没有被人手工动过（或上次 ALTER 成功、写版本号前进程被杀）。
        探针很便宜（一条 ``SELECT``），而漏补一列的代价是「所有带这列的查询直接
        no such column」。版本号的作用只是把「每次启动都探三条」降成「落后时才探」。
        """
        current = self._read_schema_version()
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            probe, alter = migration.ddl(self.engine.dialect.name)
            self._apply_column_migration(probe, alter, migration.column)
            log.info("已应用数据库迁移 v%d：%s", migration.version, migration.name)
        if current != SCHEMA_VERSION:
            self._write_schema_version(SCHEMA_VERSION)
            log.info("数据库 schema 版本：%d → %d", current, SCHEMA_VERSION)

    def _read_schema_version(self) -> int:
        """读当前 schema 版本；版本表不存在时建出来并返回 0。

        老库升级上来时 ``fissue_meta`` 一定不存在，返回 0 让全部迁移再跑一遍——
        它们幂等，代价是几条探针，换来「不必人工判断这次启动该跑哪几条」。
        """
        with self.engine.begin() as conn:
            conn.execute(text(_META_DDL))
            row = conn.execute(
                text("SELECT v FROM fissue_meta WHERE k = 'schema_version'")
            ).first()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):  # pragma: no cover - 只有人工改坏才走得到
            log.warning("fissue_meta.schema_version 不是整数（%r），按 0 处理", row[0])
            return 0

    def _write_schema_version(self, version: int) -> None:
        """记录 schema 版本。

        先 DELETE 再 INSERT：SQLite / PostgreSQL / MySQL 都认。三种方言的 upsert 语法
        各不相同（``ON CONFLICT`` / ``ON DUPLICATE KEY``），为一张只放一行的表分三套
        写法不值当。
        """
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM fissue_meta WHERE k = 'schema_version'"))
            conn.execute(
                text("INSERT INTO fissue_meta (k, v) VALUES ('schema_version', :v)"),
                {"v": str(version)},
            )

    def _apply_column_migration(self, probe: str | None, alter: str, column: str) -> None:
        """按「探针查一列 → 缺了再 ALTER」执行一次补列（全新库是空操作）。"""
        with self.engine.begin() as conn:
            if probe is None:
                conn.execute(text(alter))
                return
            # 探针语句要么返回「列名」列表，要么返回「有没有」——两种都只看第一列，
            # 于是统一按「是否出现目标列名」判断，不必为每种方言各写一个分支。
            found = {str(row[0]).lower() for row in conn.execute(text(probe))}
            if column in found:
                return
            conn.execute(text(alter))

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
