"""改动影响分析与业务视图：风险判定规则 + `/biz` 页面。

C2 的意义是"让人先看清楚再决定"：diff 本身是几十行加减号，非工程用户看不出
哪一行危险。判定必须是**确定性**的（正则 + 计数），不能换模型就换个结论。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fissue.fixer.impact import analyze
from fissue.models import (
    BlockReason,
    FixAttempt,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    RawItem,
    RepoRef,
)
from fissue.store.db import build_database
from fissue.store.repository import Repository
from fissue.web.app import create_app
from fissue.web.auth import PAGE_PREFIXES

# ---------------------------------------------------------------------------
# 风险判定
# ---------------------------------------------------------------------------


def _diff(path: str, added: list[str], removed: list[str]) -> str:
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1 +1 @@"]
    lines += [f"-{line}" for line in removed]
    lines += [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"


def test_source_only_fix_is_low_risk():
    impact = analyze(_diff("src/app.py", ["    return 0"], ["    return None"]))
    assert impact.risk == "low"
    assert impact.files == 1
    assert impact.added == 1 and impact.removed == 1
    assert impact.source_files == 1 and impact.test_files == 0
    assert impact.reasons


def test_removing_assertion_is_high_risk():
    """删断言是最典型的"让测试变绿"信号。"""
    impact = analyze(_diff("src/app.py", ["    pass"], ["    assert x == 1"]))
    assert impact.removed_asserts == 1
    assert impact.risk == "high"
    assert any("断言" in reason for reason in impact.reasons)


def test_test_only_change_is_high_risk():
    impact = analyze(_diff("tests/test_app.py", ["    assert f() == 2"], ["    assert f() == 1"]))
    assert impact.test_files == 1
    assert impact.source_files == 0
    assert impact.risk == "high"
    assert any("只改了测试" in reason for reason in impact.reasons)


def test_ci_change_is_high_risk():
    impact = analyze(_diff(".github/workflows/ci.yml", ["    python-version: 3.11"], ["    python-version: 3.10"]))
    assert impact.touched_ci is True
    assert impact.risk == "high"


def test_config_change_is_medium_risk():
    impact = analyze(_diff("pyproject.toml", ['"newdep>=1"'], ['"olddep>=1"']))
    assert impact.touched_config is True
    assert impact.risk == "medium"


def test_many_files_is_medium_risk():
    diff = "".join(
        _diff(f"src/mod{i}.py", ["    pass"], []) for i in range(6)
    )
    impact = analyze(diff)
    assert impact.files == 6
    assert impact.risk == "medium"
    assert any("6 个文件" in reason for reason in impact.reasons)


def test_test_heavier_than_source_is_medium():
    diff = _diff("src/app.py", ["    x = 1"], [])
    diff += _diff("tests/test_app.py", ["    assert a", "    assert b", "    assert c"], [])
    impact = analyze(diff)
    assert impact.test_files == 1 and impact.source_files == 1
    assert impact.risk == "medium"


def test_empty_diff_is_low_and_says_so():
    impact = analyze("")
    assert impact.files == 0
    assert impact.risk == "low"
    assert impact.reasons


def test_changed_files_fallback_when_diff_missing():
    impact = analyze("", changed_files=["tests/test_x.py", "src/x.py"])
    assert impact.files == 2
    assert impact.test_files == 1
    assert impact.source_files == 1


# ---------------------------------------------------------------------------
# 业务视图 /biz
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def _repo_of(settings) -> Repository:
    db = build_database(settings)
    db.create_all()
    return Repository(db)


def _seed(settings) -> tuple[str, str]:
    repo = _repo_of(settings)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="acme", name="web"))
    for n in (1, 2):
        repo.upsert_item(
            rid,
            RawItem(
                platform=Platform.GITHUB,
                repo="acme/web",
                number=n,
                item_type=ItemType.ISSUE,
                title=f"问题{n}",
                body="b",
            ),
        )
    repo.set_item_status(
        "github:acme/web#1",
        ItemStatus.NEEDS_MANUAL,
        block_reason=BlockReason.MISSING_TEST.value,
    )
    repo.set_item_status("github:acme/web#2", ItemStatus.MERGED)
    return "github:acme/web#1", "github:acme/web#2"


def test_biz_page_answers_three_questions(settings, client):
    _seed(settings)
    html = client.get("/biz").text
    assert "业务总览" in html
    assert "修好并合入" in html
    assert "需要你处理" in html
    assert "最近修好的" in html
    # 正文里不出现工程术语（这一页的定位就是给不做工程的人看）。
    # 只看 <main> 之后：侧栏的模式开关 tooltip 本身会提到"验证器命令/回归门"，
    # 那是全局提示、与这一页的定位无关。
    body = html.split("<main", 1)[1] if "<main" in html else html
    assert "回归门" not in body and "验证器" not in body


def test_biz_page_shows_blocked_with_next_step(settings, client):
    _seed(settings)
    html = client.get("/biz").text
    assert "为什么卡住" in html
    assert "缺少可执行的验证" in html
    assert "test_hint" in html


def test_biz_page_empty_state(settings, client):
    html = client.get("/biz").text
    assert "还没有修好的问题" in html
    assert "没有卡住的问题" in html


def test_biz_is_in_page_guard():
    """页面上有仓库名与条目标题，必须和总览同级受鉴权保护。"""
    assert "/biz" in PAGE_PREFIXES


def test_nav_links_to_biz(settings, client):
    assert 'href="/biz"' in client.get("/biz").text


def test_item_page_shows_impact_and_risk(settings, client):
    """修复记录里要能直接看出"改了几个文件、风险多大、为什么"。"""
    key, _ = _seed(settings)
    repo = _repo_of(settings)
    repo.save_fix_attempt(
        FixAttempt(
            item_key=key,
            outcome=FixOutcome.NEEDS_MANUAL,
            branch="fissue/fix-1",
            diff=_diff("tests/test_app.py", ["    pass"], ["    assert f() == 1"]),
            changed_files=["tests/test_app.py"],
        )
    )
    from urllib.parse import quote

    html = client.get("/item/" + quote(key, safe="")).text
    assert "高风险" in html
    assert "为什么是这个风险" in html
    assert "删除了 1 行断言" in html
