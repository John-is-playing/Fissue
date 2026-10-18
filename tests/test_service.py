"""常驻服务测试：调度开关与循环启停。

这两个开关原先写在示例配置里却从未被代码读取——`schedule.enabled=false` 照样起
三个循环。这里守住「它真的生效」，同时用对照组确认闸门没有做成焊死的。
"""

from __future__ import annotations

from fissue.service.daemon import FissueService


def _silence_loops(service: FissueService, monkeypatch, started: list[str]) -> None:
    """把三个循环都换成可控替身：扫描循环记录并立刻请求停机，另外两个空转。"""

    async def fake_scan() -> None:
        started.append("scan")
        service.request_stop()

    async def noop() -> None:
        return None

    monkeypatch.setattr(service, "_scan_loop", fake_scan)
    monkeypatch.setattr(service, "_flush_loop", noop)
    monkeypatch.setattr(service, "_fix_loop", noop)


async def test_schedule_disabled_starts_no_loops(settings, monkeypatch) -> None:
    """`schedule.enabled=false` 时不启动定时循环。"""
    settings.schedule.enabled = False
    service = FissueService(settings)
    started: list[str] = []
    _silence_loops(service, monkeypatch, started)

    service.request_stop()          # 立刻退出 run_forever，别让测试挂住
    await service.run_forever()

    assert started == [], "关了调度就不该起任何循环"


async def test_schedule_enabled_starts_scan_loop(settings, monkeypatch) -> None:
    """对照组：开着调度时扫描循环要真的被起来。"""
    settings.schedule.enabled = True
    service = FissueService(settings)
    started: list[str] = []
    _silence_loops(service, monkeypatch, started)

    await service.run_forever()

    assert started == ["scan"]


async def test_write_loops_queue_instead_of_colliding(settings, monkeypatch, caplog) -> None:
    """scan 与 flush 同时到期：后者**排队等**，不撞闸门、也不被整轮 scan 饿死。

    回归（systemd 长跑实测，树莓派，scan 间隔 3600s / flush 间隔 60s）：scan 那一轮还在
    跑（fetch → 评测 → 验证）时，flush 每个周期都撞 `RuntimeContext.open(exclusive=True)`
    用的 `WRITE_GATE`——每 60s 记一条 `flush 轮次异常` 外加约 40 行 Traceback，而 flush
    在整轮 scan 期间**一次都没跑成**。

    所以这里同时守住两件事：两者不重叠（互斥还在），且 flush 不必等到下一个周期——
    scan 一轮结束就立刻轮到它。
    """
    import asyncio
    import contextlib
    import logging
    from types import SimpleNamespace

    from fissue import lockfile
    from fissue.pipeline.context import RuntimeContext

    settings.schedule.enabled = True
    service = FissueService(settings)

    events: list[str] = []
    flush_ran = asyncio.Event()

    @contextlib.asynccontextmanager
    async def fake_open(settings=None, *, exclusive=False, **kwargs):
        # 故意走**真**闸门：不绕开它，才测得出「谁在挡谁」
        async with lockfile.WRITE_GATE.hold_or_fail(action="抓取/评测/验证/修复"):
            yield SimpleNamespace(repo=object())

    async def slow_scan_round() -> dict:
        events.append("scan:enter")
        await asyncio.sleep(0.2)                    # 替代「分钟级的一轮」
        events.append("scan:exit")
        return {}

    async def record_flush(ctx, notifier) -> dict:
        events.append("flush")
        flush_ran.set()
        return {}

    monkeypatch.setattr(RuntimeContext, "open", fake_open)
    monkeypatch.setattr("fissue.service.daemon.Notifier", lambda *a, **kw: object())
    monkeypatch.setattr(service, "run_once", slow_scan_round)
    monkeypatch.setattr(service, "_flush_queues", record_flush)

    with caplog.at_level(logging.INFO):
        scan_task = asyncio.create_task(service._scan_loop())
        await asyncio.sleep(0.05)                   # 让 scan 先持住这一轮
        flush_task = asyncio.create_task(service._flush_loop())
        try:
            await asyncio.wait_for(flush_ran.wait(), timeout=2.0)
        finally:
            service.request_stop()
            await asyncio.gather(scan_task, flush_task, return_exceptions=True)

    assert "scan:exit" in events, f"scan 那轮没跑完：{events}"
    assert events.index("flush") > events.index("scan:exit"), (
        f"flush 必须等 scan 那轮结束才跑（互斥还在，且没有被饿死）：{events}"
    )
    assert "让位" in caplog.text, "排队时该留下一句说明——既不静默，也不打 Traceback"
    assert "Traceback" not in caplog.text, "让位是预期行为，不该打整段 Traceback"


# ---------------------------------------------------------------------------
# 验证步骤（常驻服务原先**没有**这一步）
# ---------------------------------------------------------------------------


