"""真实 MySQL 上的仓储层回归（按环境变量启用）。

为什么单开一个文件、而不是把整包测试切到 MySQL：`tests/conftest.py` 的 `db` 夹具给
**每个用例一个独立临时 SQLite 文件**，MySQL 上做不到「每个用例一个新库」而不付昂贵代价。
所以这里只钉 :mod:`fissue.store` 在 MySQL 上**独有**的风险点——它们在 SQLite / PostgreSQL
上永远不会暴露，而一旦踩到，都是在真机 `db init` 或第一轮抓取时才炸：

* **键列长度**：utf8mb4 下索引上限 3072 字节，`uq_item_identity` 是四列复合唯一索引；
* **长文本列**：`TEXT` 只有 64KB，而 GitHub 正文上限恰好是 **65536 字符**；
* **补列迁移**：MySQL 既没有 ``ADD COLUMN IF NOT EXISTS`` 也没有 ``PRAGMA table_info``；
* **`FOR UPDATE SKIP LOCKED`**：队列领取用它，MySQL 8.0 才有（5.7 直接语法错）；
* **时间戳无时区**：MySQL 的 ``DATETIME`` 不存时区，读回来是 naive，与 aware 的
  ``_now()`` 相减会 ``TypeError``——库里凡是算「多久之前」的地方都会炸。

怎么跑（本机实测过的命令）::

    docker run -d --name fissue-mysql \\
        -e MYSQL_ROOT_PASSWORD=fissue-root-pw -e MYSQL_DATABASE=fissue \\
        -e MYSQL_USER=fissue -e MYSQL_PASSWORD=fissue-pw -p 13306:3306 mysql:8.0 \\
        --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci

    FISSUE_TEST_MYSQL_URL="mysql+pymysql://fissue:fissue-pw@127.0.0.1:13306/fissue?charset=utf8mb4" \\
        python -m pytest tests/test_store_mysql.py -q

不设这个变量就全部 skip——CI 没有 MySQL 服务时行为完全不变。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from fissue.models import (
    Comment,
    ItemType,
    Platform,
    QueueName,
    RawItem,
    RepoRef,
)
from fissue.store.db import Database
from fissue.store.repository import Repository

MYSQL_URL = os.environ.get("FISSUE_TEST_MYSQL_URL", "")

pytestmark = [
    pytest.mark.skipif(
        not MYSQL_URL,
        reason="未设置 FISSUE_TEST_MYSQL_URL（真机 MySQL 用例，见本文件头部说明）",
    ),
    # 真机建库 + 大字段往返，比 sqlite 慢一个量级；120s 的默认超时对这里偏紧。
    pytest.mark.timeout(300),
]


@pytest.fixture()
def db() -> Database:
    """每个用例从**干净 schema** 开始。

    为什么必须 drop：真机库是复用的（本机验证时它还留着 `fissue db init` 建的表），
    而用例之间共享同一个库——不清一遍，第二条用例就会撞上第一条留下的唯一约束。
    """
    database = Database(MYSQL_URL)
    database.drop_all()
    database.create_all()
    return database


@pytest.fixture()
def repo(db: Database) -> Repository:
    return Repository(db)


def _issue(number: int = 42, *, body: str = "复现步骤：调用后崩溃", comment: str = "已确认") -> RawItem:
    return RawItem(
        platform=Platform.GITHUB,
        repo="psf/requests",
        number=number,
        item_type=ItemType.ISSUE,
        title="程序崩溃：超时后抛异常",
        body=body,
        labels=["bug", "needs-triage"],
        author="reporter",
        comments=[Comment(author="maintainer", body=comment)],
    )


def _repo_id(repo: Repository) -> int:
    return repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))


def test_schema_uses_mysql_safe_types(db: Database) -> None:
    """真机核对建出来的类型与索引宽度——这一条是整套 MySQL 支持的地基。"""
    with db.engine.connect() as conn:
        cols = {
            name: (data_type, max_len)
            for name, data_type, max_len in conn.execute(
                text(
                    "SELECT column_name, data_type, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'items'"
                )
            ).all()
        }
        collation = conn.execute(
            text(
                "SELECT table_collation FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'items'"
            )
        ).scalar_one()
        widths = conn.execute(
            text(
                "SELECT s.index_name, SUM(c.character_maximum_length * 4) AS bytes "
                "FROM information_schema.statistics s "
                "JOIN information_schema.columns c "
                "  ON c.table_schema = s.table_schema AND c.table_name = s.table_name "
                " AND c.column_name = s.column_name "
                "WHERE s.table_schema = DATABASE() AND s.table_name = 'items' "
                "GROUP BY s.index_name"
            )
        ).all()

    assert cols["body"] == ("mediumtext", 16777215), "TEXT 只有 64KB，正文必然装不下"
    assert cols["key"] == ("varchar", 511)
    assert cols["number"] == ("varchar", 64), "裸 String 在 MySQL 上建表就会 CompileError"
    assert collation.startswith("utf8mb4"), "库/表字符集不是 utf8mb4，中文与 emoji 会乱码"

    for name, width in widths:
        assert (width or 0) <= 3072, f"索引 {name} 占 {width} 字节，超出 MySQL utf8mb4 上限"


def test_body_and_comment_over_64kb_round_trip(repo: Repository) -> None:
    """超过 64KB 的正文与评论要能原样存取（中文按 UTF-8 是 3 字节/字）。"""
    rid = _repo_id(repo)
    body = "中文正文" * 25000          # 10 万字符 / 约 300KB
    comment = "评论内容" * 15000       # 6 万字符 / 约 180KB
    item = _issue(body=body, comment=comment)

    repo.upsert_item(rid, item)
    fresh = repo.get_item(item.key)

    assert fresh is not None
    assert fresh.body == body
    assert fresh.comments and fresh.comments[0].body == comment


def test_queue_claim_and_finish_uses_skip_locked(repo: Repository) -> None:
    """队列领取走 ``FOR UPDATE SKIP LOCKED``（MySQL 8.0+ 才有，5.7 会语法错）。"""
    rid = _repo_id(repo)
    item = _issue()
    repo.upsert_item(rid, item)

    entry_id = repo.enqueue(QueueName.VERIFY, item.key, payload={"why": "真机回归"})
    claimed = repo.claim_next(QueueName.VERIFY)

    assert claimed is not None and int(claimed.id) == entry_id
    assert claimed.status == "running"

    repo.finish_queue_entry(entry_id, status="done", result={"ok": True})
    assert repo.queue_pending_count(QueueName.VERIFY) == 0


def test_queue_age_arithmetic_survives_naive_datetimes(repo: Repository, db: Database) -> None:
    """MySQL 的 ``DATETIME`` 不存时区：读回来是 naive，与 aware 的 ``_now()`` 相减不能炸。

    凡是算「这条 claim 多久没动」的地方（reclaim / stale 判定）都做这个减法，
    漏一处就是 ``TypeError: can't subtract offset-naive and offset-aware datetimes``——
    而它只会在真机上出现，SQLite 存的是同一串带时区的值，永远照不出来。
    """
    rid = _repo_id(repo)
    item = _issue()
    repo.upsert_item(rid, item)
    repo.enqueue(QueueName.VERIFY, item.key)

    # 这两个方法都会拿库里的时间戳与当前时间相减
    assert repo.reclaim_stuck_queue_entries() == []
    assert repo.queue_stats().get(QueueName.VERIFY.value, {}).get("pending") == 1


def test_force_fix_column_migration_readds_on_mysql(repo: Repository, db: Database) -> None:
    """补列迁移走 ``information_schema``：删掉列后 ``ensure_schema`` 必须补回来。

    MySQL 的两个替代方案都不存在：没有 ``ADD COLUMN IF NOT EXISTS``（那是 PostgreSQL），
    也没有 ``PRAGMA table_info``（那是 SQLite）。这条路径写错的表现是
    ``OperationalError: (1064, 'You have an error in your SQL syntax')``——只在真机出现。
    """
    rid = _repo_id(repo)
    item = _issue()
    repo.upsert_item(rid, item)

    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE items DROP COLUMN force_fix"))
        cols = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'items'"
                )
            ).all()
        }
    assert "force_fix" not in cols, "前提没造成：列没删掉"

    # 新实例才有「本进程还没建过表」的标记，ensure_schema 才会真的跑迁移
    fresh = Database(MYSQL_URL)
    fresh.ensure_schema()

    with fresh.engine.connect() as conn:
        cols = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'items'"
                )
            ).all()
        }
    assert "force_fix" in cols, "ensure_schema 没能把 force_fix 补回来"

    # 存量行的默认值必须是 False（不是 NULL），否则读出来会让 `force_fix and ...` 变成三值逻辑
    got = Repository(fresh).get_item(item.key)
    assert got is not None and got.force_fix is False
