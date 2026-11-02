"""后台刷新的聚合状态缓存。

为什么有它
----------
数据库在远端（PostgreSQL）时，看板每渲染一次页面就要串行打十来个往返
（队列面板、全局概览、今日用量、仓库列表、抓取记录……），每个往返几十到
几百毫秒——切换一次页面就是几秒。而这些「背景数字」并不随用户的筛选而变，
只随作业推进缓慢变化，前台根本不需要「就在此刻」的值。

所以：一个**后台线程**每 ``REFRESH_SECONDS`` 秒把聚合数据整体重算一遍
（:func:`fissue.web.status.collect_aggregates`），前台**只读缓存、不做查询**。

语义约定
--------
* **stale-while-revalidate**：快照过期时，当前请求照旧立刻返回旧值，同时唤醒
  后台线程刷新——下一次请求（或前端下一次轮询）就能拿到新值。前台永不因为
  刷新而阻塞。
* **冷启动**：还没有任何快照时，:meth:`StatsCache.get` 同步算一次——只有
  启动后的第一个请求付这个成本。后台线程在 ``start()`` 时立刻开始首轮
  刷新，正常情况下用户打开页面时首份快照已就绪。
* **刷新失败**（数据库抖动、连接中断）：保留上一份快照并记日志，**绝不**把
  空 dict 或异常抛给前台——看板宁要「2 秒前的数字」，不要空白。
* **提前刷新**（bump）：作业结束时由 ``JobRegistry.on_complete`` 唤醒一次，
  让面板在作业刚完成后的第一个刷新周期内就反映变化，而不是干等周期到点。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

#: 后台刷新周期（秒）。状态页前端每 2 秒轮询一次——对齐它，
#: 面板上的数字最迟滞后一个轮询周期。
REFRESH_SECONDS = 2.0

#: 多久没人读就算「没人看」（秒）。看板是「有人的时候才需要新鲜」的东西：
#: 一个挂在服务器上的实例整夜没人打开页面，却仍按 2 秒一次重算十来次往返，
#: 那是纯白烧——远端数据库上尤其明显。
IDLE_AFTER_SECONDS = 60.0

#: 没人看时的刷新周期（秒）。不降到「完全停」的理由：作业还在跑、条目还在变，
#: 真有人回来时看到的应该是最多 30 秒前的数字，而不是昨天那份快照。
IDLE_INTERVAL = 30.0

#: 多久没人读就彻底不刷新（秒），等一次读取或作业完成再醒。
#: 取 10 分钟：足以覆盖「午休 / 开会 / 过夜」这类自然空档，又不会让缓存变成
#: 一份需要几分钟才能重新热起来的冷启动。
DORMANT_AFTER_SECONDS = 600.0


class StatsCache:
    """进程内聚合缓存：后台线程刷新，前台只读。

    ``build`` 是取数函数（无参，返回一个可 JSON 化的 dict）；它**在后台线程里**
    执行，因此不能假设自己跑在事件循环里——SQLAlchemy 引擎线程安全，
    :func:`fissue.web.status.collect_aggregates` 只开普通会话，满足这一点。
    """

    def __init__(
        self, build: Callable[[], dict[str, Any]], *, interval: float = REFRESH_SECONDS
    ) -> None:
        self._build = build
        self._interval = interval
        self._snapshot: dict[str, Any] | None = None
        self._refreshed_at: float = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._refreshing = False
        self._thread: threading.Thread | None = None
        #: 上次被前台读取的时刻（monotonic）。**空闲降频的唯一依据**：没人读，
        #: 说明这些「背景数字」此刻没人需要新鲜值。
        self._read_at: float = time.monotonic()
        #: bump 过一次、强制刷新一轮。用它而不是只置 wakeup：休眠期被唤醒时，
        #: 线程仍会按「没人读」判定要不要刷——作业刚跑完那一份必须刷出来。
        self._forced = False
        #: 成功完成的刷新次数——测试与排障用（「缓存到底有没有在动」）。
        self.build_count = 0

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        """启动后台刷新线程（幂等）。"""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="fissue-stats-cache", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停止后台线程（幂等）。关闭路径在 ``JobRegistry.shutdown`` 之前调用。"""
        self._stop.set()
        self._wakeup.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)

    # -- 后台 --------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            refresh, wait = self._next_action(time.monotonic())
            if refresh:
                self._refresh()
            # 睡满周期，或等一次提前唤醒（bump / 前台读取）。被唤醒后立即再判一轮：
            # 唤醒可能发生在「正在刷新」期间，event 已置位会让下一圈补上。
            self._wakeup.wait(timeout=wait)
            self._wakeup.clear()

    def _next_action(self, now: float) -> tuple[bool, float]:
        """决定「这一圈刷不刷、下一圈等多久」。

        抽成一个只依赖 ``now`` 的函数，是为了让「空闲降频」这条策略能被用例直接钉住
        ——否则只能靠 sleep 去猜后台线程的时序，测出来的是抖动不是行为。

        三档：
        * 刚被读过（``age < IDLE_AFTER_SECONDS``）：正常节奏，跟着前端轮询走；
        * 一分钟没人读：降到 ``IDLE_INTERVAL``，数字还在动，但不必每 2 秒一次；
        * 十分钟没人读：**完全不刷**，等一次读取或作业完成来唤醒。此时仍按
          ``IDLE_INTERVAL`` 醒来判一次，代价是一次内存判断，不是一次数据库往返。
        """
        forced = self._forced
        self._forced = False
        age = now - self._read_at
        if forced:
            # 作业刚结束：不管有没有人在看，这一份要刷出来，否则回来看到的是作业前的数字。
            return True, self._interval
        if age >= DORMANT_AFTER_SECONDS:
            return False, IDLE_INTERVAL
        if age >= IDLE_AFTER_SECONDS:
            return True, IDLE_INTERVAL
        return True, self._interval

    def _refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            snapshot = self._build()
            with self._lock:
                self._snapshot = snapshot
                self._refreshed_at = time.monotonic()
                self.build_count += 1
        except Exception as exc:  # noqa: BLE001 - 刷新失败保留旧快照，不打扰前台
            log.warning("聚合缓存刷新失败，保留上一份快照：%s: %s", type(exc).__name__, exc)
        finally:
            with self._lock:
                self._refreshing = False

    # -- 前台 --------------------------------------------------------------

    def get(self) -> dict[str, Any] | None:
        """取当前快照（冷启动且尚无快照时同步算一次）。

        有过期快照时**立刻返回旧值**并唤醒后台刷新（stale-while-revalidate）：
        前台永不阻塞在数据库往返上。
        """
        with self._lock:
            now = time.monotonic()
            snapshot = self._snapshot
            busy = self._refreshing
            # 这一次读取本身就是「有人在看」的证据：休眠中的缓存要因此醒过来，
            # 否则用户回到页面后得等一个 IDLE_INTERVAL 才看到新数字。
            was_dormant = (now - self._read_at) >= DORMANT_AFTER_SECONDS
            self._read_at = now
            overdue = snapshot is not None and (now - self._refreshed_at) >= self._interval
        if snapshot is not None:
            if (overdue or was_dormant) and not busy:
                self._wakeup.set()
            return snapshot
        # 冷启动：同步算一次，避免页面拿到空面板。后台线程通常早已预热，
        # 走到这里的一般只有启动瞬间的第一个请求。
        self._refresh()
        with self._lock:
            return self._snapshot

    def bump(self) -> None:
        """请求一次提前刷新（作业完成时调用）。

        只是置标记 + 置一个 event：后台线程在「当前刷新结束后的下一圈」立刻补刷。
        ``_forced`` 不能省——休眠期被唤醒时线程仍会按「没人读」判定要不要刷，
        而作业刚跑完那一份必须刷出来。若后台线程尚未启动（测试路径），这里是无害的空操作。
        """
        self._forced = True
        self._wakeup.set()
