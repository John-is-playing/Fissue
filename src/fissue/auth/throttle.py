"""登录失败退避（进程内内存态）。

为什么需要它：口令是低熵的，一个人一天能试几万次；而 TOTP 只有 6 位（100 万种）。
没有限速的话，这两样都能被在线爆破。

为什么是内存态：不引 Redis、不建表。**已知边界**（docs/AUTH.md §8 明确写下）：
多进程部署时各进程各算各的，`fissue serve --with-web` 这种单进程多 worker 场景
退化成「按 worker 计数」。真要多进程共享，得把它挪进数据库——那是一个独立话题，
不该顺手塞进这次改造里。
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable

#: 第一次触发锁定的时长（秒）。
DEFAULT_BASE_LOCK_SECONDS = 60.0
#: 锁定时长的上限（秒）——指数退避不能变成「锁一辈子」。
DEFAULT_MAX_LOCK_SECONDS = 3600.0


class LoginThrottle:
    """按 key（来源 IP / 用户名）计数，超过阈值后指数退避锁定。

    两个 key 都记：只按 IP 记挡不住「换 IP 打同一个账号」，只按用户名记挡不住
    「一个 IP 扫一堆账号」。任一维度过阈值即锁定。
    """

    def __init__(
        self,
        *,
        base_lock_seconds: float = DEFAULT_BASE_LOCK_SECONDS,
        max_lock_seconds: float = DEFAULT_MAX_LOCK_SECONDS,
    ) -> None:
        self._base = base_lock_seconds
        self._max = max_lock_seconds
        self._lock = threading.Lock()
        #: key → (连续失败次数, 锁定截止时刻 monotonic)
        self._state: dict[str, tuple[int, float]] = {}

    def locked_seconds(self, keys: Iterable[str]) -> int:
        """还要等多少秒（0 = 没锁）。取所有 key 里最长的那个。"""
        now = time.monotonic()
        with self._lock:
            remaining = max(
                (self._state.get(key, (0, 0.0))[1] - now for key in keys), default=0.0
            )
        return max(0, math.ceil(remaining))

    def record_failure(self, keys: Iterable[str], *, max_failures: int) -> int:
        """记一次失败，返回**本次之后**的锁定秒数（0 = 还没到阈值）。"""
        now = time.monotonic()
        locked = 0.0
        with self._lock:
            for key in keys:
                failures, locked_until = self._state.get(key, (0, 0.0))
                failures += 1
                if failures >= max_failures:
                    # 每多失败一次，锁定翻倍：60s → 120s → 240s … 封顶 1 小时。
                    over = failures - max_failures
                    locked_until = now + min(self._base * (2**over), self._max)
                self._state[key] = (failures, locked_until)
                locked = max(locked, locked_until - now)
        return max(0, math.ceil(locked))

    def reset(self, keys: Iterable[str]) -> None:
        """登录成功时清零。不清的话，一个人正常用着用着就会被自己之前的失败锁住。"""
        with self._lock:
            for key in keys:
                self._state.pop(key, None)
