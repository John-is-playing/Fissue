"""常驻服务的子流程测试：预算守卫、全量兜底、告警、flush 派发、自动修复。

`daemon.py` 里的这些方法只在**长跑**中才会被执行到，人工点一次跑不到它们：
预算熔断错了会静默烧钱、flush 派发错了会丢条目、修复异常没兜住会让整个循环停摆。
现有 `test_service.py` 守的是「循环起没起来、验证有没有被漏掉」，这里补的是
「每一轮内部出岔子时，它到底怎么收场」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from fissue.errors import BudgetExceeded
from fissue.models import Category, Evaluation, ItemStatus, ItemType, Priority, RawItem, Scores
from fissue.service.daemon import FissueService

# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _Recorder:
    """记录被推送了哪些通知。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def notify_scan(self, *, repo_slug, stats) -> None:  # noqa: ANN001
        self.events.append(("scan", repo_slug))

    async def notify_budget(self, *, repo_key, used) -> None:  # noqa: ANN001
        self.events.append(("budget", repo_key))

    async def notify_high_importance(self, item, evaluation, *, url=None) -> bool:  # noqa: ANN001
        self.events.append(("high", (item.key, url)))
        return True

    async def notify_pr_created(self, item, attempt) -> None:  # noqa: ANN001
        self.events.append(("pr", attempt.pr_url))

    async def notify_needs_manual(self, item, reason, *, report_path=None) -> None:  # noqa: ANN001
        self.events.append(("manual", (item.key, reason)))


def _stub() -> Any:
    """通知器替身。

    返回类型是 `Any`：被测方法签名要求真实 `Notifier`，而这些用例只借用它的几个
    方法名。把替身标成 `Any`，就不必为了「传参能过类型检查」去伪造整个 Notifier。
    """
    return _Recorder()


def _row(last_scanned_at=None):  # noqa: ANN001, ANN202
    return SimpleNamespace(last_scanned_at=last_scanned_at)


def _ctx(settings, repo=None, *, usage=None, adapter=None, sandbox_ready=(True, "ok")):  # noqa: ANN001, ANN202
    class _Sandbox:
        @staticmethod
        def image_ready(image):  # noqa: ANN001, ANN202
            return sandbox_ready

    class _LLM:
        budget_guard = None

    async def _adapter_for_repo(cfg):  # noqa: ANN001, ANN202
        if adapter is not None:
            raise adapter
        return SimpleNamespace(
            item_url=lambda ref, number, itype: f"https://gh/{number}"
        )

    return SimpleNamespace(
        settings=settings,
        repo=repo,
        sandbox=_Sandbox(),
        llm=_LLM(),
        budget_guard=lambda slug: None,
        all_repos=lambda: settings.enabled_repos(),
        usage_report=lambda *a, **k: usage or {"total_tokens": 0, "cost_usd": 0.0},
        adapter_for_repo=_adapter_for_repo,
    )


def _eval(importance: int = 90) -> Evaluation:
    from fissue.models import DimensionScore

    return Evaluation(
        scores=Scores(importance=DimensionScore(score=importance)),
        category=Category.BUG,
        priority=Priority.TIER1,
        summary="看起来是真 Bug",
    )


def _item(number: int = 7, *, priority: Priority = Priority.TIER1) -> RawItem:
    from fissue.models import Platform

    item = RawItem(
        platform=Platform.GITHUB,
        repo="psf/requests",
        number=number,
        item_type=ItemType.ISSUE,
        title="崩溃",
        body="复现",
    )
    item.priority = priority
    return item


# ---------------------------------------------------------------------------
# 预算守卫
# ---------------------------------------------------------------------------


def test_budget_ok_blocks_when_hard_stop_and_tokens_exceeded(settings) -> None:
    settings.llm.budget.daily_tokens_per_repo = 100
    settings.llm.budget.hard_stop = True
    service = FissueService(settings)
    ctx = _ctx(settings, usage={"total_tokens": 101, "cost_usd": 0.0})

    assert service._budget_ok(ctx, settings.enabled_repos()[0], _stub()) is False


def test_budget_ok_allows_when_hard_stop_disabled(settings) -> None:
    """`hard_stop=false` 是「只告警、继续跑」：超了也不该把这一轮停掉。"""
    settings.llm.budget.daily_tokens_per_repo = 100
    settings.llm.budget.hard_stop = False
    service = FissueService(settings)
    ctx = _ctx(settings, usage={"total_tokens": 999, "cost_usd": 0.0})

    assert service._budget_ok(ctx, settings.enabled_repos()[0], _stub()) is True


