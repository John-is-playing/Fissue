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

    return SimpleNamespace(
        settings=settings,
        repo=repo,
        sandbox=_Sandbox(),
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
