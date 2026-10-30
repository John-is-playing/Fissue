"""分页控件的**数据**构造（纯函数）。

模板里现算页码链接有三个坑，都会在真实使用里暴露：

1. **丢参数**：翻页时只拼 `?page=N`，筛选条件（kind / status / q）就没了——
   用户以为筛选被重置。所以这里必须把当前查询串原样带上。
2. **页码无限增长**：几十页时把每页都列出来，控件比内容还长。所以给窗口：
   首页、末页、当前页 ±N，中间用省略号。
3. **越界**：删掉几条后原来的第 3 页只剩 2 页。报错或给空白都像「数据没了」，
   夹回末页最接近预期（与 `JobRegistry.page` 的夹取口径一致）。

做成纯函数是为了能测——分页错位的 bug 在页面上很难用眼睛看出来。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

#: 当前页左右各显示几个页码。
DEFAULT_WINDOW = 2


def build_pager(
    *,
    page: int,
    pages: int,
    total: int,
    per_page: int,
    base_path: str,
    params: dict[str, Any] | None = None,
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """构造分页控件的数据。

    :param page: 当前页（越界会被夹回 ``[1, pages]``）
    :param pages: 总页数（至少 1）
    :param total: 总条数（用来显示「共 N 条」与区间）
    :param per_page: 每页条数
    :param base_path: 链接前缀，例如 ``/jobs``
    :param params: 需要**原样保留**的查询参数（筛选条件）。值为空/None 的会被丢掉，
                   免得链接里堆一串 ``kind=&status=``。
    """
    per = max(1, int(per_page))
    total_pages = max(1, int(pages))
    cur = min(max(1, int(page)), total_pages)

    # 只保留有值的参数：空串与 None 传下去等于没筛，留着只会让链接变脏。
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}

    def url_for(target: int) -> str:
        query = urlencode({**clean, "page": target})
        return f"{base_path}?{query}" if query else base_path

    # 页码窗口：首页/末页/当前页附近。相邻区段用「…」占位。
    visible = {1, total_pages}
    for n in range(cur - window, cur + window + 1):
        if 1 <= n <= total_pages:
            visible.add(n)

    links: list[dict[str, Any]] = []
    previous = 0
    for n in sorted(visible):
        if previous and n - previous > 1:
            links.append({"gap": True, "n": None, "url": "", "current": False})
        links.append({"gap": False, "n": n, "url": url_for(n), "current": n == cur})
        previous = n

    # 区间文字：0 条时说「0 条」而不是「0–0 条」（后者看着像出错）。
    if total <= 0:
        span = "共 0 条"
    else:
        first = (cur - 1) * per + 1
        last = min(cur * per, total)
        span = f"第 {first}–{last} 条，共 {total} 条"

    return {
        "page": cur,
        "pages": total_pages,
        "total": total,
        "per_page": per,
        "span": span,
        "links": links,
        "prev_url": url_for(cur - 1) if cur > 1 else None,
        "next_url": url_for(cur + 1) if cur < total_pages else None,
        # 只有一页时不画控件：一个「1」加两个灰按钮纯属噪音。
        "show": total_pages > 1,
    }