def test_budget_ok_blocks_on_cost_overrun(settings) -> None:
    settings.llm.budget.daily_tokens_per_repo = 10_000
    settings.llm.budget.daily_usd_per_repo = 1.0
    settings.llm.budget.hard_stop = True
    service = FissueService(settings)
    ctx = _ctx(settings, usage={"total_tokens": 0, "cost_usd": 2.0})

    assert service._budget_ok(ctx, settings.enabled_repos()[0], _stub()) is False


def test_budget_ok_passes_under_both_limits(settings) -> None:
    service = FissueService(settings)
    ctx = _ctx(settings)
    assert service._budget_ok(ctx, settings.enabled_repos()[0], _stub()) is True


# ---------------------------------------------------------------------------
# 全量兜底
# ---------------------------------------------------------------------------


def test_need_full_rescan_disabled_when_days_is_zero(settings) -> None:
    settings.schedule.full_rescan_days = 0
    service = FissueService(settings)
    assert service._need_full_rescan(_ctx(settings, repo=None)) is False


def test_need_full_rescan_true_when_repo_never_scanned(settings) -> None:
    """从没扫过的仓库（或库里没登记）必须全量——否则它永远只有增量窗口。"""
    settings.schedule.full_rescan_days = 7
    service = FissueService(settings)

    class _Repo:
        @staticmethod
        def get_repo(slug):  # noqa: ANN001, ANN202
            return None

    assert service._need_full_rescan(_ctx(settings, repo=_Repo())) is True


def test_need_full_rescan_true_when_stale(settings) -> None:
    settings.schedule.full_rescan_days = 7
    service = FissueService(settings)
    old = datetime.now(timezone.utc) - timedelta(days=8)

    class _Repo:
        @staticmethod
        def get_repo(slug):  # noqa: ANN001, ANN202
            return _row(old)

    assert service._need_full_rescan(_ctx(settings, repo=_Repo())) is True


def test_need_full_rescan_handles_naive_timestamps(settings) -> None:
    """SQLite 读回来的 `last_scanned_at` 常是 naive 的，不能因此抛 TypeError。"""
    settings.schedule.full_rescan_days = 7
    service = FissueService(settings)
    recent_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    class _Repo:
        @staticmethod
        def get_repo(slug):  # noqa: ANN001, ANN202
            return _row(recent_naive)

    assert service._need_full_rescan(_ctx(settings, repo=_Repo())) is False


# ---------------------------------------------------------------------------
# 高重要性告警
# ---------------------------------------------------------------------------


async def test_alert_skips_results_without_evaluation(settings) -> None:
    service = FissueService(settings)
    rec: Any = _Recorder()
    results = [SimpleNamespace(key="github:a/b#1", evaluation=None)]

    await service._alert_high_importance(_ctx(settings), settings.enabled_repos()[0], results, rec)

    assert rec.events == []


async def test_alert_skips_below_threshold(settings) -> None:
    service = FissueService(settings)
    rec: Any = _Recorder()
    threshold = settings.evaluation.thresholds.alert_importance
    results = [SimpleNamespace(key="github:a/b#1", evaluation=_eval(importance=max(0, threshold - 1)))]
    ctx = _ctx(settings, repo=SimpleNamespace(get_item=lambda key: _item()))

    await service._alert_high_importance(ctx, settings.enabled_repos()[0], results, rec)

    assert rec.events == []


async def test_alert_skips_missing_item(settings) -> None:
    """评测结果指向的条目已被删掉时，不该因为 get_item 返回 None 就炸。"""
    service = FissueService(settings)
    rec: Any = _Recorder()
    results = [SimpleNamespace(key="github:a/b#1", evaluation=_eval(importance=95))]
    ctx = _ctx(settings, repo=SimpleNamespace(get_item=lambda key: None))

    await service._alert_high_importance(ctx, settings.enabled_repos()[0], results, rec)

    assert rec.events == []


async def test_alert_still_notifies_when_url_build_fails(settings) -> None:
    """拿不到条目链接不该让告警一起消失——少个链接比少条告警好。"""
    service = FissueService(settings)
    rec: Any = _Recorder()
    results = [SimpleNamespace(key="github:a/b#1", evaluation=_eval(importance=95))]
    ctx = _ctx(settings, repo=SimpleNamespace(get_item=lambda key: _item()), adapter=RuntimeError("no token"))

    await service._alert_high_importance(ctx, settings.enabled_repos()[0], results, rec)

    assert rec.events == [("high", ("github:psf/requests#7", None))]


