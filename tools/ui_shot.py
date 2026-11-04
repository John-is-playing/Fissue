#!/usr/bin/env python
"""用真浏览器检查 Web UI：截图 + 控制台错误 + 布局实测。

为什么需要这个工具
------------------
本机没有可用的「无头 Edge」（`--headless` 各种模式全部静默失败），但 **Playwright
自带 chromium 可用**。在这之前所有前端修复都只能靠静态断言守着——而静态断言抓不到
"hover 高亮比旁边矮""点了没反应""控制台每行一条 ReferenceError"这类问题，它们
不报错、不冒烟，只是用起来像坏页。

两种用法
--------
    # 起一个临时服务（自动造数据），跑全部检查并截图
    python tools/ui_shot.py

    # 对着正在跑的服务检查（不造数据，看真实状态）
    python tools/ui_shot.py --url http://127.0.0.1:8000

    # 只截图不改数据、只跑某一页、放大到 1600 宽
    python tools/ui_shot.py --only /pipeline --width 1600

输出
----
`data/ui-shots/<时间戳>/` 下：每页每主题一张 PNG + `report.json`。
控制台错误、水平溢出、看板列换行、动作单元格高度不一致、格子点击区缺口、
三个视图切换是否真的生效——全部以**退出码**表达（0 = 全过，1 = 有问题）。

依赖：playwright（本机已装）+ 其 chromium 内核；服务端用项目自己的 create_app。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

#: 默认检查的页面。顺序即截图顺序。
PAGES = [
    "/",
    "/items",
    "/pipeline",
    "/jobs",
    "/repos",
    "/ops",
    "/ops/guide?repo_slug=acme/web",
    "/settings",
    "/setup",
    "/login",
]


# ---------------------------------------------------------------------------
# 临时服务（带种子数据）
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed(settings: Any, tmp: Path) -> None:
    """造一点数据，否则页面全是空态，"格子能不能点"根本没得测。

    直接在库里改状态而不是走完整流水线：这里要的是**渲染用的样本**，
    不是重跑一遍抓取/评测。
    """
    from sqlalchemy import select

    from fissue.models import ItemType, Platform, RawItem, RepoRef
    from fissue.store.db import build_database
    from fissue.store.repository import Repository
    from fissue.store.tables import ItemRow

    db = build_database(settings)
    db.create_all()
    repo = Repository(db)
    repo_id = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="acme", name="web"))

    prios = ["tier1", "tier2", "tier2", "none", "tier1", "tier2", "none"]
    for n, _prio in enumerate(prios, start=1):
        repo.upsert_item(
            repo_id,
            RawItem(
                platform=Platform.GITHUB,
                repo="acme/web",
                number=n,
                item_type=ItemType.ISSUE if n % 2 else ItemType.PR,
                title=f"示例条目 {n}：这是一条用来测试渲染的长标题，故意写长一点看换行",
                body="正文示例",
                url=f"https://example.invalid/{n}",
            ),
        )
    with db.session() as s:
        for row in s.scalars(select(ItemRow)):
            idx = int(row.number) - 1
            row.status = "needs_manual" if idx < 4 else "fixing"
            row.priority = prios[idx]


def _start_server(tmp: Path) -> tuple[str, Any]:
    """在后台线程起 uvicorn，返回 (base_url, server)。

    用 127.0.0.1：回环来源天然可信，页面直接可读，不用处理登录与 CSRF。
    """
    import uvicorn

    from fissue.config import Settings
    from fissue.web.app import create_app

    settings = Settings()
    settings.database.url = f"sqlite:///{tmp.as_posix()}/ui.db"
    settings.app.data_dir = str(tmp / "data")
    settings.repos = []
    # 关掉调度：检查 UI 时不想后台任务在跑，否则页面上的"进行中"会跳
    for attr, value in (("enabled", False),):
        schedule = getattr(settings, "schedule", None)
        if schedule is not None and hasattr(schedule, attr):
            setattr(schedule, attr, value)

    _seed(settings, tmp)

    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("临时服务没能在 30 秒内起来")
    return f"http://127.0.0.1:{port}", server


# ---------------------------------------------------------------------------
# 检查结果
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """一条问题。``page`` / ``detail`` 直接进报告，便于对照截图。"""

    page: str
    check: str
    detail: str
    severity: str = "error"  # error | warn


@dataclass
class Report:
    base_url: str
    out_dir: Path
    shots: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add(self, page: str, check: str, detail: str, severity: str = "error") -> None:
        self.findings.append(Finding(page=page, check=check, detail=detail, severity=severity))

    def dump(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "shots": self.shots,
            "findings": [f.__dict__ for f in self.findings],
        }


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------


def _new_page(browser: Any, *, width: int, theme: str, report: Report, page_path: str) -> Any:
    """开一个页面，并挂上控制台/JS 异常采集。

    `pageerror` 与 `console.error` 都要收：前者是未捕获异常（比如
    `Fissue is not defined`），后者是应用自己的报错，两者都不会让页面变白，
    但都说明有东西在坏。
    """
    ctx = browser.new_context(viewport={"width": width, "height": 900}, locale="zh-CN")
    page = ctx.new_page()
    # 主题偏好存在 localStorage：在文档脚本执行前注入，避免先白后黑
    page.add_init_script(
        f"try {{ localStorage.setItem('fissue.ui.theme', {json.dumps(theme)}); "
        f"localStorage.setItem('fissue.pipeline.mode', 'kanban'); }} catch (e) {{}}"
    )
    page.on(
        "console",
        lambda msg: (
            report.add(page_path, "console", f"[{msg.type}] {msg.text}")
            if msg.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda exc: report.add(page_path, "pageerror", str(exc)))
    return page


def _shot(page: Any, out: Path, name: str, report: Report) -> None:
    # 整页截图：滚动条/折叠区也能看到，便于和用户截图对照
    target = out / f"{name}.png"
    page.screenshot(path=str(target), full_page=True)
    report.shots.append(str(target.relative_to(out.parent.parent)))


# ---------------------------------------------------------------------------
# 检查项
#
# 每一项都对应一个**真实踩过的坑**。判据尽量用"量出来的数"而不是"文本里有没有"，
# 因为后者静态断言也能做，没必要开浏览器。
# ---------------------------------------------------------------------------


def check_page_overflow(page: Any, path: str, report: Report, width: int) -> None:
    """整页不该横向溢出。

    横向溢出的典型表现是"右边有一块能拖过去、但什么都没有"的背景。
    """
    box = page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})"
    )
    if box["sw"] > box["cw"] + 1:
        report.add(path, "overflow", f"页面横向溢出：scrollWidth={box['sw']} > clientWidth={box['cw']}（窗口 {width}px）")


def check_kanban(page: Any, path: str, report: Report) -> None:
    """看板：六列必须在同一行，且不需要横向滚动。

    - 列被挤到第二行：`offsetTop` 出现多个取值；
    - 需要滚动：看板容器 `scrollWidth` 超过自身宽度。
    这两条都曾经发生过（`auto-fit` 换行、列宽下限算错导致每屏都滚）。
    """
    if not page.query_selector(".pipe-kanban"):
        return
    rows = page.evaluate(
        "() => [...new Set([...document.querySelectorAll('.pipe-col')].map(e => e.offsetTop))].length"
    )
    if rows > 1:
        report.add(path, "kanban-wrap", f"看板列被挤成了 {rows} 行（应当只有 1 行）")

    scroll = page.evaluate(
        "() => { const k = document.querySelector('.pipe-kanban');"
        " return k ? {sw: k.scrollWidth, cw: k.clientWidth} : null; }"
    )
    if scroll and scroll["sw"] > scroll["cw"] + 1:
        report.add(
            path,
            "kanban-scroll",
            f"看板需要横向滚动：scrollWidth={scroll['sw']} > clientWidth={scroll['cw']}",
            severity="warn",
        )


def check_action_cell_alignment(page: Any, path: str, report: Report) -> None:
    """动作单元格的高度必须与整行一致。

    把 `display:flex` 挂在 `<td>` 上时它不再是 table-cell，格子高度与行高不一致，
    行 hover 高亮的下沿就会露出一段"看着能点、其实不属于任何内容"的死区。
    """
    rows = page.evaluate(
        """() => [...document.querySelectorAll('tr')]
            .filter(tr => tr.querySelector('td.cell-acts'))
            .map(tr => {
              const cell = tr.querySelector('td.cell-acts');
              return {row: tr.getBoundingClientRect().height,
                      cell: cell.getBoundingClientRect().height};
            })"""
    )
    for index, item in enumerate(rows):
        if abs(item["row"] - item["cell"]) > 1:
            report.add(
                path,
                "cell-height",
                f"第 {index + 1} 行动作格高度 {item['cell']:.1f}px ≠ 行高 {item['row']:.1f}px"
                "（td 又被改成 flex 了？）",
            )


def check_kanban_cell_clickable(page: Any, path: str, report: Report) -> None:
    """看板格子：右下角内缩 2px 处点下去，命中的必须是这个格子自己。

    这就是"高亮区里有一截点不动"的死区的量化判据。

    必须先滚进视口再量：`elementFromPoint` 对**视口外**的坐标返回 null
    （看板常在首屏下方），不滚就会把"量不到"误报成"点不到"。
    """
    cells = page.query_selector_all("a.pipe-cell")
    if not cells:
        return
    for index, cell in enumerate(cells[:6]):
        cell.evaluate("el => el.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(30)
        hit = cell.evaluate(
            """el => {
              const r = el.getBoundingClientRect();
              // 内缩要**大于圆角半径**：格子有 6px 圆角，贴着角量会落到圆角外侧、
              // 命中父容器，把"圆角"误判成"死区"。取 12/6px 也仍在近角区域。
              const points = [
                [r.right - 12, r.bottom - 6],   // 右下
                [r.left + 12, r.bottom - 6],    // 左下
                [(r.left + r.right) / 2, r.bottom - 3],  // 下沿中点
              ];
              const results = [];
              for (const [x, y] of points) {
                if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) {
                  return {outside: true};
                }
                const node = document.elementFromPoint(x, y);
                results.push({
                  inside: !!(node && (node === el || el.contains(node))),
                  tag: node ? node.tagName + (node.className ? '.' + node.className : '') : null,
                  x: Math.round(x), y: Math.round(y),
                });
              }
              const bad = results.find(item => !item.inside);
              return {outside: false, inside: !bad, bad: bad || null};
            }"""
        )
        if hit.get("outside"):
            continue
        if not hit["inside"]:
            bad = hit["bad"]
            report.add(
                path,
                "cell-dead-zone",
                f"第 {index + 1} 个格子在 ({bad['x']},{bad['y']}) 命中了 {bad['tag']}，"
                "不是格子本身（有死区）",
            )


def check_view_switching(page: Any, path: str, report: Report) -> None:
    """三个视图切换必须真的生效。

    这条直接对应"切泳道无效"的事故：`<html data-mode>` 与切换按钮共用
    `data-mode` 属性，裸选择器把根节点也注册成了按钮，切完立刻被改回看板。
    """
    switcher = page.query_selector(".pipe-modes")
    if not switcher:
        return
    for mode in ("swimlane", "kanban", "funnel"):
        button = page.query_selector(f'.pipe-modes [data-mode="{mode}"]')
        if not button or not button.is_visible():
            continue  # 漏斗在简单模式下不可见，跳过
        button.click()
        page.wait_for_timeout(120)
        state = page.evaluate(
            """m => {
              const view = document.querySelector('[data-view="' + m + '"]');
              const others = [...document.querySelectorAll('[data-view]')]
                .filter(v => v !== view).map(v => v.hidden);
              return {hidden: view ? view.hidden : null, othersHidden: others.every(Boolean)};
            }""",
            mode,
        )
        if state["hidden"] is not False or not state["othersHidden"]:
            report.add(
                path,
                "view-switch",
                f"点了「{mode}」但视图没切过去（hidden={state['hidden']}，"
                f"其它视图是否都隐藏={state['othersHidden']}）",
            )


def check_stuck_layout(page: Any, path: str, report: Report) -> None:
    """「需要你处理的」标题必须在表格**上方**，不能并排。

    这里踩过一次：`.pipe-stuck` 和 `.pipe-acts` 共用了一条 `display:flex` 规则，
    于是 section 变成横向排列，标题被挤到表格左侧、表头与数据整体错位。
    截图里一眼能看出来，静态断言却抓不到——所以这条必须由浏览器量。
    """
    box = page.evaluate(
        """() => {
          const section = document.querySelector('.pipe-stuck');
          if (!section) return null;
          const head = section.querySelector('h2');
          const table = section.querySelector('table');
          if (!head || !table) return null;
          const h = head.getBoundingClientRect(), t = table.getBoundingClientRect();
          return {hBottom: h.bottom, tTop: t.top, sideBySide: t.left > h.right + 1};
        }"""
    )
    if not box:
        return
    if box["sideBySide"] or box["hBottom"] > box["tTop"] + 1:
        report.add(
            path,
            "stuck-layout",
            f"待办区标题与表格并排/错位（标题底 {box['hBottom']:.0f}px，表格顶 {box['tTop']:.0f}px，"
            f"并排={box['sideBySide']}）",
        )


def check_theme(page: Any, path: str, theme: str, report: Report) -> None:
    actual = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    if actual != theme:
        report.add(path, "theme", f"请求主题 {theme}，实际 data-theme={actual}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


#: 反向对照：每个毛病**单独一张合成页面**，互不干扰。
#:
#: 为什么必须分开：第一版把它们塞进同一张页面，结果 `cell-height` 与 `kanban-wrap`
#: 两条检查静默地不响——不是检查写错了，而是合成页面没造出真实条件（flex 的 td
#: 只有在**旁边那一格更高**时才会比行矮；`auto-fit` 也只有容器窄到放不下时才换行）。
#: 合成页面不够狠，反向对照就变成了自我安慰。
SELF_TEST_SCENARIOS: dict[str, str] = {
    # 控制台：console.error + 未捕获异常（后者就是 "Fissue is not defined" 的形态）
    "errors": """
