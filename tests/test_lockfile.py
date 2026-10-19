"""单实例锁：跨进程互斥、进程内可重入、拿不到锁要**立即报错**。

这个模块的存在理由是「别重复花钱、别互相写冲突」——所以它的用例也照这个来写：
真起一个子进程持锁（不靠 sleep 赌时序），再看本进程是不是**立刻**被拒。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from fissue import lockfile as lockfile_mod
from fissue.errors import FissueError
from fissue.lockfile import _lock_path, single_flight

#: 跨进程互斥建立在 `fcntl.flock` 上。没有它的平台（Windows）产品会**降级为不互斥**：
#: 既不建锁文件、也不写持有者 pid（见 `lockfile.py` 的 `fcntl is None` 分支，以及
#: `WriteGate` 那边仍在工作的事实）。下面几个用例守的是「真起一个子进程持锁 → 本进程
#: **立刻**被拒」，那个平台上这个语义不存在——跳过，而不是把断言改成「什么都不该发生」。
#:
#: 判据直接取产品自己的那个能力开关，而不是 `sys.platform == "win32"`：跳过条件与
#: 「产品能不能 flock」严格同源，不会在别的平台上误判。
requires_flock = pytest.mark.skipif(
    lockfile_mod.fcntl is None,
    reason="本机无 fcntl：single_flight 降级为不互斥（不建锁文件、不记持有者 pid）",
)


def test_single_flight_is_reentrant_in_one_process(settings) -> None:
    """同进程重入要放行——否则 daemon 的「外层循环 + 内层操作」会把自己挡住。

    `flock` 是**按打开的文件描述符**计的，同进程再开一次真的会失败；所以这个用例
    守的是 `_hold_depth` 那层重入保护。
    """
    with single_flight(settings, action="外层"), single_flight(settings, action="内层"):     # 不该抛
        pass


@requires_flock
def test_lock_file_records_holder_pid(settings) -> None:
    """锁文件里写下持有者 pid：拿不到锁的人由此能看出「是谁在跑」。"""
    with single_flight(settings, action="测试"):
        content = _lock_path(settings).read_text(encoding="utf-8")

    assert f"pid={os.getpid()}" in content


@requires_flock
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


@requires_flock
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


# ---------------------------------------------------------------------------
# 写闸门要说清「是谁在写、在做什么、写了多久」
# ---------------------------------------------------------------------------
#
# 真机踩到的困惑：界面上只说一句「写操作进行中」＋一个 pid。用户既不知道是谁在写、也判断
# 不出它是在正常干活还是卡住了（那次其实是常驻服务的定时轮次在正常推进，Token 一直在涨）。


async def test_gate_reports_who_is_writing_and_for_how_long() -> None:
    from fissue.lockfile import WriteGate

    gate = WriteGate()
    assert gate.describe().busy is False

    async with gate.hold_or_fail(action="常驻服务：扫描轮次", kind="round"):
        holder = gate.describe()
        assert holder.busy is True
        assert holder.who == "常驻服务：扫描轮次"
        assert holder.kind == "round"
        assert holder.since is not None
        assert holder.seconds is not None and holder.seconds >= 0

    assert gate.describe().busy is False, "退出后必须清干净，否则状态页会一直说有人在写"


async def test_gate_keeps_the_outer_label_on_reentry() -> None:
    """重入不换名字：可读名字该由**最外层**那句 action 决定。

    daemon 是「外层循环持锁 → 内层 open」的嵌套结构。若内层把名字覆盖成「抓取/评测/验证/修复」，
    状态页就会丢掉「常驻服务的扫描轮次」这条真正有用的信息。
    """
    from fissue.lockfile import WriteGate

    gate = WriteGate()
    # 一道 `async with` 里同时握住两层（内层是重入）——这正是 daemon 那套「外层循环持锁 →
    # 内层 open」的结构。
    async with (
        gate.hold_or_fail(action="常驻服务：扫描轮次", kind="round"),
        gate.hold_or_fail(action="抓取/评测/验证/修复"),
    ):
        holder = gate.describe()
        assert holder.who == "常驻服务：扫描轮次"
        assert holder.kind == "round"


def test_gate_describes_a_foreign_owner_without_crashing() -> None:
    """有人直接塞 `_owner`（测试替身 / 老路径）时要退回旧文案，而不是崩。"""
    from fissue.lockfile import WriteGate

    gate = WriteGate()
    gate._owner = object()          # type: ignore[assignment]  # noqa: SLF001 —— 故意的：模拟「有 owner 但没登记标签」
    holder = gate.describe()
    assert holder.busy is True
    assert "pid=" in holder.who
    assert holder.kind == "write"


async def test_probe_write_lock_returns_label_and_kind(settings) -> None:
    """探测函数要连着种类一起回——页面靠它区分「网页作业」与「常驻轮次」。"""
    from fissue.lockfile import WRITE_GATE, probe_write_lock

    assert probe_write_lock(settings).busy is False

    async with WRITE_GATE.hold_or_fail(action="作业 flush job-1", kind="job"):
        holder = probe_write_lock(settings)
        assert holder.busy is True
        assert holder.who == "作业 flush job-1"
        assert holder.kind == "job"
        assert holder.seconds is not None

    assert probe_write_lock(settings).busy is False


@requires_flock
def test_lock_file_records_who_and_since(settings) -> None:
    """拿不到锁的人从锁文件里要能读到「谁在跑、什么时候开始的」，而不是只有一个 pid。"""
    with single_flight(settings, action="抓取/评测/验证/修复"):
        text = _lock_path(settings).read_text(encoding="utf-8")

    assert f"pid={os.getpid()}" in text
    assert "开始于" in text
    assert "抓取/评测/验证/修复" in text, "锁文件里要能看出对方在做什么"