async def test_alert_includes_item_url(settings) -> None:
    service = FissueService(settings)
    rec: Any = _Recorder()
    results = [SimpleNamespace(key="github:a/b#1", evaluation=_eval(importance=95))]
    ctx = _ctx(settings, repo=SimpleNamespace(get_item=lambda key: _item(number=7)))

    await service._alert_high_importance(ctx, settings.enabled_repos()[0], results, rec)

    assert rec.events == [("high", ("github:psf/requests#7", "https://gh/7"))]


# ---------------------------------------------------------------------------
# flush 派发
# ---------------------------------------------------------------------------


async def test_flush_queues_records_reason_when_not_due(settings, monkeypatch) -> None:
    """没到阈值不是错误：结果里要带原因，否则运维看不出「为什么没 flush」。"""
    import fissue.service.daemon as daemon_mod

    class _QM:
        def __init__(self, ctx) -> None:  # noqa: ANN001
            pass

        def should_flush(self, queue):  # noqa: ANN001, ANN202
            return SimpleNamespace(should_flush=False, reason="未到阈值")

        async def flush(self, queue, *, handler):  # noqa: ANN001, ANN202
            raise AssertionError("不该 flush")

    monkeypatch.setattr(daemon_mod, "QueueManager", _QM)
    monkeypatch.setattr(daemon_mod, "FlushProcessor", lambda ctx: SimpleNamespace(make_handler=lambda: None))

    info = await FissueService(settings)._flush_queues(_ctx(settings), _stub())

    assert info, "每个队列都该出现一次"
    for entry in info.values():
        assert entry == {"flushed": 0, "reason": "未到阈值"}


async def test_flush_queues_surfaces_per_queue_error(settings, monkeypatch) -> None:
    """单个队列 flush 失败要留在结果里（并继续下一个），不能让整轮抛出去。"""
    import fissue.service.daemon as daemon_mod

    class _QM:
        def __init__(self, ctx) -> None:  # noqa: ANN001
            pass

        def should_flush(self, queue):  # noqa: ANN001, ANN202
            return SimpleNamespace(should_flush=True, reason="数量到了")

        async def flush(self, queue, *, handler):  # noqa: ANN001, ANN202
            return SimpleNamespace(flushed=0, conclusions=[], error="LLM 超时")

    monkeypatch.setattr(daemon_mod, "QueueManager", _QM)
    monkeypatch.setattr(daemon_mod, "FlushProcessor", lambda ctx: SimpleNamespace(make_handler=lambda: None))

    info = await FissueService(settings)._flush_queues(_ctx(settings), _stub())

    assert all(entry["error"] == "LLM 超时" for entry in info.values())


async def test_flush_queues_reports_flushed_and_conclusions(settings, monkeypatch) -> None:
    import fissue.service.daemon as daemon_mod

    class _QM:
        def __init__(self, ctx) -> None:  # noqa: ANN001
            pass

        def should_flush(self, queue):  # noqa: ANN001, ANN202
            return SimpleNamespace(should_flush=True, reason="队列空")

        async def flush(self, queue, *, handler):  # noqa: ANN001, ANN202
            return SimpleNamespace(flushed=2, conclusions=["a", "b"], error=None)

    monkeypatch.setattr(daemon_mod, "QueueManager", _QM)
    monkeypatch.setattr(daemon_mod, "FlushProcessor", lambda ctx: SimpleNamespace(make_handler=lambda: None))

    info = await FissueService(settings)._flush_queues(_ctx(settings), _stub())

    first = next(iter(info.values()))
    assert first == {"flushed": 2, "conclusions": 2, "error": None, "reason": "队列空"}


# ---------------------------------------------------------------------------
# 自动修复
# ---------------------------------------------------------------------------


class _FixRepo:
    """`_run_fixes` 用到的仓库接口：认领残留 + 取候选。"""

    def __init__(self, candidates) -> None:  # noqa: ANN001
        self.candidates = candidates
        self.reclaimed: list[list[str]] = []
        self.statuses: dict[str, ItemStatus] = {}

    def reclaim_fixing_items(self, *, repo_slug):  # noqa: ANN001, ANN202
        self.reclaimed.append(["github:a/b#1"])
        return ["github:a/b#1"]

    def list_items(self, **kw):  # noqa: ANN003, ANN202
        return list(self.candidates)

    def set_item_status(self, key, status):  # noqa: ANN001, ANN202
        self.statuses[key] = status


class _FakeFixer:
    """`AutoFixer` 替身：按 key 决定这一条的结果。"""

    calls: list[str] = []

    def __init__(self, ctx) -> None:  # noqa: ANN001
        pass

    async def fix_item(self, item):  # noqa: ANN001, ANN202
        type(self).calls.append(item.key)
        number = getattr(item, "number", 0)
        if number == 1:
            return SimpleNamespace(pr_url="https://gh/pr/1", outcome=SimpleNamespace(value="pr_created"),
                                   error=None, report_path=None, branch="b", rounds=1)
        if number == 2:
            return SimpleNamespace(pr_url=None, outcome=SimpleNamespace(value="needs_manual"),
                                   error="改不动", report_path="/tmp/r.md", branch="b", rounds=1)
        if number == 3:
            raise BudgetExceeded("预算用完")
        raise RuntimeError("修崩了")


