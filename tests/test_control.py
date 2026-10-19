"""「当前轮次」的登记与停止（``fissue/service/control.py``）。

为什么单独有它：常驻服务的定时轮次在整轮期间持有写闸门，又**不在** WebUI 的作业表里——
用户在页面上看得到「写操作进行中」，却既不知道是谁、也无从停下（真机实测最后只能 Ctrl-C
停掉整个服务）。这一层补的就是「停得掉」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fissue.service.control import ROUNDS, RoundControl

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_current_is_empty_before_any_round() -> None:
    assert RoundControl().current() is None


def test_request_stop_without_a_round_returns_none() -> None:
    """没有在跑的轮次就如实返回 ``None``——端点据此回 409，而不是假装停掉了。"""
    assert RoundControl().request_stop() is None


async def test_register_reports_the_running_round() -> None:
    control = RoundControl()
    assert control.current() is None

    with control.register("扫描轮次"):
        info = control.current()
        assert info is not None
        assert info.name == "扫描轮次"
        assert info.seconds is not None and info.seconds >= 0

    assert control.current() is None, "退出后要清干净，否则页面会一直说有一轮在跑"


async def test_request_stop_cancels_the_round_task() -> None:
    """请求停止会**取消**那一轮的任务：长步骤都停在 await 上，取消立刻生效。"""
    control = RoundControl()
    seen: list[str] = []

    async def round_work() -> None:
        with control.register("扫描轮次"):
            try:
                await asyncio.sleep(30)          # 替代「一轮里的长步骤」
            except asyncio.CancelledError:
                seen.append("cancelled")
                raise

    task = asyncio.create_task(round_work())
    await asyncio.sleep(0.01)                    # 让它进到 register 里面

    stopped = control.request_stop()

    assert stopped is not None and stopped.name == "扫描轮次"
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == ["cancelled"]
    assert control.current() is None


def test_daemon_and_module_share_one_singleton() -> None:
    """daemon 与 Web 必须用**同一个**单例——同进程里才看得见彼此。"""
    from fissue.service import control as control_mod
    from fissue.service import daemon as daemon_mod

    assert daemon_mod.ROUNDS is control_mod.ROUNDS is ROUNDS


async def test_scan_loop_survives_a_manually_stopped_round(settings, monkeypatch) -> None:
    """被界面停掉的那一轮**不能把循环带走**——否则扫描器静默消失。

    真机场景：用户在任务页点「停止这一轮」。若那条循环任务因此结束，之后再也不会有扫描，
    而界面上什么都不会说。这条守的就是「停一轮 ≠ 停服务」。
    """
    from fissue.service import control as control_mod
    from fissue.service.daemon import FissueService

    service = FissueService(settings)
    rounds: list[str] = []

    async def fake_round(*, round_name: str = "") -> dict:
        rounds.append(round_name)
        if len(rounds) == 1:
            # 模拟「界面请求停止」：取消当前任务，CancelledError 会从下面的 await 抛出来。
            task = asyncio.current_task()
            assert task is not None, "这一段跑在任务里"
            task.cancel()
            await asyncio.sleep(0.01)
        else:
            service.stop_event.set()             # 第二轮：让循环正常退出
        return {}

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(service, "run_once", fake_round)
    monkeypatch.setattr(service, "_sleep", no_sleep)

    await asyncio.wait_for(service._scan_loop(), timeout=5)

    assert rounds == ["扫描轮次", "扫描轮次"], "第一轮被停后，循环必须继续跑下一轮"
    assert control_mod.ROUNDS.current() is None, "退出时登记要清干净"


def test_all_three_loops_register_and_swallow_cancellation() -> None:
    """三条循环都要做两件事：登记进 ``ROUNDS``、并**单独**接住 ``CancelledError``。

    只测扫描那条不够：另外两条是各自复制的一段代码，漏掉任何一处都会让**那一条**循环
    静默死掉（扫描 / flush / 修复之一再也不会回来，而界面上什么都不会说）。
    也得单独接：``CancelledError`` 自 Python 3.8 起是 ``BaseException``，
    那些 ``except Exception`` 吃不到它。
    """
    text = (REPO_ROOT / "src" / "fissue" / "service" / "daemon.py").read_text(encoding="utf-8")

    assert text.count("with ROUNDS.register(") == 3, "三条循环都要登记当前轮次"
    assert text.count("except asyncio.CancelledError:") == 3, "三条循环都要接住取消"
    for name in ("扫描轮次", "flush 轮次", "修复轮次"):
        assert f'ROUNDS.register("{name}")' in text