def _fake_ctx(settings, repo, *, image_ready: tuple[bool, str] = (True, "ok")):
    """`_verify_pending` 需要的最小 ctx：沙盒只用到 `image_ready`。"""
    from types import SimpleNamespace

    class _Sandbox:
        @staticmethod
        def image_ready(image):  # noqa: ANN001, ANN202
            return image_ready

    class _LLM:
        # daemon 会按仓库换预算守卫（这是个全局单槽），替身里给个占位即可
        budget_guard = None

    return SimpleNamespace(
        settings=settings,
        repo=repo,
        sandbox=_Sandbox(),
        llm=_LLM(),
        budget_guard=lambda slug: None,
        all_repos=lambda: settings.enabled_repos(),
        usage_report=lambda *a, **k: {"total_tokens": 0, "cost_usd": 0.0},
    )


def _seed_evaluated(repo, *, number: int = 7, category=None):
    """塞一条已评测（status=evaluated）的 Issue。"""
    from fissue.models import Category, Evaluation, ItemType, Platform, Priority, RawItem, RepoRef

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number,
        item_type=ItemType.ISSUE, title="崩溃", body="复现",
    )
    repo.upsert_item(rid, item)
    repo.save_evaluation(
        item.key,
        Evaluation(category=category or Category.BUG, priority=Priority.TIER1, model="stub"),
    )
    return item


def _stub_stages(monkeypatch, ran: list[str], stage_result=None):
    """把 VerifyStage / PRVerifyStage 换成替身，记录被验证的 key。"""
    from fissue.models import ItemStatus
    from fissue.pipeline import stages as stages_mod

    class _FakeStage:
        def __init__(self, ctx) -> None:  # noqa: ANN001
            pass

        async def run(self, item, ev):  # noqa: ANN001, ANN202
            ran.append(item.key)
            if stage_result is not None:
                return stage_result
            return stages_mod.StageResult(key=item.key, status=ItemStatus.QUEUED)

    monkeypatch.setattr(stages_mod, "VerifyStage", _FakeStage)
    monkeypatch.setattr(stages_mod, "PRVerifyStage", _FakeStage)


async def test_daemon_verifies_evaluated_items(settings, repo, monkeypatch) -> None:
    """常驻服务要把 `evaluated` 的条目送去验证。

    回归：`daemon.run_once` 原先只做 fetch → eval → flush → fix，**没有验证步骤**，于是
    常驻模式下条目停在 evaluated、再也不会往下走——验证只能靠人工 `fissue verify` 或调 API。
    """
    from fissue.service.notify import Notifier

    item = _seed_evaluated(repo)
    ran: list[str] = []
    _stub_stages(monkeypatch, ran)

    service = FissueService(settings)
    out = await service._verify_pending(
        _fake_ctx(settings, repo), Notifier(settings.notify, settings, repo)
    )

    assert ran == [item.key], "evaluated 的条目应当被送去验证"
    assert out == {"verified": 1, "skipped": 0, "failed": 0}


async def test_daemon_skips_verification_when_image_is_missing(settings, repo, monkeypatch) -> None:
    """基础镜像不可用时跳过整步。

    daemon 里没人盯着，缺镜像时逐条「克隆 → LLM 生成验证器 → 沙盒报错」白烧 token 又白等
    （pipekit 实测 15 条全栽在这同一条上）。
    """
    from fissue.service.notify import Notifier

    _seed_evaluated(repo)
    ran: list[str] = []
    _stub_stages(monkeypatch, ran)

    service = FissueService(settings)
    out = await service._verify_pending(
        _fake_ctx(settings, repo, image_ready=(False, "镜像不可用")),
        Notifier(settings.notify, settings, repo),
    )

    assert ran == [], "镜像不可用时不该逐条去跑"
    assert out["verified"] == 0 and "镜像不可用" in out["skipped_reason"]


async def test_daemon_skips_issue_feature(settings, repo, monkeypatch) -> None:
    """Issue-FEATURE 不验证（没有缺陷可复现），直接置 labeled 等打标签——与 CLI 同口径。"""
    from fissue.models import Category, ItemStatus
    from fissue.service.notify import Notifier

    item = _seed_evaluated(repo, category=Category.FEATURE)
    ran: list[str] = []
    _stub_stages(monkeypatch, ran)

    service = FissueService(settings)
    out = await service._verify_pending(
        _fake_ctx(settings, repo), Notifier(settings.notify, settings, repo)
    )

    assert ran == [], "Issue-FEATURE 不该进验证器"
    assert out == {"verified": 0, "skipped": 1, "failed": 0}
    assert repo.get_item_row(item.key).status == ItemStatus.LABELED.value, "应当直接等打标签"


async def test_daemon_skips_verify_when_disabled(settings, repo, monkeypatch) -> None:
    """`schedule.verify_enabled=false` 时不碰验证这一步（验证交给人工 `fissue verify`）。"""
    from fissue.service.notify import Notifier

    _seed_evaluated(repo)
    ran: list[str] = []
    _stub_stages(monkeypatch, ran)
    settings.schedule.verify_enabled = False

    service = FissueService(settings)
    out = await service._verify_pending(
        _fake_ctx(settings, repo), Notifier(settings.notify, settings, repo)
    )

    assert ran == [], "关掉之后一条都不该验"
    assert out["verified"] == 0 and out["skipped_reason"] == "schedule.verify_enabled=false"