def _patch_fixer(monkeypatch, fixer_cls=None) -> None:
    import fissue.fixer.autofix as autofix_mod

    monkeypatch.setattr(autofix_mod, "AutoFixer", fixer_cls or _FakeFixer)


async def test_run_fixes_returns_skipped_when_disabled(settings) -> None:
    settings.auto_fix.enabled = False
    out = await FissueService(settings)._run_fixes(_ctx(settings), _stub())
    assert out == {"skipped": "auto_fix 未启用"}


async def test_run_fixes_skips_non_tier_candidates(settings, monkeypatch) -> None:
    """候选取回来还要再按优先级筛一次：状态对了但没定级的条目不该被自动改。"""
    settings.auto_fix.enabled = True
    _patch_fixer(monkeypatch)
    _FakeFixer.calls = []
    repo = _FixRepo([_item(1, priority=Priority.NONE)])

    out = await FissueService(settings)._run_fixes(_ctx(settings, repo=repo), _stub())

    assert out["attempted"] == 0
    assert _FakeFixer.calls == []


async def test_run_fixes_counts_pr_and_notifies(settings, monkeypatch) -> None:
    settings.auto_fix.enabled = True
    _patch_fixer(monkeypatch)
    _FakeFixer.calls = []
    rec: Any = _Recorder()
    repo = _FixRepo([_item(1)])

    out = await FissueService(settings)._run_fixes(_ctx(settings, repo=repo), rec)

    assert out == {"attempted": 1, "succeeded": 1, "failed": 0, "prs": ["https://gh/pr/1"]}
    assert ("pr", "https://gh/pr/1") in rec.events
    assert repo.reclaimed, "要先认领上次中断残留的 fixing 条目"


async def test_run_fixes_reports_needs_manual(settings, monkeypatch) -> None:
    settings.auto_fix.enabled = True
    _patch_fixer(monkeypatch)
    _FakeFixer.calls = []
    rec: Any = _Recorder()
    repo = _FixRepo([_item(2)])

    out = await FissueService(settings)._run_fixes(_ctx(settings, repo=repo), rec)

    assert out["failed"] == 1 and out["succeeded"] == 0
    assert ("manual", ("github:psf/requests#2", "改不动")) in rec.events


async def test_run_fixes_stops_repo_on_budget_exhaustion(settings, monkeypatch) -> None:
    """预算熔断要 `break`（本仓库剩的候选不再试），并推一条预算告警。"""
    settings.auto_fix.enabled = True
    _patch_fixer(monkeypatch)
    _FakeFixer.calls = []
    rec: Any = _Recorder()
    repo = _FixRepo([_item(3), _item(4)])

    out = await FissueService(settings)._run_fixes(_ctx(settings, repo=repo), rec)

    assert out["attempted"] == 0
    assert _FakeFixer.calls == ["github:psf/requests#3"], "熔断后不该再试下一条"
    assert ("budget", "psf/requests") in rec.events


async def test_run_fixes_continues_after_generic_failure(settings, monkeypatch) -> None:
    """单条修复抛异常不该拖垮整轮——后面的候选照样要试。"""
    settings.auto_fix.enabled = True
    _patch_fixer(monkeypatch)
    _FakeFixer.calls = []
    repo = _FixRepo([_item(4), _item(1)])

    out = await FissueService(settings)._run_fixes(_ctx(settings, repo=repo), _stub())

    assert _FakeFixer.calls == ["github:psf/requests#4", "github:psf/requests#1"]
    assert out["attempted"] == 1 and out["succeeded"] == 1


async def test_run_fixes_skips_repo_when_budget_exceeded(settings, monkeypatch) -> None:
    settings.auto_fix.enabled = True
    settings.llm.budget.daily_tokens_per_repo = 1
    settings.llm.budget.hard_stop = True
    _patch_fixer(monkeypatch)
    _FakeFixer.calls = []
    repo = _FixRepo([_item(1)])
    ctx = _ctx(settings, repo=repo, usage={"total_tokens": 999, "cost_usd": 0.0})

    out = await FissueService(settings)._run_fixes(ctx, _stub())

    assert out["attempted"] == 0 and repo.reclaimed == []


# ---------------------------------------------------------------------------
# 验证：重试与失败落库
# ---------------------------------------------------------------------------


