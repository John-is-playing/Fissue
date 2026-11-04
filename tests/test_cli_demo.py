"""演示模式（`fissue demo`）：读内置夹具、写独立库、只读。

守的是三件"新用户第一次打开"必须成立的事：

1. 不需要任何 token / 模型就能有数据（否则空页面直接劝退）；
2. 数据落**独立库**，绝不碰 `.env` 指向的生产库（这个坑踩过）；
3. 演示实例是**只读**的（否则有人会拿演示数据去真打标签/提 PR）。
"""

from __future__ import annotations

import pytest

from fissue.cli.demo import (
    DEMO_STATUSES,
    available_kits,
    demo_settings,
    parse_fixture,
    seed_demo,
)
from fissue.store.db import build_database
from fissue.store.repository import Repository

#: 演示夹具随源码仓库提供；装成 wheel 后没有时这些用例自动跳过。
pytestmark = pytest.mark.skipif(
    not available_kits(), reason="没有 demo/ 夹具目录（安装包环境）"
)


def test_kits_are_discovered() -> None:
    kits = available_kits()
    assert {"textkit", "ratekit"}.issubset(set(kits))


def test_parse_fixture_reads_front_matter() -> None:
    from fissue.cli.demo import DEMO_ROOT

    path = sorted((DEMO_ROOT / "textkit" / "fixtures").glob("*.md"))[0]
    data = parse_fixture(path)
    assert data["title"], "标题不能为空"
    assert data["body"], "正文不能为空"
    # 文件头注释不能混进正文（否则页面上会看到一段 HTML）
    assert "<!--" not in data["body"]


def test_seed_writes_items_into_isolated_db(tmp_path) -> None:
    from fissue.config import Settings

    settings = Settings()
    settings.app.data_dir = str(tmp_path)
    settings.database.url = f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"

    stats = seed_demo(settings, "textkit")
    assert stats["items"] >= 5
    assert stats["repo"] == "demo/textkit"

    # 条目真的进了库，且状态铺开（否则看板全是 0，演示没意义）
    db = build_database(settings)
    repo = Repository(db)
    items = repo.list_items(limit=50)
    assert len(items) >= 5
    statuses = {item.status.value for item in items}
    assert len(statuses) > 1, "状态应当铺开，不能全是一个"


def test_seed_is_idempotent(tmp_path) -> None:
    """重复导入只更新、不翻倍（upsert 按 repo + number）。

    否则用户多跑一次 `demo up` 就会看到 10 条重复条目。
    """
    from fissue.config import Settings

    settings = Settings()
    settings.app.data_dir = str(tmp_path)
    settings.database.url = f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"

    first = seed_demo(settings, "textkit")["items"]
    second = seed_demo(settings, "textkit")["items"]
    assert first == second
    db = build_database(settings)
    assert len(Repository(db).list_items(limit=100)) == first


def test_unknown_kit_raises(tmp_path) -> None:
    from fissue.config import Settings

    settings = Settings()
    settings.app.data_dir = str(tmp_path)
    settings.database.url = f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"
    with pytest.raises(FileNotFoundError):
        seed_demo(settings, "no-such-kit")


def test_demo_settings_isolates_db_and_is_read_only(tmp_path) -> None:
    """演示配置必须换库、换数据目录、并强制只读。

    直接改对象而不是走 overrides：`.env` 的 `FISSUE_DATABASE_URL` 优先级更高，
    "配置里写演示库"随时可能被它覆盖成生产库。
    """
    from fissue.config import Settings

    base = Settings()
    base.app.data_dir = str(tmp_path)
    base.database.url = "sqlite:///prod-should-not-be-touched.db"
    base.web.read_only = False

    settings = demo_settings(kit="ratekit", base=base)
    assert "demo/ratekit" in settings.database.url
    assert settings.database.url != base.database.url
    assert settings.web.read_only is True
    assert settings.repos == []
    assert "demo" in settings.app.data_dir


def test_statuses_cover_several_views() -> None:
    """状态清单要能撑起「待办 / 在途 / 已出结论」三类视图。"""
    assert "needs_manual" in DEMO_STATUSES
    assert any(s in DEMO_STATUSES for s in ("verified", "evaluated"))
    assert any(s in DEMO_STATUSES for s in ("fixing", "queued"))
