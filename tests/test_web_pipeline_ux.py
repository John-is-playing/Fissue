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


def test_stuck_list_shows_top_n_and_collapses_rest(seeded_client):
    """默认展开条数由 `web.stuck_top_n` 决定（默认 2）。"""
    html = seeded_client.get("/pipeline").text
    assert "stuck-rest" in html
    assert "其余 5 条" in html  # 7 条里默认展开 2 条、折叠 5 条


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

# ---------------------------------------------------------------------------
# 6. 表格单元格不能被改成 flex（用户报的"高亮块比旁边矮、下面是无效点击"）
# ---------------------------------------------------------------------------


def test_action_cell_keeps_table_cell_layout(seeded_client):
    """动作列必须是 `<td class="cell-acts">` + 内层 `div.pipe-acts`。

    把 `display:flex` 直接给 `<td>`，它就不再作为 table-cell 参与行高计算
    （浏览器生成匿名单元格盒子），于是该格高度与整行不一致——行 hover 高亮的
    下沿会出现一段"看着能点、其实不属于任何内容"的死区，视觉上还比旁边矮一截。
    """
    html = seeded_client.get("/pipeline").text
    assert 'td class="pipe-acts"' not in html, "td 又被套上 flex 布局类了"
    assert 'class="cell-acts"' in html
    # 有 td.cell-acts 就必须有内层 flex 容器承载按钮
    assert 'class="cell-acts"' in html and '<div class="pipe-acts">' in html


# ---------------------------------------------------------------------------
# 7. 待办展开条数可配置（web.stuck_top_n，默认 2）
# ---------------------------------------------------------------------------


def _rows_before_details(html: str) -> int:
    """默认展开区里渲染了多少行待办（`<details>` 之前的部分）。"""
    head = html.split("<details", 1)[0]
    return head.count('class="mono">acme/web#')


def test_stuck_defaults_to_two_rows(seeded_client):
    """默认只展开 2 条：7 条数据里 5 条折叠。"""
    html = seeded_client.get("/pipeline").text
    assert _rows_before_details(html) == 2
    assert "其余 5 条" in html


def test_stuck_top_n_is_configurable(settings):
    """`web.stuck_top_n` 改大，展开区就跟着变多（排序仍是重要性优先）。"""
    settings.web.stuck_top_n = 4
    _seed(settings)
    with TestClient(create_app(settings)) as c:
        html = c.get("/pipeline").text
    assert _rows_before_details(html) == 4
    assert "其余 3 条" in html


def test_stuck_top_n_zero_collapses_everything(settings):
    """0 = 全部折叠：不留空表格，折叠块默认展开，避免"点了才看到东西"。"""
    settings.web.stuck_top_n = 0
    _seed(settings)
    with TestClient(create_app(settings)) as c:
        html = c.get("/pipeline").text
    head = html.split("<details", 1)[0]
    assert _rows_before_details(html) == 0
    assert "<table>" not in head, "默认区不该留一张空表"
    assert "全部 7 条" in html
    assert "<details class=\"stuck-rest\" open>" in html


def test_stuck_top_n_rejects_out_of_range():
    """范围 0–50：越界在加载期就报错，而不是悄悄夹到边界。"""
    from pydantic import ValidationError

    from fissue.config import WebConfig

    with pytest.raises(ValidationError):
        WebConfig(stuck_top_n=51)
    with pytest.raises(ValidationError):
        WebConfig(stuck_top_n=-1)


def test_stuck_top_n_from_env(monkeypatch):
    """环境变量能覆盖（`FISSUE_WEB_STUCK_TOP_N`），非法值直接报错。"""
    from fissue.config import ConfigError, load_settings

    monkeypatch.setenv("FISSUE_WEB_STUCK_TOP_N", "3")
    assert load_settings(require_llm_key=False).web.stuck_top_n == 3

    monkeypatch.setenv("FISSUE_WEB_STUCK_TOP_N", "三条")
    with pytest.raises(ConfigError):
        load_settings(require_llm_key=False)


# ---------------------------------------------------------------------------
# 8. 看板滚动观感：要么不滚，要么滚得好看
# ---------------------------------------------------------------------------