async def test_verify_one_retries_then_returns_error_instead_of_raising(settings, monkeypatch) -> None:
    """重试用尽后返回带 error 的结果：一条验证失败不该把整轮打挂。"""
    service = FissueService(settings)
    attempts: list[int] = []

    class _Stage:
        async def run(self, item, evaluation):  # noqa: ANN001, ANN202
            attempts.append(1)
            raise TimeoutError("沙盒超时")

    async def _no_sleep(seconds):  # noqa: ANN001
        return None

    monkeypatch.setattr(service, "_sleep", _no_sleep)

    result = await service._verify_one(_Stage(), _item(), _eval(), max_retries=2)

    assert len(attempts) == 3, "初试 + 2 次重试"
    assert "TimeoutError" in result.error
    assert result.key == "github:psf/requests#7"


async def test_verify_one_returns_first_success(settings, monkeypatch) -> None:
    service = FissueService(settings)
    attempts: list[int] = []

    class _Stage:
        async def run(self, item, evaluation):  # noqa: ANN001, ANN202
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("瞬时故障")
            return SimpleNamespace(key=item.key, error=None, status=ItemStatus.QUEUED)

    async def _no_sleep(seconds):  # noqa: ANN001
        return None

    monkeypatch.setattr(service, "_sleep", _no_sleep)

    result = await service._verify_one(_Stage(), _item(), _eval(), max_retries=2)

    assert len(attempts) == 2
    assert result.error is None


class _VerifyRepo:
    """`_verify_pending` 需要的仓库接口。"""

    def __init__(self, items, evaluations) -> None:  # noqa: ANN001
        self.items = items
        self.evaluations = evaluations
        self.statuses: dict[str, ItemStatus] = {}

    def list_items(self, **kw):  # noqa: ANN003, ANN202
        return list(self.items)

    def latest_evaluation(self, key):  # noqa: ANN001, ANN202
        return self.evaluations.get(key)

    def set_item_status(self, key, status):  # noqa: ANN001, ANN202
        self.statuses[key] = status


async def test_verify_pending_marks_failed_after_retries_exhausted(settings, monkeypatch) -> None:
    item = _item()
    repo = _VerifyRepo([item], {item.key: _eval()})
    service = FissueService(settings)
    settings.verifier.item_retries = 1

    import fissue.pipeline.stages as stages_mod

    class _Stage:
        def __init__(self, ctx) -> None:  # noqa: ANN001
            pass

        async def run(self, item, evaluation):  # noqa: ANN001, ANN202
            raise RuntimeError("验证器跑不起来")

    monkeypatch.setattr(stages_mod, "VerifyStage", _Stage)
    monkeypatch.setattr(stages_mod, "PRVerifyStage", _Stage)

    async def _no_sleep(seconds):  # noqa: ANN001
        return None

    monkeypatch.setattr(service, "_sleep", _no_sleep)

    out = await service._verify_pending(_ctx(settings, repo=repo), _stub())

    assert out["failed"] == 1
    assert repo.statuses[item.key] is ItemStatus.FAILED, "重试用尽才落 FAILED（之后可 requeue）"


async def test_verify_pending_skips_item_without_evaluation(settings, monkeypatch) -> None:
    item = _item()
    repo = _VerifyRepo([item], {})           # 状态是 evaluated 但取不到评测结果
    service = FissueService(settings)

    out = await service._verify_pending(_ctx(settings, repo=repo), _stub())

    assert out["skipped"] == 1 and out["verified"] == 0
    assert item.key not in repo.statuses


# ---------------------------------------------------------------------------
# 轮次登记（Web 在跑时，轮次要能出现在任务列表里）
# ---------------------------------------------------------------------------


class _Registry:
    """`app.state.jobs` 的替身：记录轮次的开始与收尾。"""

    def __init__(self) -> None:
        self.finished: list[tuple[str, object, str | None]] = []
        self.job = SimpleNamespace(live=True)

    def begin_round(self, title):  # noqa: ANN001, ANN202
        self.job = SimpleNamespace(live=True, title=title)
        return self.job

    def finish_round(self, job, *, status, error=None, result=None):  # noqa: ANN001, ANN202
        job.live = False
        self.finished.append((status, result, error))


def _service_with_registry(settings) -> tuple[FissueService, _Registry]:
    service = FissueService(settings)
    registry = _Registry()
    service.app = SimpleNamespace(state=SimpleNamespace(jobs=registry))
    return service, registry


async def test_run_once_marks_round_done(settings, monkeypatch) -> None:
    from fissue.web.jobs import DONE

    service, registry = _service_with_registry(settings)

    async def _round(*, round_name="服务轮次"):  # noqa: ANN001, ANN202
        return {"ok": 1}

    monkeypatch.setattr(service, "_run_round", _round)

    assert await service.run_once() == {"ok": 1}
    assert registry.finished == [(DONE, {"ok": 1}, None)]


