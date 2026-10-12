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
