"""「当前正在跑的那一轮」的登记处：给界面一个**能停下它**的入口。

为什么需要它
------------
常驻服务的定时轮次在整轮期间持有写闸门，而它**不在 WebUI 的作业表里**——用户在页面上只看得到
「写操作进行中」，既不知道是谁、也无从停下（真机实测就卡在这里：最后只能 Ctrl-C 掉整个服务）。
视角那半已经补上了（见 ``lockfile.GATE_KINDS`` 与状态页），这里补「停得掉」那半。

为什么是进程内的模块级单例
--------------------------
``fissue serve --with-web`` 把 uvicorn 与 daemon 起在**同一个进程**里，所以 Web 请求与轮次
能看见同一份状态。独立 ``fissue web`` 进程里这份状态是空的——那时接口会如实回答「本进程没有在
跑的轮次」，而不是假装停得掉（跨进程的轮次确实停不了：那份 task 在别的进程的内存里）。

停止是**打断式**的，代价必须说清
--------------------------------
``request_stop()`` 走的是 ``task.cancel()``：轮次里的长步骤（一次 LLM 调用、一次沙盒执行）
都停在 await 上，取消能立刻生效；而「设个标志等它自己查」要把检查点一路埋进
fetch/eval/verify/fix 的每一层。代价是：正在进行的**单条**操作可能只完成一半
（例如正在建 PR）。所以调用方必须在界面上把这句话告诉用户，而不是给一个静默的按钮。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class RoundInfo:
    """一轮的对外描述（给状态页与任务页）。"""

    name: str
    started_at: datetime
    #: 已经跑了多少秒。
    seconds: float


class RoundControl:
    """登记「当前这一轮」的任务，并支持请求停止它。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[Any] | None = None
        self._name = ""
        self._since_wall: datetime | None = None
        self._since_mono: float | None = None

    @contextmanager
    def register(self, name: str) -> Iterator[None]:
        """把**当前任务**登记为「正在跑的这一轮」。daemon 的三类轮次都该包一层。

        只登记当前任务：轮次里的长步骤都跑在这个任务上，取消它才有意义。
        """
        self._task = asyncio.current_task()
        self._name = name
        self._since_wall = datetime.now(timezone.utc)
        self._since_mono = time.monotonic()
        try:
            yield
        finally:
            self._task = None
            self._name = ""
            self._since_wall = None
            self._since_mono = None

    def current(self) -> RoundInfo | None:
        """当前轮次；没有在跑的轮次就返回 ``None``。"""
        if self._task is None or self._since_mono is None or self._since_wall is None:
            return None
        return RoundInfo(
            name=self._name,
            started_at=self._since_wall,
            seconds=round(time.monotonic() - self._since_mono, 1),
        )

    def request_stop(self) -> RoundInfo | None:
        """请求停止当前轮次，返回**被停的那一轮**；没有可停的就返回 ``None``。

        调用方（HTTP 端点 / CLI）应当如实回答「已请求停止」：取消在下一个 await 点生效，
        此刻它可能还在收尾。**跨进程的轮次停不了**——这里只看得见本进程登记的那一轮。
        """
        info = self.current()
        if info is None or self._task is None:
            return None
        self._task.cancel()
        return info


#: 进程级单例（与 ``lockfile.WRITE_GATE`` 同一个思路：跨进程各一份）。
ROUNDS = RoundControl()