async def test_run_once_marks_round_failed_and_reraises(settings, monkeypatch) -> None:
    """轮次异常要既记进任务（界面看得到失败），又照常抛给调用方（日志 / 上层处理）。"""
    from fissue.web.jobs import FAILED

    service, registry = _service_with_registry(settings)

    async def _round(*, round_name="服务轮次"):  # noqa: ANN001, ANN202
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(service, "_run_round", _round)

    with pytest.raises(RuntimeError):
        await service.run_once()
    status, _, error = registry.finished[0]
    assert status == FAILED
    assert error is not None and "LLM 挂了" in error


async def test_run_once_marks_round_canceled_and_reraises(settings, monkeypatch) -> None:
    """取消不是失败：界面上要能区分「我点了停止」和「它自己崩了」。"""
    import asyncio

    from fissue.web.jobs import CANCELED

    service, registry = _service_with_registry(settings)

    async def _round(*, round_name="服务轮次"):  # noqa: ANN001, ANN202
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "_run_round", _round)

    with pytest.raises(asyncio.CancelledError):
        await service.run_once()
    assert registry.finished[0][0] == CANCELED


async def test_run_once_without_web_does_not_register(settings, monkeypatch) -> None:
    """纯 CLI 常驻模式没有界面可看，不必登记轮次。"""
    service = FissueService(settings)

    async def _round(*, round_name="服务轮次"):  # noqa: ANN001, ANN202
        return {"ok": 1}

    monkeypatch.setattr(service, "_run_round", _round)

    assert await service.run_once() == {"ok": 1}
    assert service.app is None


async def test_run_round_returns_early_without_repos(settings, monkeypatch) -> None:
    """没有启用仓库时不该去跑 fetch/评测——直接返回并记一条告警。"""
    import fissue.service.daemon as daemon_mod

    service = FissueService(settings)

    class _Ctx:
        def init_db(self) -> None:
            return None

        repo = SimpleNamespace()

        @staticmethod
        def all_repos():  # noqa: ANN202
            return []

    class _Open:
        def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):  # noqa: ANN204
            return _Ctx()

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
            return False

    monkeypatch.setattr(daemon_mod.RuntimeContext, "open", staticmethod(lambda *a, **kw: _Open()))

    assert await service._run_round() == {"repos": 0}


# ---------------------------------------------------------------------------
# 三个循环：异常隔离（一轮出岔子不该让循环退出）
# ---------------------------------------------------------------------------


def _stop_after_first_sleep(service, monkeypatch) -> None:
    """让 `_sleep` 第一次立即返回，第二次直接置停机——循环只跑一轮但走完收尾。"""
    calls: list[int] = []

    async def _sleep(seconds):  # noqa: ANN001
        calls.append(1)
        if len(calls) >= 2:
            service.request_stop()

    monkeypatch.setattr(service, "_sleep", _sleep)


async def test_scan_loop_swallows_writer_busy_and_keeps_going(settings, monkeypatch) -> None:
    """闸门被占是预期情况：记一条让位日志，下一轮继续，循环不能退出。"""
    from fissue.errors import WriterBusy

    service = FissueService(settings)
    calls: list[int] = []

    async def _run_once(*, round_name=""):  # noqa: ANN001, ANN202
        calls.append(1)
        raise WriterBusy("另一个进程在写")

    monkeypatch.setattr(service, "run_once", _run_once)
    _stop_after_first_sleep(service, monkeypatch)

    await service._scan_loop()

    assert len(calls) >= 1, "让位之后循环应当继续（第二轮被 _sleep 置停）"


async def test_scan_loop_survives_unexpected_exception(settings, monkeypatch) -> None:
    service = FissueService(settings)
    calls: list[int] = []

    async def _run_once(*, round_name=""):  # noqa: ANN001, ANN202
        calls.append(1)
        raise RuntimeError("抓取崩了")

    monkeypatch.setattr(service, "run_once", _run_once)
    _stop_after_first_sleep(service, monkeypatch)

    await service._scan_loop()          # 不抛即通过

    assert calls


