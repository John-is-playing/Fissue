"""单实例锁：同一时刻只允许一个「会写外部仓库」的进程在跑。

为什么需要：daemon 与 CLI 同跑（或两个 CLI 同跑）会重复评测（白烧 token），并在同一个
SQLite 上互相写冲突。注意唯一键、确定性分支名与「只认 open 的复用」已经挡住了**重复条目
与重复 PR**，但**重复花钱**和写冲突它们挡不住。

为什么用文件锁而不是标记位 / pidfile：进程被 `kill -9` 时 `flock` 由内核释放，不会留下
需要人工清理的陈旧锁；pidfile 会。

锁的身份取自**数据库地址**：两个进程写同一个库才互斥，写不同库互不打扰。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from .config import Settings
from .errors import FissueError
from .logging_setup import get_logger

log = get_logger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 上不存在
    fcntl = None  # type: ignore[assignment]


#: 进程内的持有深度。**同进程重入不该再去抢同一把锁**——`flock` 是按「打开的文件描述符」
#: 计的，同一进程再开一次会真的失败，于是 daemon 的「外层循环 + 内层某操作」会把自己挡住。
_hold_depth = 0


def _lock_path(settings: Settings) -> Path:
    """锁文件放数据目录，文件名带上库地址的短哈希。"""
    key = hashlib.sha1(str(settings.database.url).encode("utf-8")).hexdigest()[:12]
    return settings.data_dir / f"fissue-{key}.lock"


@contextlib.contextmanager
def single_flight(settings: Settings, *, action: str = "本次操作") -> Iterator[None]:
    """独占执行；拿不到锁**立即报错**，不排队、也不静默等待。"""
    global _hold_depth

    if _hold_depth:  # 本进程已经持有同一把锁 → 直接放行（重入，不重复抢）
        _hold_depth += 1
        try:
            yield
        finally:
            _hold_depth -= 1
        return

    if fcntl is None:  # pragma: no cover - Windows
        log.warning("当前平台没有 flock：跳过互斥（并发跑只会重复评测、白烧开销）")
        yield
        return

    path = _lock_path(settings)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            os.lseek(handle, 0, os.SEEK_SET)
            holder = os.read(handle, 200).decode("utf-8", "replace").strip()
            detail = f"（当前持有者：{holder}）" if holder else ""
            raise FissueError(
                f"{action}正在别处运行，本次不再并发执行{detail}。"
                f"锁文件：{path}。同一时刻只允许一个会写仓库的进程——"
                "请等它结束，或先停掉常驻服务（fissue daemon）。"
            ) from exc
        # 把持有者写进锁文件：拿不到锁的人由此能看到「是谁在跑」，便于判断。
        os.lseek(handle, 0, os.SEEK_SET)
        os.ftruncate(handle, 0)
        os.write(handle, f"pid={os.getpid()}\n".encode())
        # `_hold_depth` 必须在**可能抛异常的那几步之后**才置 1，归零仍放在 yield 之后。
        # 否则 `lseek/ftruncate/write` 任一步抛错（磁盘满、只读数据目录、EBADF）时：flock 被
        # 外层 `finally: os.close` 释放，而进程内深度留在 1 → 此后本进程**所有** `single_flight`
        # 都命中「重入」旁路，再也不抢锁——于是两个进程可以同时写同一个库。
        _hold_depth = 1
        try:
            yield
        finally:
            _hold_depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


class WriteGate:
    """进程内的「写操作」串行闸门：同一任务可重入，不同任务互斥，拿不到立即报错。

    为什么 `flock` 之外还需要它：`flock` 解决的是**跨进程**互斥，而
    ``fissue daemon --web`` 把 uvicorn 起在**同一个进程**里（见
    ``service.daemon.FissueService._web_loop``）。此时 daemon 的
    ``_flush_loop`` 正持着 `flock`（并把它记在 `_hold_depth` 上），
    同进程的 ``/queues/flush``、``/actions/*`` 走的是重入旁路、直接放行——
    两个任务于是并行跑同一批条目：重复调 LLM（重复计费）、重复打平台标签。

    ``asyncio.Lock`` 在这里不能用，有两个具体原因：

    1. 它**不可重入**。daemon 是「外层循环持锁 → 内层 ``open(exclusive=True)``」
       的嵌套结构，用 asyncio.Lock 会把自己锁死（`_hold_depth` 那段注释记的正是
       这个坑）。
    2. 它会把事件循环记在自己身上。测试里每个用例一个新循环，被争用过的
       ``asyncio.Lock`` 换循环就会抛「bound to a different event loop」。

    需要的语义只有三条——同任务可重入、不同任务互斥、拿不到立即报错——用一个
    「拥有者 + 深度」就够，且与事件循环无关。检查与赋值之间没有 await，
    单线程事件循环下是原子的。
    """

    def __init__(self) -> None:
        self._owner: asyncio.Task | None = None
        self._depth = 0

    @contextlib.asynccontextmanager
    async def hold_or_fail(self, *, action: str = "本次写操作") -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is not None and task is self._owner:
            # 同一任务重入：daemon 的外层循环 + 内层 open，直接放行
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        if self._owner is not None:
            raise FissueError(
                f"{action}正在本进程内进行中，本次不再并发执行。"
                "同一时刻只允许一处会写外部仓库的操作——并发跑会重复调用 LLM 并重复写平台。"
            )
        self._owner = task
        self._depth = 1
        try:
            yield
        finally:
            self._owner = None
            self._depth = 0


#: 进程级单例（各进程一份；跨进程的互斥仍由 `single_flight` 的 flock 负责）。
WRITE_GATE = WriteGate()