def test_kanban_columns_fit_without_scrolling():
    """六列必须在正文宽度里排得下——否则每块屏都被迫横向滚。

    算式：6 × 150(列最小宽) + 5 × 8(gap) = 940px；正文内容宽 ≈ 1180 − 64(padding)
    = 1116px。留了余量，正常屏幕不会出现滚动条。
    之前是 190px：6 × 190 + 5 × 12 = 1200 > 1116，刚刚好溢出，于是每屏都滚。
    """
    css = Path("src/fissue/web/static/app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    block = "\n".join(re.findall(r"\.pipe-kanban\s*\{([^}]*)\}", css))
    m = re.search(r"grid-auto-columns:\s*minmax\((\d+)px", block)
    assert m, "看板列宽不再是 minmax(…px, 1fr)，滚动行为变了"
    min_width = int(m.group(1))
    gap = 8  # --sp-2
    assert 6 * min_width + 5 * gap <= 1116, (
        f"六列放不下：6×{min_width} + 5×{gap} = {6 * min_width + 5 * gap} > 1116"
    )


def test_scrollbars_are_styled():
    """滚动条必须两套写法都写：标准属性 + WebKit 伪元素。

    只写一套时另一类浏览器会保留原生样式——深色主题下就是那条又亮又粗的白条。
    """
    css = Path("src/fissue/web/static/app.css").read_text(encoding="utf-8")
    for selector in (".pipe-kanban", ".table-scroll", "html"):
        assert f"{selector}::-webkit-scrollbar" in css, f"{selector} 缺少 WebKit 滚动条样式"
    assert "scrollbar-color:" in css
    assert "scrollbar-width: thin" in css


def test_kanban_reserves_room_for_its_scrollbar():
    """滚动条不能压在卡片下沿上。"""
    css = Path("src/fissue/web/static/app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    block = "\n".join(re.findall(r"\.pipe-kanban\s*\{([^}]*)\}", css))
    assert "padding-bottom" in block


# ---------------------------------------------------------------------------
# 9. 视图切换（看板/泳道/漏斗）必须能切
# ---------------------------------------------------------------------------


def test_view_switcher_scopes_data_mode_selector(seeded_client):
    """切换器只能用 `.pipe-modes [data-mode]`，不能用裸 `[data-mode]`。

    base.html 在运行时会给 `<html>` 加 `data-mode="simple|expert"`（界面详略开关）。
    裸选择器会把它当成按钮并在根节点上注册点击：任何点击冒泡上来都会再执行一次
    applyMode("simple")，于是刚切到的泳道/漏斗被立刻改回看板——表现就是"切换无效"。
    """
    html = seeded_client.get("/pipeline").text
    # 注意只能禁 `document.` 开头的裸调用：switcher.querySelectorAll(...) 也含同样的子串
    assert 'document.querySelectorAll("[data-mode]")' not in html, (
        "又用裸选择器了，会和 html[data-mode] 撞车"
    )
    assert 'switcher.querySelectorAll("[data-mode]")' in html


def test_view_switcher_confirms_the_collision_exists():
    """记录撞车的来源：`<html data-mode>` 确实是运行时加上的。

    这条不是在夸代码，是在固定前提——哪天界面详略开关换了属性名，这里会提醒复核。
    """
    base = Path("src/fissue/web/templates/base.html").read_text(encoding="utf-8")
    assert 'setAttribute("data-mode"' in base


def test_apply_mode_ignores_unknown_values(seeded_client):
    """未知视图名只忽略，不硬回退成看板（回退会让切换看起来失效）。"""
    html = seeded_client.get("/pipeline").text
    assert "if (MODES.indexOf(mode) < 0) return;" in html


def test_saved_mode_is_sanitized_before_first_apply(seeded_client):
    """localStorage 里的脏值要在首次 apply 前修掉，否则三个视图全 hidden（整页空白）。"""
    html = seeded_client.get("/pipeline").text
    assert 'if (MODES.indexOf(saved) < 0) saved = "kanban";' in html
    # switcher 必须先于 applyMode 的调用被解析，否则初始化时按钮高亮不上
    assert html.index('var switcher = document.querySelector(".pipe-modes")') < html.index(
        "applyMode(saved);"
    )


def test_all_three_views_exist_and_start_hidden(seeded_client):
    html = seeded_client.get("/pipeline").text
    for mode in ("funnel", "kanban", "swimlane"):
        assert f'data-view="{mode}"' in html, f"缺少视图 {mode}"
        assert f'data-mode="{mode}"' in html, f"缺少切换按钮 {mode}"