async def test_flush_loop_swallows_writer_busy(settings, monkeypatch) -> None:
    import fissue.service.daemon as daemon_mod
    from fissue.errors import WriterBusy

    service = FissueService(settings)

    def _open(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise WriterBusy("扫描轮次在跑")

    monkeypatch.setattr(daemon_mod.RuntimeContext, "open", _open)
    _stop_after_first_sleep(service, monkeypatch)

    await service._flush_loop()


async def test_fix_loop_runs_one_round_and_stops_on_shutdown(settings, monkeypatch) -> None:
    import fissue.service.daemon as daemon_mod
    from fissue.errors import WriterBusy

    service = FissueService(settings)

    def _open(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise WriterBusy("有人占着")

    monkeypatch.setattr(daemon_mod.RuntimeContext, "open", _open)

    calls: list[int] = []

    async def _sleep(seconds):  # noqa: ANN001
        calls.append(1)
        if len(calls) >= 3:
            service.request_stop()

    monkeypatch.setattr(service, "_sleep", _sleep)

    await service._fix_loop()

    assert service.stop_event.is_set(), "停机后循环应当正常返回，而不是卡住"


# ---------------------------------------------------------------------------
# run_service
# ---------------------------------------------------------------------------


async def test_run_service_once_calls_run_once(settings, monkeypatch) -> None:
    import fissue.service.daemon as daemon_mod

    seen: list[str] = []

    class _Fake:
        def __init__(self, settings, *, with_web=False) -> None:  # noqa: ANN001
            seen.append(f"init:{with_web}")

        async def run_once(self) -> None:
            seen.append("once")

        async def run_forever(self) -> None:
            seen.append("forever")

    monkeypatch.setattr(daemon_mod, "FissueService", _Fake)

    await daemon_mod.run_service(settings, once=True)
    assert seen == ["init:False", "once"]


async def test_run_service_defaults_to_forever(settings, monkeypatch) -> None:
    import fissue.service.daemon as daemon_mod

    seen: list[str] = []

    class _Fake:
        def __init__(self, settings, *, with_web=False) -> None:  # noqa: ANN001
            self.with_web = with_web
            seen.append(f"init:{with_web}")

        async def run_once(self) -> None:
            seen.append("once")

        async def run_forever(self) -> None:
            seen.append("forever")

    monkeypatch.setattr(daemon_mod, "FissueService", _Fake)

    await daemon_mod.run_service(settings, with_web=True)
    assert seen == ["init:True", "forever"]


# ---------------------------------------------------------------------------
# 整轮编排：fetch → 评测 → 验证 → flush → 修复
# ---------------------------------------------------------------------------


class _RunRoundCtx:
    """`_run_round` 需要的 ctx：仓库 / 条目 / 用量都可由用例摆布。"""

    def __init__(self, repo, *, items, usage=None) -> None:  # noqa: ANN001
        self.repo = repo
        self._items = items
        self.usage = usage or {"total_tokens": 0, "cost_usd": 0.0}
        self.llm = SimpleNamespace(budget_guard=None)

    def init_db(self) -> None:
        return None

    def all_repos(self):  # noqa: ANN202
        return self._repos

    def usage_report(self, slug):  # noqa: ANN001, ANN202
        return self.usage

    def budget_guard(self, slug):  # noqa: ANN001, ANN202
        return None

    @property
    def repo_items(self):  # noqa: ANN202
        return self._items


class _Fetcher:
    calls: list[dict] = []

    def __init__(self, ctx) -> None:  # noqa: ANN001
        pass

    async def fetch_many(self, repos, *, full, concurrency):  # noqa: ANN001, ANN202
        type(self).calls.append({"count": len(repos), "full": full, "concurrency": concurrency})
        return SimpleNamespace(fetched=5, created=2, updated=1, unchanged=2)


class _EvalStage:
    behaviour: str = "ok"

    def __init__(self, ctx) -> None:  # noqa: ANN001
        pass

    async def run_batch(self, items, *, concurrency):  # noqa: ANN001, ANN202
        if type(self).behaviour == "budget":
            raise BudgetExceeded("预算用完")
        if type(self).behaviour == "boom":
            raise RuntimeError("模型 500")
        return SimpleNamespace(evaluated=len(items), results=[])


def _patch_round(monkeypatch, service, ctx, *, items=()):  # noqa: ANN001, ANN202
    """把整轮里的外部依赖都换成替身，只留 `_run_round` 自身的编排逻辑。"""
    import fissue.service.daemon as daemon_mod

    ctx._repos = service.settings.enabled_repos()
    ctx._items = items

    class _Open:
        def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):  # noqa: ANN204
            return ctx

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
            return False

    monkeypatch.setattr(daemon_mod.RuntimeContext, "open", staticmethod(lambda *a, **kw: _Open()))
    monkeypatch.setattr(daemon_mod, "Fetcher", _Fetcher)
    monkeypatch.setattr(daemon_mod, "EvalStage", _EvalStage)

    async def _verify(ctx, notifier):  # noqa: ANN001, ANN202
        return {"verified": 0}

    async def _flush(ctx, notifier):  # noqa: ANN001, ANN202
        return {"flushed": 0}

    async def _fixes(ctx, notifier):  # noqa: ANN001, ANN202
        return {"attempted": 0}

    monkeypatch.setattr(service, "_verify_pending", _verify)
    monkeypatch.setattr(service, "_flush_queues", _flush)
    monkeypatch.setattr(service, "_run_fixes", _fixes)
    return ctx


async def test_run_round_full_cycle(settings, monkeypatch) -> None:
    """一轮要把五步都走一遍，并把各步结果汇总进同一个 dict。"""
    from fissue.models import ItemStatus, Platform, RawItem

    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=1,
        item_type=ItemType.ISSUE, title="崩溃", body="复现",
    )
    repo = SimpleNamespace(
        list_items=lambda **kw: [item] if kw.get("status") is ItemStatus.NEW else [],
        get_repo=lambda slug: None,
    )
    service = FissueService(settings)
    _patch_round(monkeypatch, service, _RunRoundCtx(repo, items=[item]), items=[item])
    _Fetcher.calls = []
    _EvalStage.behaviour = "ok"

    out = await service._run_round(round_name="扫描轮次")

    assert out["fetch"] == {"fetched": 5, "created": 2, "updated": 1, "unchanged": 2}
    assert out["evaluated"] == 1
    assert out["verify"] == {"verified": 0}
    assert out["flush"] == {"flushed": 0}
    assert out["fix"] == {"attempted": 0}
    assert _Fetcher.calls == [{"count": 1, "full": True, "concurrency": 2}], "没扫过的仓库应走全量"
    assert service._last_scan is not None


