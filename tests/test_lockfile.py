"""单实例锁：跨进程互斥、进程内可重入、拿不到锁要**立即报错**。

这个模块的存在理由是「别重复花钱、别互相写冲突」——所以它的用例也照这个来写：
真起一个子进程持锁（不靠 sleep 赌时序），再看本进程是不是**立刻**被拒。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from fissue.errors import FissueError
from fissue.lockfile import _lock_path, single_flight


def test_single_flight_is_reentrant_in_one_process(settings) -> None:
    """同进程重入要放行——否则 daemon 的「外层循环 + 内层操作」会把自己挡住。

    `flock` 是**按打开的文件描述符**计的，同进程再开一次真的会失败；所以这个用例
    守的是 `_hold_depth` 那层重入保护。
    """
    with single_flight(settings, action="外层"), single_flight(settings, action="内层"):     # 不该抛
        pass


def test_lock_file_records_holder_pid(settings) -> None:
    """锁文件里写下持有者 pid：拿不到锁的人由此能看出「是谁在跑」。"""
    with single_flight(settings, action="测试"):
        content = _lock_path(settings).read_text(encoding="utf-8")

    assert f"pid={os.getpid()}" in content


def test_failed_lock_file_write_does_not_leak_the_reentrancy_depth(settings, monkeypatch) -> None:
    """写锁文件那一步失败时**不能**把进程内深度留在 1——否则本进程此后再也不抢锁。

    回归：`_hold_depth = 1` 原先写在 `lseek/ftruncate/write` **之前**，而归零只在 `yield`
    之后。任一步抛错（磁盘满、只读数据目录、EBADF）时，flock 会被外层 `finally: os.close`
    释放，深度却留在 1 → 此后所有 `single_flight` 都命中「重入」旁路，两个进程可以同时写同一个库。
    """
    import fissue.lockfile as lock_mod

    real_write = os.write
    calls = {"n": 0}

    def flaky_write(fd, data):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky_write)
    with pytest.raises(OSError), single_flight(settings, action="测试"):
        pass

    assert lock_mod._hold_depth == 0, "失败路径不能把进程内重入深度留在 1"

    # 而且下一次要**真的重新拿锁**（重入旁路不会写锁文件）
    _lock_path(settings).write_text("", encoding="utf-8")
    with single_flight(settings, action="再来一次"):
        assert f"pid={os.getpid()}" in _lock_path(settings).read_text(encoding="utf-8")


def test_lock_is_refused_when_another_process_holds_it(settings) -> None:
    """真·跨进程：别的进程持锁时，本进程**立即报错**（不排队、不静默等待）。

    子进程直接 `flock` 同一个锁文件——测的是机制本身；用它的 stdout 作同步点，
    而不是 sleep 赌时序。
    """
    path = _lock_path(settings)
    path.touch()                                    # 子进程要以 r+ 打开，先确保存在
    code = (
        "import fcntl,sys,time;"
        "f=open(sys.argv[1],'r+');"
        "fcntl.flock(f,fcntl.LOCK_EX);"
        "print('held',flush=True);"
        "time.sleep(20)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held", "子进程没能先拿到锁"
        with pytest.raises(FissueError, match="正在别处运行"), single_flight(settings, action="测试"):
            pass                                # pragma: no cover - 不该走到
    finally:
        child.kill()
        child.wait(timeout=10)


def test_lock_released_after_use(settings) -> None:
    """用完要能再拿：否则一次失败会把这台机器永久锁死。"""
    with single_flight(settings, action="第一次"):
        pass
    with single_flight(settings, action="第二次"):
        pass


# ---------------------------------------------------------------------------
# 进程内写闸门（flock 管不到的那一半）
# ---------------------------------------------------------------------------


async def test_write_gate_is_reentrant_for_the_same_task() -> None:
    """同一任务重入必须放行。

    `fissue serve --with-web` 是「外层循环持锁 → 内层 `open(exclusive=True)`」的嵌套结构，
    闸门若不可重入就会把自己锁死；而它同时又要挡住同进程里另一处（uvicorn 的请求任务）。
    """
    from fissue.lockfile import WriteGate

    gate = WriteGate()
    async with gate.hold_or_fail(action="外层循环"), gate.hold_or_fail(action="内层 open"):    # 不该抛
        pass
    # 重入计数必须归零：否则之后谁都拿不到
    async with gate.hold_or_fail(action="之后"):
        pass


async def test_write_gate_blocks_a_different_task() -> None:
    """不同任务必须**立即**被拒（不排队）——同进程重复评测/重复打标签就靠这一道。"""
    import asyncio

    from fissue.lockfile import WriteGate

    gate = WriteGate()
    seen: list[str] = []

    async def web_request() -> None:
        try:
            async with gate.hold_or_fail(action="uvicorn 的 /queues/flush"):
                seen.append("BAD：不该拿到闸门")     # pragma: no cover
        except FissueError:
            seen.append("blocked")

    async with gate.hold_or_fail(action="daemon 的 flush 循环"):
        await asyncio.create_task(web_request())
        assert seen == ["blocked"], f"不同任务必须被挡住：{seen}"
