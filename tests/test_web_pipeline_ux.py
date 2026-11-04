"""流水线页的交互回归测试（用户报的五个问题的守门）。

这些都不是"好看不好看"，而是**坏了不报错、只是用起来像坏页**的问题：

1. `Fissue is not defined` —— content 块里的内联脚本比 app.js 先执行；
2. 看板格子只有小字可点，hover 高亮区里有一截死区；
3. 泳道看着是一张表，点哪儿都没反应；
4. 看板列明明还有空间却被挤到第二行；
5. 「需要你处理的」一屏塞不下，反而是最重要的被淹掉。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fissue.models import ItemType, Platform, RawItem, RepoRef
from fissue.store.db import build_database
from fissue.store.repository import Repository
from fissue.store.tables import ItemRow
from fissue.web.app import create_app

PRIOS = ["tier2", "none", "tier1", "tier2", "tier1", "none", "tier2"]


def _seed(settings, *, status: str = "needs_manual") -> None:
    """造 7 条卡住的条目（含 2 条 tier1），用于验证排序与折叠。"""
    settings.repos = []
    db = build_database(settings)
    db.create_all()
    repo = Repository(db)
    repo_id = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="acme", name="web"))
    for n, _prio in enumerate(PRIOS, start=1):
        repo.upsert_item(
            repo_id,
            RawItem(
                platform=Platform.GITHUB,
                repo="acme/web",
                number=n,
                item_type=ItemType.ISSUE,
                title=f"条目{n}",
                body="x",
                url=f"https://example.invalid/{n}",
            ),
        )
    with db.session() as s:
        for row in s.scalars(select(ItemRow)):
            row.status = status
            row.priority = PRIOS[int(row.number) - 1]


@pytest.fixture
def seeded_client(settings):
    _seed(settings)
    with TestClient(create_app(settings)) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. 控制台不能有 "Fissue is not defined"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/pipeline", "/", "/jobs"])
def test_no_inline_fissue_calls_in_content(seeded_client, path):
    """页面内容里不允许出现 `document.write(Fissue...)` 这类早执行的调用。

    content 块渲染在 app.js 之前，任何直接引用 `Fissue` 的内联脚本都会抛
    ReferenceError，并且那一列从此永远渲染不出来。相对时间改由 `[data-time]`
    占位 + app.js 填充。
    """
    html = seeded_client.get(path).text
    assert "document.write(Fissue" not in html


def test_relative_time_uses_data_time_placeholder(seeded_client):
    html = seeded_client.get("/pipeline").text
    assert 'data-time="' in html
    # 占位文本要有兜底：JS 没跑起来时也不能是空白单元格
    assert "<time data-time=" in html


# ---------------------------------------------------------------------------
# 2/4. 看板：整格是链接、单行不换行
# ---------------------------------------------------------------------------


def test_kanban_cell_is_the_whole_link(seeded_client):
    """格子必须整块可点。

    以前只有格子里那行「看条目」可点，而 hover 高亮的是整格——高亮区下沿
    就成了"看着能点、点了没反应"的死区，格子内容长短不同还会让高亮块比旁边矮。
    """
    html = seeded_client.get("/pipeline").text
    assert '<a class="pipe-cell"' in html
    # 旧结构（格子里再套一个小链接）必须消失
    assert 'class="small" href="/items?lane=' not in html


def test_kanban_never_wraps_to_a_second_row():
    """列数固定，不能因为列宽不够就把最后一列甩到第二行。

    注意 `.pipe-kanban` 出现在多处（有一处是分组选择器），所以要把**所有**
    同名声明块拼起来看，只取第一段会拿到别的规则。
    """
    css = Path("src/fissue/web/static/app.css").read_text(encoding="utf-8")
    # 注释里可能会提到 auto-fit（解释"为什么不用它"），先剥掉再断言
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    blocks = re.findall(r"\.pipe-kanban\s*\{([^}]*)\}", css)
    assert blocks, "没找到 .pipe-kanban 的样式"
    joined = "\n".join(blocks)
    assert "grid-auto-flow: column" in joined
    assert "auto-fit" not in joined, "auto-fit 会在空间不足时换行"


# ---------------------------------------------------------------------------
# 3. 泳道：整行可点 + 阶段点可跳
# ---------------------------------------------------------------------------


def test_swimlane_rows_are_clickable(seeded_client):
    html = seeded_client.get("/pipeline").text
    assert 'data-href="/item/' in html
    # 键盘也要能用
    assert 'tabindex="0"' in html
    assert "pipe-dot-link" in html


def test_clickable_row_script_ignores_inner_controls(seeded_client):
    """整行可点不能把行内按钮/链接的点击抢走。"""
    html = seeded_client.get("/pipeline").text
    assert "openRow" in html
    assert 'closest("a, button, input, select, label, textarea")' in html


def test_run_job_failure_is_not_silent(seeded_client):
    """脚本没加载完时点动作要有提示，不能静默无反应。"""
    html = seeded_client.get("/pipeline").text
    assert "页面脚本没有加载完成" in html


# ---------------------------------------------------------------------------
# 5. 「需要你处理的」：前 5 条 + 折叠其余，按重要性排序
# ---------------------------------------------------------------------------


def test_stuck_list_shows_top_five_and_collapses_rest(seeded_client):
    html = seeded_client.get("/pipeline").text
    assert "stuck-rest" in html
    assert "其余 2 条" in html  # 7 条里折叠 2 条


def test_stuck_list_is_sorted_by_importance(seeded_client):
    """tier1 必须排在前 5 条里（7 条数据里 2 条 tier1 却排在第 3、5 位）。"""
    html = seeded_client.get("/pipeline").text
    head = html.split("<details", 1)[0]
    assert head.count(">tier1<") == 2, "tier1 应当全部落在默认展开的 5 条里"


def test_stuck_list_links_to_items_page(seeded_client):
    html = seeded_client.get("/pipeline").text
    assert "/items?status=needs_manual" in html


def test_no_stuck_means_no_section(settings):
    """没有卡住的条目时不该出现空区块（空表比没有更让人困惑）。"""
    settings.repos = []
    db = build_database(settings)
    db.create_all()
    with TestClient(create_app(settings)) as c:
        html = c.get("/pipeline").text
    assert "需要你处理的" not in html