async def test_run_round_skips_repo_without_new_items(settings, monkeypatch) -> None:
    repo = SimpleNamespace(
        list_items=lambda **kw: [],
        get_repo=lambda slug: None,
    )
    service = FissueService(settings)
    _patch_round(monkeypatch, service, _RunRoundCtx(repo, items=[]))
    _EvalStage.behaviour = "ok"

    out = await service._run_round()

    assert out["evaluated"] == 0


async def test_run_round_reports_budget_exhaustion_and_continues(settings, monkeypatch) -> None:
    from fissue.models import ItemStatus, Platform, RawItem

    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=1,
        item_type=ItemType.ISSUE, title="崩溃", body="复现",
    )
    repo = SimpleNamespace(
        list_items=lambda **kw: [item] if kw.get("status") is ItemStatus.NEW else [],
        get_repo=lambda slug: None,
    )
    service = FissueService(settings)
    _patch_round(monkeypatch, service, _RunRoundCtx(repo, items=[item]), items=[item])
    _EvalStage.behaviour = "budget"

    recorded: list[str] = []

    class _Notifier:
        def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
            pass

        async def notify_scan(self, *, repo_slug, stats):  # noqa: ANN001
            return None

        async def notify_budget(self, *, repo_key, used):  # noqa: ANN001
            recorded.append(repo_key)

    import fissue.service.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "Notifier", _Notifier)

    out = await service._run_round()

    assert recorded == ["psf/requests"], "熔断要推一条预算告警"
    assert out["evaluated"] == 0


async def test_run_round_survives_eval_exception(settings, monkeypatch) -> None:
    """单个仓库评测抛异常：记日志继续，整轮仍然要跑完后续步骤。"""
    from fissue.models import ItemStatus, Platform, RawItem

    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=1,
        item_type=ItemType.ISSUE, title="崩溃", body="复现",
    )
    repo = SimpleNamespace(
        list_items=lambda **kw: [item] if kw.get("status") is ItemStatus.NEW else [],
        get_repo=lambda slug: None,
    )
    service = FissueService(settings)
    _patch_round(monkeypatch, service, _RunRoundCtx(repo, items=[item]), items=[item])
    _EvalStage.behaviour = "boom"

    out = await service._run_round()

    assert out["evaluated"] == 0
    assert out["fix"] == {"attempted": 0}, "评测失败不该中断后面的 flush / 修复"


async def test_run_fixes_continues_when_candidate_query_fails(settings, monkeypatch) -> None:
    """取候选本身抛异常（库锁等）：跳过这个仓库，别让整轮修复停摆。"""
    settings.auto_fix.enabled = True
    _patch_fixer(monkeypatch)
    _FakeFixer.calls = []

    class _BrokenRepo:
        def reclaim_fixing_items(self, *, repo_slug):  # noqa: ANN001, ANN202
            raise RuntimeError("database is locked")

    out = await FissueService(settings)._run_fixes(_ctx(settings, repo=_BrokenRepo()), _stub())

    assert out["attempted"] == 0