<!doctype html><html data-theme="light"><body><h1>x</h1>
<script>console.error("合成：console.error 必须被收上来"); notDefinedAnywhere();</script>
</body></html>""",
    # 换行：旧实现 `repeat(auto-fit, minmax(120px, 1fr))` 在 300px 容器里塞 4 列 → 换行
    "wrap": """
<!doctype html><html data-theme="light"><head><style>
  .pipe-kanban { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
                 gap: 8px; width: 300px; }
  .pipe-col { height: 60px; background: #eee; }
</style></head><body><div class="pipe-kanban">
  <div class="pipe-col"></div><div class="pipe-col"></div>
  <div class="pipe-col"></div><div class="pipe-col"></div>
</div></body></html>""",
    # 滚动：列流布局 + 窄容器 → 必须横向滚（应当报 warn）
    "scroll": """
<!doctype html><html data-theme="light"><head><style>
  .pipe-kanban { display: grid; grid-auto-flow: column; grid-auto-columns: 120px;
                 gap: 8px; width: 300px; overflow-x: auto; }
  .pipe-col { height: 60px; background: #eee; }
</style></head><body><div class="pipe-kanban">
  <div class="pipe-col"></div><div class="pipe-col"></div>
  <div class="pipe-col"></div><div class="pipe-col"></div>
</div></body></html>""",
    # 动作格：td 被设成 flex，且**旁边那一格更高** → 格子比行矮（实测 29px vs 86px）
    "flex-td": """
<!doctype html><html data-theme="light"><head><style>
  td.cell-acts { display: flex; padding: 4px 0; background: #eee; }
</style></head><body><table><tbody>
  <tr><td>多行文字<br>第二行<br>第三行<br>第四行</td>
      <td class="cell-acts"><div>按钮</div></td></tr>
</tbody></table></body></html>""",
    # 死区：格子被容器裁掉一部分 → 右下角命中不到格子自己
    "dead-zone": """
<!doctype html><html data-theme="light"><head><style>
  .clip { width: 100px; height: 40px; overflow: hidden; }
  a.pipe-cell { display: block; width: 100px; height: 120px; background: #ddd; }
</style></head><body><div class="clip"><a class="pipe-cell" href="#">格子</a></div>
</body></html>""",
    # 待办区：标题与表格被同一条 flex 规则并排（真实事故）
    "stuck-inline": """
<!doctype html><html data-theme="light"><head><style>
  .pipe-stuck { display: flex; gap: 8px; }
</style></head><body><div class="pipe-stuck">
  <h2>需要你处理的</h2><table><tbody><tr><td>条目</td></tr></tbody></table>
</div></body></html>""",
    # 切换：按钮点了不切视图（正是 data-mode 撞车事故的形态）
    "view-switch": """
<!doctype html><html data-theme="light"><body>
<div class="pipe-modes"><button data-mode="swimlane">泳道</button></div>
<section data-view="kanban"></section>
<section data-view="swimlane" hidden></section>
</body></html>""",
}

#: 每个场景该触发的检查（场景名 → 期望的 check 名）。
#: `errors` 场景的两条由 `_new_page` 的监听器采集，不经过显式 check 函数。
SELF_TEST_EXPECTED: dict[str, tuple[str, ...]] = {
    "errors": ("console", "pageerror"),
    "wrap": ("kanban-wrap",),
    "scroll": ("kanban-scroll",),
    "flex-td": ("cell-height",),
    "dead-zone": ("cell-dead-zone",),
    "stuck-inline": ("stuck-layout",),
    "view-switch": ("view-switch",),
}


def self_test() -> int:
    """用合成页面验证**每个检查都真的会响**。

    没有这一步，"全部通过"可能只是眼睛闭着——第一版反向对照就把两条哑检查
    （`cell-height`、`kanban-wrap`）照出来了，原因还是合成页面没造出真实条件。
    """
    from playwright.sync_api import sync_playwright

    report = Report(base_url="about:blank", out_dir=Path("."))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for name, html in SELF_TEST_SCENARIOS.items():
                path = f"/self-test/{name}"
                page = _new_page(
                    browser, width=800, theme="light", report=report, page_path=path
                )
                try:
                    page.set_content(html, wait_until="load")
                    page.wait_for_timeout(150)
                    check_kanban(page, path, report)
                    check_action_cell_alignment(page, path, report)
                    check_stuck_layout(page, path, report)
                    check_kanban_cell_clickable(page, path, report)
                    check_view_switching(page, path, report)
                finally:
                    page.context.close()
        finally:
            browser.close()

    failed: list[str] = []
    total = sum(len(v) for v in SELF_TEST_EXPECTED.values())
    print(f"反向对照：{len(SELF_TEST_SCENARIOS)} 个场景，期望 {total} 类问题")
    for name, expected in SELF_TEST_EXPECTED.items():
        path = f"/self-test/{name}"
        seen = {f.check for f in report.findings if f.page == path}
        missing = [check for check in expected if check not in seen]
        detail = "；".join(
            f"{f.check}: {f.detail}" for f in report.findings if f.page == path
        ) or "（没有报警）"
        print(f"  {'✓' if not missing else '✗'} {name}：{detail}")
        if missing:
            failed.append(f"{name} 没触发 {missing}")

    if failed:
        print(f"\n✗ 这些检查是哑的：{failed}")
        return 1
    print("\n全部检查都能响，工具不是摆设。")
    return 0


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    out_root = REPO_ROOT / "data" / "ui-shots"
    out_dir = out_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    server = None
    tmp: tempfile.TemporaryDirectory | None = None
    if args.url:
        base_url = args.url.rstrip("/")
    else:
        # ignore_cleanup_errors：Windows 下 SQLite 文件常被占用，清理失败不该让工具以异常退出
        tmp = tempfile.TemporaryDirectory(prefix="fissue-ui-", ignore_cleanup_errors=True)
        base_url, server = _start_server(Path(tmp.name))

    report = Report(base_url=base_url, out_dir=out_dir)
    pages = args.only or PAGES
    themes = ["light", "dark"] if args.theme == "both" else [args.theme]

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in themes:
                    for path in pages:
                        page = _new_page(
                            browser, width=args.width, theme=theme, report=report, page_path=path
                        )
                        try:
                            page.goto(f"{base_url}{path}", wait_until="load", timeout=20000)
                            page.wait_for_timeout(250)
                            check_page_overflow(page, path, report, args.width)
                            check_theme(page, path, theme, report)
                            if path.startswith("/pipeline"):
                                check_kanban(page, path, report)
                                check_action_cell_alignment(page, path, report)
                                check_stuck_layout(page, path, report)
                                check_kanban_cell_clickable(page, path, report)
                                check_view_switching(page, path, report)
                            name = f"{path.strip('/').replace('/', '-').replace('?', '_').replace('=', '-') or 'index'}-{theme}"
                            _shot(page, out_dir, name, report)
                        finally:
                            page.context.close()
                        if not args.quiet:
                            print(f"  ✓ {path} [{theme}]")
            finally:
                browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        # 报告必须在**清理之前**落盘：Windows 上临时 SQLite 文件还挂在 uvicorn 线程
        # 手里，删除会抛 WinError 32——清理失败不该把已经跑出来的结果一起丢掉。
        (out_dir / "report.json").write_text(
            json.dumps(report.dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if tmp is not None:
            time.sleep(0.3)
            tmp.cleanup()

    print()
    if report.findings:
        print(f"发现 {len(report.findings)} 个问题：")
        for f in report.findings:
            print(f"  [{f.severity}] {f.page} · {f.check}：{f.detail}")
    else:
        print("没有发现问题。")
    print(f"\n截图与报告：{out_dir}")
    return 1 if any(f.severity == "error" for f in report.findings) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用真浏览器检查 Web UI（截图 + 控制台 + 布局）")
    parser.add_argument("--url", help="对已运行的服务检查（默认自动起临时服务）")
    parser.add_argument("--only", nargs="+", help="只检查这些路径，例如 --only /pipeline /items")
    parser.add_argument("--width", type=int, default=1280, help="视口宽度（默认 1280）")
    parser.add_argument(
        "--theme", choices=["both", "light", "dark"], default="both", help="截哪个主题"
    )
    parser.add_argument("--quiet", action="store_true", help="不逐页打印进度")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="反向对照：用故意做坏的合成页面验证每条检查真的会报警",
    )
    args = parser.parse_args(argv)
    return self_test() if args.self_test else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
