"""分页控件的测试。

分页错位在页面上很难用眼睛看出来（「第 2 页显示的其实是第 1 页的尾巴」这种），
所以每个不变量都直接钉住：
页码链接必须带着筛选参数、窗口之外用省略号、越界页夹回合法范围、
只有一页时不画控件。
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from fissue.web.paging import build_pager


def _q(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def _nums(pager) -> list:  # noqa: ANN001
    return [link["n"] for link in pager["links"] if not link["gap"]]


def _gaps(pager) -> int:  # noqa: ANN001
    return sum(1 for link in pager["links"] if link["gap"])


# ---------------------------------------------------------------------------
# 基本形态
# ---------------------------------------------------------------------------


def test_first_page_has_no_prev() -> None:
    p = build_pager(page=1, pages=5, total=100, per_page=20, base_path="/jobs")
    assert p["prev_url"] is None
    assert p["next_url"] is not None
    assert _q(p["next_url"])["page"] == ["2"]


def test_last_page_has_no_next() -> None:
    p = build_pager(page=5, pages=5, total=100, per_page=20, base_path="/jobs")
    assert p["next_url"] is None
    assert _q(p["prev_url"])["page"] == ["4"]


def test_single_page_hides_the_control() -> None:
    """只有一页时不画控件：一个「1」加两个灰按钮纯属噪音。"""
    p = build_pager(page=1, pages=1, total=5, per_page=20, base_path="/jobs")
    assert p["show"] is False
    assert p["prev_url"] is None and p["next_url"] is None


def test_empty_result_says_zero() -> None:
    """0 条时说「共 0 条」，而不是「第 0–0 条」（后者看着像出错）。"""
    p = build_pager(page=1, pages=1, total=0, per_page=20, base_path="/jobs")
    assert p["span"] == "共 0 条"


# ---------------------------------------------------------------------------
# 翻页要带着筛选条件
# ---------------------------------------------------------------------------


def test_page_links_keep_filters() -> None:
    """只拼 `?page=N` 会让翻页悄悄丢掉筛选，用户以为筛选被重置。"""
    p = build_pager(
        page=2, pages=5, total=100, per_page=20, base_path="/jobs",
        params={"kind": "scan", "status": "failed"},
    )
    for link in p["links"]:
        if link["gap"]:
            continue
        q = _q(link["url"])
        assert q["kind"] == ["scan"] and q["status"] == ["failed"], link["url"]
        assert q["page"] == [str(link["n"])]


def test_empty_filters_are_dropped_from_the_url() -> None:
    """空串与 None 传下去等于没筛，留在链接里只会让 URL 变脏。"""
    p = build_pager(
        page=1, pages=3, total=30, per_page=10, base_path="/jobs",
        params={"kind": "", "status": None, "per_page": 10},
    )
    assert "kind" not in _q(p["next_url"])
    assert "status" not in _q(p["next_url"])
    assert _q(p["next_url"])["per_page"] == ["10"]


# ---------------------------------------------------------------------------
# 越界与边界
# ---------------------------------------------------------------------------


def test_page_beyond_the_end_is_clamped_to_last() -> None:
    """删掉几条后原来的第 3 页可能只剩 2 页：报错或给空白都像「数据没了」。"""
    p = build_pager(page=99, pages=2, total=7, per_page=20, base_path="/jobs")
    assert p["page"] == 2
    assert p["next_url"] is None


def test_page_below_one_is_clamped_to_first() -> None:
    p = build_pager(page=0, pages=3, total=30, per_page=10, base_path="/jobs")
    assert p["page"] == 1
    assert p["prev_url"] is None


def test_zero_per_page_does_not_divide_by_zero() -> None:
    p = build_pager(page=1, pages=1, total=5, per_page=0, base_path="/jobs")
    assert p["per_page"] == 1


def test_span_reports_the_visible_range() -> None:
    p = build_pager(page=3, pages=5, total=100, per_page=20, base_path="/jobs")
    assert p["span"] == "第 41–60 条，共 100 条"


def test_last_page_span_is_short() -> None:
    """末页不满时区间要收窄，不能写成「第 91–110 条」。"""
    p = build_pager(page=3, pages=3, total=42, per_page=20, base_path="/jobs")
    assert p["span"] == "第 41–42 条，共 42 条"


# ---------------------------------------------------------------------------
# 页码窗口
# ---------------------------------------------------------------------------


def test_middle_page_shows_first_last_and_neighbours() -> None:
    """几十页时把每页都列出来，控件比内容还长。"""
    p = build_pager(page=10, pages=20, total=400, per_page=20, base_path="/jobs")
    nums = _nums(p)
    assert 1 in nums and 20 in nums          # 首末页
    assert all(n in nums for n in (8, 9, 10, 11, 12))   # 当前页 ±2
    assert len(nums) < 20, "窗口没生效，所有页都列出来了"
    assert _gaps(p) >= 2, "首末与窗口之间应当有省略号"


def test_no_gap_when_pages_are_few() -> None:
    p = build_pager(page=2, pages=3, total=30, per_page=10, base_path="/jobs")
    assert _gaps(p) == 0


@pytest.mark.parametrize("page", [1, 2, 3, 4, 5])
def test_current_page_is_marked_exactly_once(page: int) -> None:
    """当前页高亮必须唯一——两个「on」用户就不知道自己在哪。"""
    p = build_pager(page=page, pages=5, total=50, per_page=10, base_path="/jobs")
    assert sum(1 for link in p["links"] if link["current"]) == 1


@pytest.mark.parametrize("pages", [1, 2, 5, 20, 100])
def test_links_are_always_ascending_and_unique(pages: int) -> None:
    p = build_pager(page=max(1, pages // 2), pages=pages, total=pages * 10,
                    per_page=10, base_path="/jobs")
    nums = _nums(p)
    assert nums == sorted(nums)
    assert len(nums) == len(set(nums))


# ---------------------------------------------------------------------------
# JobRegistry.page：分页 + 筛选
# ---------------------------------------------------------------------------


def _registry(n: int, settings):  # noqa: ANN001, ANN202
    """造 n 条已结束的作业，创建时间依次递增（保证顺序确定）。"""
    from datetime import datetime, timedelta, timezone

    from fissue.web import jobs as jobs_mod

    reg = jobs_mod.JobRegistry(settings)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for i in range(n):
        job = jobs_mod.Job(id=f"j{i:03d}", kind="scan" if i % 2 == 0 else "evaluate")
        job.status = jobs_mod.DONE if i % 3 else jobs_mod.FAILED
        job.created_at = base + timedelta(minutes=i)
        reg._jobs[job.id] = job
    return reg


def test_registry_page_slices_newest_first(settings) -> None:  # noqa: ANN001
    reg = _registry(25, settings)
    data = reg.page(page=1, per_page=10)
    assert data["total"] == 25
    assert data["pages"] == 3
    assert len(data["items"]) == 10
    assert data["items"][0]["id"] == "j024", "应当按创建时间倒序（最新的在前）"


def test_registry_page_is_contiguous_and_non_overlapping(settings) -> None:  # noqa: ANN001
    """翻页不能重复或漏条：把三页拼起来应当正好是全部且无重复。"""
    reg = _registry(25, settings)
    seen: list[str] = []
    for page in (1, 2, 3):
        seen.extend(item["id"] for item in reg.page(page=page, per_page=10)["items"])
    assert len(seen) == 25
    assert len(set(seen)) == 25


def test_registry_page_filters_by_kind(settings) -> None:  # noqa: ANN001
    reg = _registry(10, settings)
    data = reg.page(kind="scan", per_page=50)
    assert data["total"] == 5
    assert all(item["kind"] == "scan" for item in data["items"])


def test_registry_page_filters_by_status(settings) -> None:  # noqa: ANN001
    reg = _registry(9, settings)
    data = reg.page(status="failed", per_page=50)
    assert data["total"] == 3
    assert all(item["status"] == "failed" for item in data["items"])


def test_registry_page_combines_filters(settings) -> None:  # noqa: ANN001
    reg = _registry(12, settings)
    data = reg.page(kind="scan", status="failed", per_page=50)
    assert all(i["kind"] == "scan" and i["status"] == "failed" for i in data["items"])


def test_registry_page_clamps_out_of_range(settings) -> None:  # noqa: ANN001
    reg = _registry(30, settings)
    data = reg.page(page=99, per_page=10)
    assert data["page"] == 3, "越界页夹回末页"
    assert data["items"]


def test_registry_page_empty_result_still_has_one_page(settings) -> None:  # noqa: ANN001
    reg = _registry(0, settings)
    data = reg.page(page=1, per_page=10)
    assert data["total"] == 0
    assert data["pages"] == 1
    assert data["items"] == []


def test_registry_page_lists_available_filter_values(settings) -> None:  # noqa: ANN001
    """筛选下拉的选项来自实际出现过的值——写死清单会在新增种类时漏掉它。"""
    reg = _registry(6, settings)
    data = reg.page()
    assert data["kinds"] == ["evaluate", "scan"]
    assert "done" in data["statuses"] and "failed" in data["statuses"]
