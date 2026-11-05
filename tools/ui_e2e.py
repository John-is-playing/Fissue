#!/usr/bin/env python
"""真浏览器 E2E：跑完整栈，并**真的点**（不是只看 HTML 里有没有字符串）。

与 `tools/ui_shot.py` 的分工：那个是"渲染快照 + 布局断言"（看页面长成什么样），
这个是"交互链路"（点了会发生什么）：

* 设置页三档修复策略：切换 → 保存 → 重载后仍保持 → 覆盖层文件真的写了；
* 主题切换：点一次 → `data-theme` 变化 → 刷新后保持；
* 流水线三视图：看板 / 泳道 / 漏斗切得过去、且只有一个是可见的；
* `/ops` 失败记录的「回滚到上一版」：点下去出确认框，确认后真的发出
  `POST /deployments/{id}/rollback`（请求被拦截，不会连真机）；
* 新手引导三档：只读 / 半自动 / 一键 切换生效并记住。

用法：

    python tools/ui_e2e.py              # 起临时 demo 实例跑全部场景
    python tools/ui_e2e.py --keep       # 跑完不关服务（自己接着看）
    python tools/ui_e2e.py --base-url http://127.0.0.1:8000   # 对着已有实例

退出码 0 = 全过；1 = 有失败场景（每步的截图留在 data/ui-e2e/<时间戳>/）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_shot import Report, _new_page, _shot  # noqa: E402

OUT_ROOT = REPO_ROOT / "data" / "ui-e2e"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# 临时实例：demo 数据 + 一条「失败且可回滚」的部署记录
# ---------------------------------------------------------------------------


def _seed_failed_deployment(settings: Any) -> int:
    """造一条 failed 且带 previous_release 的部署记录。

    没有它就没法验证「失败行内回滚」——而这个按钮只在"失败 + 有上一版"时出现，
    正好是最该被点、也最容易被写错的条件。
    """
    from sqlalchemy import select

    from fissue.deploy.schema import AppSpec, DeployPlan, ProbeSpec
    from fissue.store.db import build_database
    from fissue.store.deploy_repo import DeployRepository
    from fissue.store.tables import DeploymentRow

    repo = DeployRepository(build_database(settings))
    host_id = repo.upsert_host(name="pi5", address="192.168.2.10", credential="fissuebox:v1:X")
    plan = DeployPlan(
        repo_slug="demo/textkit",
        app=AppSpec(name="textkit"),
        health=[ProbeSpec(url="http://192.168.2.10:8000/")],
    )
    dep_id = repo.create_deployment(
        host_id=host_id,
        repo_slug="demo/textkit",
        ref="main",
        commit_sha=None,
        plan=plan.dump(),
        app_root="/opt/fissue-deploy",
    )
    db = build_database(settings)
    with db.session() as session:
        row = session.scalar(select(DeploymentRow).where(DeploymentRow.id == dep_id))
        row.status = "failed"
        row.previous_release = "20261101T100000Z"
        row.error = "probe-http 失败：ConnectError"
    return int(dep_id)


def start_server(tmp: Path) -> tuple[str, Any, dict[str, Any]]:
    """起一个隔离实例：临时库、临时覆盖层、demo 数据、一条失败部署。"""
    import uvicorn

    from fissue.cli.demo import seed_demo
    from fissue.config import Settings
    from fissue.web.app import create_app

    settings = Settings()
    settings.app.data_dir = str(tmp / "data")
    settings.database.url = f"sqlite:///{(tmp / 'e2e.db').as_posix()}"
    # 覆盖层必须指到临时目录：否则设置页「保存」会写进仓库里的 config.local.yaml，
    # 跑一次 E2E 就悄悄改掉本机配置。
    settings.config_path = str(tmp / "config.yaml")
    settings.overlay_path = str(tmp / "config.local.yaml")
    settings.env_path = str(tmp / ".env")
    # **必须**把库地址写进 .env：设置页「保存」会触发配置重载，而 .env 的优先级高于
    # config.yaml / 覆盖层。不写它，重载后 database 会回落成默认的 ./data/fissue.db
    # （仓库里的库），后面 /ops 看到的就是另一个空库——第一轮 E2E 里"没有回滚按钮"
    # 正是这个原因，不是产品缺陷。
    Path(settings.env_path).write_text(
        f"FISSUE_DATABASE_URL=sqlite:///{(tmp / 'e2e.db').as_posix()}\n", encoding="utf-8"
    )
    settings.overlay_path and Path(settings.overlay_path).write_text(
        "web:\n  read_only: false\n", encoding="utf-8"
    )
    Path(settings.config_path).write_text("web:\n  read_only: false\n", encoding="utf-8")
    settings.repos = []
    settings.web.read_only = False
    if hasattr(settings, "schedule"):
        settings.schedule.enabled = False

    stats = seed_demo(settings, "textkit")
    dep_id = _seed_failed_deployment(settings)

    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("临时服务没起来")
    meta = {
        "items": stats["items"],
        "deployment_id": dep_id,
        "overlay": str(settings.overlay_path),
        "database": str(settings.database.url),
    }
    return f"http://127.0.0.1:{port}", server, meta


# ---------------------------------------------------------------------------
# 场景：每个都对应一次"点了应该发生什么"
# ---------------------------------------------------------------------------


def scenario_theme(page: Any, report: Report, path: str = "/") -> None:
    """三态主题（跟随系统 → 浅色 → 深色）：每点一次偏好都要变，且刷新后保持。

    判据看 **`data-theme-pref`** 而不是 `data-theme`：浅色系统下 `auto` 与 `light`
    渲染结果完全相同，只看 `data-theme` 会把"正常的第一次点击"误判成没反应。
    """
    pref0 = page.evaluate("() => document.documentElement.getAttribute('data-theme-pref')")
    page.click("#theme-toggle")
    page.wait_for_timeout(150)
    pref1 = page.evaluate("() => document.documentElement.getAttribute('data-theme-pref')")
    if pref0 == pref1:
        report.add(path, "theme-toggle", f"点了主题按钮，偏好仍是 {pref0}")

    # 再点一次应当走到 dark：此时 data-theme 必须真的变
    page.click("#theme-toggle")
    page.wait_for_timeout(150)
    pref2 = page.evaluate("() => document.documentElement.getAttribute('data-theme-pref')")
    theme2 = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    if pref2 != "dark" or theme2 != "dark":
        report.add(path, "theme-cycle", f"第二次点击后 pref={pref2} theme={theme2}，期望都是 dark")

    page.reload(wait_until="load")
    page.wait_for_timeout(200)
    persisted_pref = page.evaluate(
        "() => document.documentElement.getAttribute('data-theme-pref')"
    )
    persisted_theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    if (persisted_pref, persisted_theme) != ("dark", "dark"):
        report.add(
            path,
            "theme-persist",
            f"刷新后变成 pref={persisted_pref} theme={persisted_theme}，偏好没存住",
        )


def scenario_pipeline_views(page: Any, report: Report, path: str = "/pipeline") -> None:
    """看板 / 泳道 / 漏斗逐个切：目标视图可见，其余全部隐藏。

    漏斗按钮只在专家模式出现（它属于吞吐分析），所以先切到专家模式。
    """
    page.evaluate("() => localStorage.setItem('fissue.ui.mode', 'expert')")
    page.reload(wait_until="load")
    page.wait_for_timeout(250)

    for mode in ("swimlane", "funnel", "kanban"):
        button = page.query_selector(f'.pipe-modes [data-mode="{mode}"]')
        if not button or not button.is_visible():
            continue
        button.click()
        page.wait_for_timeout(150)
        state = page.evaluate(
            """m => {
              const target = document.querySelector('[data-view="' + m + '"]');
              const others = [...document.querySelectorAll('[data-view]')]
                .filter(v => v.dataset.view !== m);
              return {hidden: target ? target.hidden : null,
                      othersHidden: others.every(v => v.hidden)};
            }""",
            mode,
        )
        if state["hidden"] is not False or not state["othersHidden"]:
            report.add(
                path,
                "view-switch",
                f"点了「{mode}」：目标 hidden={state['hidden']}，其余是否全隐藏={state['othersHidden']}",
            )


def scenario_settings_strategy(page: Any, report: Report, path: str = "/settings") -> None:
    """三档修复策略：切换 → 保存 → 覆盖层真的写盘 → 刷新后仍是这一档。

    这是"页面上的开关是否真的接到了后端"的判据：只断言选中态不够，
    必须看写出来的 config.local.yaml。
    """
    draft_radio = page.query_selector('input[name="fix-strategy"][value="draft"]')
    if draft_radio is None:
        report.add(path, "strategy-ui", "设置页没有三档修复策略控件")
        return
    page.check('input[name="fix-strategy"][value="draft"]')
    page.click('#save-bar button.primary')
    page.wait_for_timeout(1500)  # 等 PUT /settings 落盘

    page.reload(wait_until="load")
    page.wait_for_timeout(300)
    checked = page.evaluate(
        """() => {
          const el = document.querySelector('input[name="fix-strategy"]:checked');
          return el ? el.value : null;
        }"""
    )
    if checked != "draft":
        report.add(path, "strategy-persist", f"保存后刷新，选中的是 {checked!r}，期望 draft")


def scenario_rollback(page: Any, report: Report, path: str = "/ops") -> dict[str, Any]:
    """失败记录的回滚按钮：点下去出确认框，确认后**真的**发出回滚请求。

    请求用 `page.route` 拦住并伪造成功响应：这条链路会 SSH 到远端，E2E 不该连真机，
    但要证明"点下去确实发了请求"——只断言按钮存在是不算的。
    """
    captured: dict[str, Any] = {}
    page.route(
        "**/deployments/*/rollback",
        lambda route: (
            captured.update(url=route.request.url, method=route.request.method),
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "rolled_back", "release": "20261101T100000Z"}),
            ),
        ),
    )

    button = page.query_selector('button[data-act="rollback-row"]')
    if button is None:
        report.add(path, "rollback-ui", "失败记录里没有「回滚到上一版」按钮")
        return captured

    button.click()
    try:
        page.wait_for_selector("dialog[open] button[value=yes]", timeout=4000)
    except Exception:  # noqa: BLE001 - 没弹确认框就是问题本身
        report.add(path, "rollback-confirm", "点了回滚没弹出确认框")
        return captured
    page.click("dialog[open] button[value=yes]")
    page.wait_for_timeout(1000)
    if not captured.get("url"):
        report.add(path, "rollback-request", "确认后没有发出回滚请求")
    return captured


def scenario_guide_modes(page: Any, report: Report, path: str = "/ops/guide") -> None:
    """新手引导三档（只读/半自动/一键）：切换要生效并记住。"""
    for mode in ("half", "auto", "readonly"):
        button = page.query_selector(f'.guide-mode[data-mode="{mode}"]')
        if button is None:
            report.add(path, "guide-mode", f"没有 {mode} 这一档按钮")
            continue
        button.click()
        page.wait_for_timeout(150)
        saved = page.evaluate("() => localStorage.getItem('fissue.guide.mode')")
        if saved != mode:
            report.add(path, "guide-mode", f"切到 {mode} 但 localStorage 是 {saved!r}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    out_dir = OUT_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    server = None
    tmp: tempfile.TemporaryDirectory | None = None
    meta: dict[str, Any] = {}
    if args.base_url:
        base_url = args.base_url.rstrip("/")
    else:
        tmp = tempfile.TemporaryDirectory(prefix="fissue-e2e-", ignore_cleanup_errors=True)
        base_url, server, meta = start_server(Path(tmp.name))

    report = Report(base_url=base_url, out_dir=out_dir)
    steps: list[tuple[str, str]] = []
    overlay_ok: bool | None = None

    def step(
        name: str, fn: Any, *fn_args: Any, shot: str | None = None, theme_pref: bool = True
    ) -> None:
        page = _new_page(
            browser_ref[0],
            width=args.width,
            theme="light",
            report=report,
            page_path=name,
            set_theme_pref=theme_pref,
        )
        try:
            page.goto(f"{base_url}{name}", wait_until="load", timeout=20000)
            page.wait_for_timeout(250)
            fn(page, report, name, *fn_args)
            if shot:
                _shot(page, out_dir, f"{shot}-light", report)
            steps.append((name, "ok"))
            if not args.quiet:
                print(f"  ✓ {name}")
        except Exception as exc:  # noqa: BLE001 - 单个场景失败不该中断其余
            steps.append((name, f"error: {exc}"))
            report.add(name, "scenario-crash", str(exc))
            print(f"  ✗ {name}: {exc}")
        finally:
            page.context.close()

    browser_ref: list[Any] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            browser_ref.append(browser)
            try:
                step("/", scenario_theme, shot="index", theme_pref=False)
                step("/pipeline", scenario_pipeline_views, shot="pipeline")
                step("/settings", scenario_settings_strategy, shot="settings")
                step("/ops", scenario_rollback, shot="ops")
                step("/ops/guide?repo_slug=demo/textkit", scenario_guide_modes, shot="guide")

                # 深色主题的关键页截图（给人看渲染用）
                for name in ("/", "/pipeline", "/settings", "/ops"):
                    page = _new_page(
                        browser, width=args.width, theme="dark", report=report, page_path=name
                    )
                    page.goto(f"{base_url}{name}", wait_until="load", timeout=20000)
                    page.wait_for_timeout(250)
                    _shot(page, out_dir, f"{name.strip('/') or 'index'}-dark", report)
                    page.context.close()
            finally:
                browser.close()
    finally:
        if server is not None and not args.keep:
            server.should_exit = True
        # 覆盖层必须在**清理临时目录之前**读：自建实例的覆盖层就落在临时目录里，
        # 先 cleanup 的话文件已经没了，只能报"未验证"——第一轮就是这么漏掉的。
        overlay = meta.get("overlay")
        if overlay and Path(overlay).exists():
            text = Path(overlay).read_text(encoding="utf-8")
            overlay_ok = "mode: fork" in text and "draft: true" in text
            if not overlay_ok:
                report.add(
                    "/settings", "overlay-write", f"覆盖层没写进三档：{text.strip()[:200]}"
                )
        (out_dir / "report.json").write_text(
            json.dumps(
                {
                    "base_url": base_url,
                    "meta": meta,
                    "steps": steps,
                    "overlay_ok": overlay_ok,
                    "findings": [f.__dict__ for f in report.findings],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if tmp is not None and not args.keep:
            time.sleep(0.3)
            tmp.cleanup()

    print()
    bad = [f for f in report.findings if f.severity == "error"]
    if bad:
        print(f"发现 {len(bad)} 个问题：")
        for finding in bad:
            print(f"  [{finding.severity}] {finding.page} · {finding.check}：{finding.detail}")
    else:
        print("全部交互场景通过。")
    if meta:
        print(
            f"实例：{meta.get('items')} 条演示数据、失败部署 #{meta.get('deployment_id')}、"
            f"覆盖层{'已写入' if overlay_ok else ('未验证' if overlay_ok is None else '未写入')}"
        )
    print(f"\n截图与报告：{out_dir}")
    if args.keep and meta:
        print(f"服务保持运行：{base_url}（Ctrl+C 结束）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真浏览器 E2E：完整栈 + 交互断言")
    parser.add_argument("--base-url", help="对着已运行实例跑（默认自建 demo 实例）")
    parser.add_argument("--width", type=int, default=1280, help="视口宽度")
    parser.add_argument("--keep", action="store_true", help="跑完保持服务运行，方便自己接着点")
    parser.add_argument("--quiet", action="store_true", help="不逐场景打印")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