async def test_daemon_respects_the_verify_batch_limit(settings, repo, monkeypatch) -> None:
    """`verify_batch_limit` 决定一轮最多验多少条（积压留给后续轮次）。"""
    from fissue.service.notify import Notifier

    for n in (1, 2, 3):
        _seed_evaluated(repo, number=n)
    ran: list[str] = []
    _stub_stages(monkeypatch, ran)
    settings.schedule.verify_batch_limit = 2

    service = FissueService(settings)
    out = await service._verify_pending(
        _fake_ctx(settings, repo), Notifier(settings.notify, settings, repo)
    )

    assert len(ran) == 2, f"上限 2 就不该验 3 条：{ran}"
    assert out["verified"] == 2


async def test_periodic_notifications_are_deduped_per_day_not_forever(settings, repo, monkeypatch) -> None:
    """周期通知（扫描完成 / 日报 / 预算告警）按**日**去重，而不是一辈子只发一次。

    回归：这三类通知的标题是**常量**（`扫描完成：owner/repo`、`Fissue 巡检日报`），而去重键原先
    只由「事件 + 条目 + 标题」组成 → 键永远相同 → `try_claim_notification` 第一次就把它占住，
    此后每轮都被判「已发过」静默丢弃。真跑里正是这个表现：三个仓库各收到过一条「扫描完成」，
    之后再没收到任何周期性告警。
    """
    from types import SimpleNamespace

    from fissue.config import NotifyChannel
    from fissue.service import notify as notify_mod
    from fissue.service.notify import Channel, Notifier

    sent: list[str] = []

    class _Fake(Channel):
        kind = "console"

        def __init__(self) -> None:
            super().__init__(NotifyChannel(type="console"))

        async def send(self, note):  # noqa: ANN001, ANN202
            sent.append(note.title)
            return True, ""

    settings.notify.enabled = True
    settings.notify.events = []
    notifier = Notifier(settings.notify, settings, repo)
    notifier.channels = [_Fake()]
    stats = SimpleNamespace(fetched=1, created=0, updated=1, unchanged=0)

    monkeypatch.setattr(notify_mod, "_periodic_scope", lambda: "2026-10-16")
    await notifier.notify_scan(repo_slug="psf/requests", stats=stats)
    await notifier.notify_scan(repo_slug="psf/requests", stats=stats)
    assert len(sent) == 1, "同一时间窗内重复的扫描通知应当去重"

    monkeypatch.setattr(notify_mod, "_periodic_scope", lambda: "2026-10-17")
    await notifier.notify_scan(repo_slug="psf/requests", stats=stats)
    assert len(sent) == 2, "换了一天必须重新发——原先常量标题让它一辈子只发一次"


async def test_run_once_wires_the_verify_step_between_eval_and_flush(settings, repo, monkeypatch) -> None:
    """`run_once` 的步骤顺序里必须有验证（回归：它原先压根没有这一步）。

    只验**接线**：把各步换成替身，确认 fetch → eval → verify → flush → fix 一步不落——
    这样将来有人重构掉这一行，用例会立刻发现。
    """
    from fissue.models import ItemType, Platform, RawItem, RepoRef
    from fissue.pipeline.fetcher import FetchStats
    from fissue.pipeline.stages import BatchResult
    from fissue.service import daemon as daemon_mod

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(
        rid,
        RawItem(platform=Platform.GITHUB, repo="psf/requests", number=1,
                item_type=ItemType.ISSUE, title="待评测", body="b"),
    )

    order: list[str] = []

    class _Fetcher:
        def __init__(self, ctx) -> None:  # noqa: ANN001
            pass

        async def fetch_many(self, repos, **kwargs):  # noqa: ANN001, ANN003, ANN202
            order.append("fetch")
            return FetchStats(repo="psf/requests")

    class _EvalStage:
        def __init__(self, ctx) -> None:  # noqa: ANN001
            pass

        async def run_batch(self, items, **kwargs):  # noqa: ANN001, ANN003, ANN202
            order.append("eval")
            return BatchResult(repo="psf/requests")

    monkeypatch.setattr(daemon_mod, "Fetcher", _Fetcher)
    monkeypatch.setattr(daemon_mod, "EvalStage", _EvalStage)

    service = FissueService(settings)
    for name in ("_verify_pending", "_flush_queues", "_run_fixes"):
        async def step(ctx, notifier, _name=name):  # noqa: ANN001, ANN202
            order.append(_name)
            return {}

        monkeypatch.setattr(service, name, step)

    result = await service.run_once()

    assert order[:2] == ["fetch", "eval"], f"前两步该是拉取与评测：{order}"
    assert order[2:] == ["_verify_pending", "_flush_queues", "_run_fixes"], \
        f"验证必须排在评测之后、flush 之前：{order}"
    assert set(result) >= {"fetch", "evaluated", "verify", "flush", "fix"}
