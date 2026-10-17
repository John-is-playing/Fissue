"""投递幂等：同一个 delivery id 只处理一次（认领 → 确认 / 回滚）。

为什么单独一个模块、而且**一处模块级可变状态都没有**：幂等账本是**每个 app 实例**的状态。
挂在模块级全局会跨 app、跨事件循环共享；把 ``id(settings)`` 之类当键更糟——地址会被回收复用，
新 app 会继承旧 app 的标志（评审里点名过这两条）。所以这里：

* 状态是**实例属性**，由 ``create_app`` 建一份挂在 app 上；
* 键是**投递标识字符串**，不用 ``id()``；
* 认领与确认/回滚都在**同一把锁**里完成——分两次独立加锁的话，并发的同一条投递会双双通过；
* 有 TTL 与**上限**，不会无界增长。

**时序**（这一条最要紧）：认领必须发生在**验签之后**。在验签之前认领，等于未认证的请求可以用一个
猜到的 delivery id 占坑，把真实的平台投递挡在门外。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class DeliveryLedger:
    """投递认领账本（每 app 一份）。

    用法：``claim`` 拿到 True 才继续处理；处理**结束**时按结果 ``confirm``（成功，TTL 内继续拒绝
    重复投递）或 ``rollback``（失败，放掉认领，让平台的重推还能被处理）。
    """

    #: ``seen`` 的记忆窗口——平台重推通常发生在数秒到数分钟内。
    ttl_seconds: float = 600.0
    #: ``seen`` 的上限，防止被灌爆内存（超了就丢最旧的）。
    max_entries: int = 4096

    _seen: OrderedDict[str, float] = field(default_factory=OrderedDict)
    _in_flight: set[str] = field(default_factory=set)
    #: 认领与确认共用这一把锁。
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def claim(self, key: str) -> bool:
        """认领一条投递：正在处理、或刚处理过 → ``False``（重复投递）。

        ``key`` 为空（平台没给投递标识）→ 不做去重，一律放行。
        """
        if not key:
            return True
        async with self._lock:
            now = time.monotonic()
            self._expire(now)
            if key in self._in_flight or key in self._seen:
                return False
            self._in_flight.add(key)
            return True

    async def confirm(self, key: str) -> None:
        """处理成功：拿掉 in-flight 并记进 ``seen``（TTL 内继续拒绝重复投递）。"""
        if not key:
            return
        async with self._lock:
            self._in_flight.discard(key)
            self._seen[key] = time.monotonic()
            self._seen.move_to_end(key)
            self._expire(time.monotonic())

    async def rollback(self, key: str) -> None:
        """处理失败：只拿掉 in-flight（**不记 seen**），让平台的重推还能被处理。"""
        if not key:
            return
        async with self._lock:
            self._in_flight.discard(key)

    def _expire(self, now: float) -> None:
        """惰性清理：``seen`` 按插入顺序，从最旧的开始丢。"""
        while self._seen:
            key, seen_at = next(iter(self._seen.items()))
            if now - seen_at <= self.ttl_seconds:
                break
            self._seen.popitem(last=False)
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
