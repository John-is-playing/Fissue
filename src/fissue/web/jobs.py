"""Web 控制面的长任务：把「评测 / 验证 / 修复 / 抓取 / flush」变成可轮询的作业。

为什么需要它
------------
这些操作动辄几分钟（评测一批要调几十次 LLM，验证要进沙盒跑用例，修复要来回几轮）。
原先页面上是一个**同步 fetch 干等**，界面既不知道进行到哪一步、也没有等待态的反馈，最后
用一个 ``alert()`` 把整坨 JSON 丢给用户。作业化之后：POST 立刻拿到一个 job id，前端按秒
轮询进度与日志，完成时用吐司给出结果摘要。

为什么是进程内的一张表
----------------------
``fissue web`` 与 ``fissue serve --with-web`` 都只起**一个** uvicorn worker
（见 ``cli.ops`` 的 uvicorn.run 与 ``service.daemon._web_loop``，都没有 workers 参数），
所以进程内的表就够了，不必引 Redis 或建库表。**代价要说清**：如果哪天有人用
``uvicorn --workers 4`` 起这个 app，轮询可能落到没有该作业的那个 worker 上、拿到 404。
那条约束由部署方式保证，代码这一层不假装支持。

与写闸门的关系（这条最关键）
----------------------------
作业**自己**全程握着写闸门：``WRITE_GATE``（进程内，按任务区分）＋ ``single_flight``
（跨进程的 flock）。不能靠「提交作业的那个请求」握——请求一返回锁就放了，而作业还在跑，
于是并发的另一次写就能同时进行，重复调 LLM、重复写平台标签。所以作业运行体在
:meth:`JobRegistry._run` 里自己进出闸门，拿不到就**快速失败**并把原因写进作业（前端能看到
「正在别处运行」而不是一个静默卡住的任务）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..errors import WriterBusy
from ..lockfile import WRITE_GATE, single_flight
from ..logging_setup import get_logger

log = get_logger(__name__)

#: 作业状态。
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
#: 被取消（用户点「取消」，或服务关闭时收尾）。与 failed 分开：取消是**预期**结果，
#: 不是故障，页面上不该显示成红色错误。
CANCELED = "canceled"

#: 仍在跑的状态。
LIVE = (QUEUED, RUNNING)

#: 单个作业保留的日志行数上限。长跑（比如 500 条的评测）会持续 report，
#: 不设上限既吃内存、又会让轮询响应越来越大。
MAX_LOG_LINES = 200

#: 保留的作业条数（含已结束的），供「最近任务」面板。超出按创建时间丢最旧的。
MAX_JOBS = 30

#: 关闭时等待作业收尾的秒数。
SHUTDOWN_GRACE_SECONDS = 3.0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class Job:
    """一次后台任务的记录。"""

    id: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = QUEUED
    progress: str = ""
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    #: 谁取消了它、为什么。由 :meth:`JobRegistry.cancel` / :meth:`JobRegistry.shutdown` 在
    #: 取消**之前**写上——取消是异步生效的（任务在下一个 await 点才退出），所以原因不能等
    #: 到 `_run` 里才判断。
    canceled_by: str = ""

    @property
    def live(self) -> bool:
        return self.status in LIVE

    def report(self, message: str) -> None:
        """记一行进度。工作函数里调它，前端下一次轮询就能看到。"""
        self.progress = message
        self.log.append(message)
        if len(self.log) > MAX_LOG_LINES:
            del self.log[: len(self.log) - MAX_LOG_LINES]

    def to_dict(self) -> dict[str, Any]:
        end = self.finished_at or datetime.now(timezone.utc)
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status,
            "progress": self.progress,
            "log": list(self.log),
            "result": self.result,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "duration_seconds": round((end - self.created_at).total_seconds(), 1),
            "live": self.live,
        }


#: 作业的工作体：拿到 job（用来 report）→ 返回可序列化的结果。
Work = Callable[[Job], Awaitable[dict[str, Any] | None]]


class JobRegistry:
    """进程内的作业表。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # -- 查询 -------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, *, limit: int = 20) -> list[Job]:
        """按创建时间倒序返回。"""
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def current(self) -> Job | None:
        """仍在跑的作业（至多一个）。"""
        live = [j for j in self._jobs.values() if j.live]
        return live[0] if live else None

    def to_dict(self) -> dict[str, Any]:
        cur = self.current()
        return {
            "jobs": [j.to_dict() for j in self.list()],
            "current": cur.to_dict() if cur else None,
        }

    # -- 提交与执行 -------------------------------------------------------

    def submit(self, kind: str, params: dict[str, Any], work: Work) -> Job:
        """登记并启动一个作业。

        同一进程内**同时只允许一个**作业：这些操作都会写外部仓库与库，并行跑只会重复
        调 LLM、重复写平台。已有作业在跑时抛 :class:`WriterBusy`（端点转成 409）。
        """
        busy = self.current()
        if busy is not None:
            raise WriterBusy(
                f"已有任务在运行（{busy.kind} #{busy.id}，{busy.progress or busy.status}），"
                "请等它结束后再提交。"
            )
        job = Job(id=uuid.uuid4().hex[:8], kind=kind, params=dict(params))
        self._jobs[job.id] = job
        self._prune()
        # 显式留引用：asyncio 只持弱引用，不存下来任务可能被 GC 掉。
        self._tasks[job.id] = asyncio.create_task(self._run(job, work), name=f"fissue-job-{job.id}")
        log.info("作业已提交：%s（%s）", job.id, kind)
        return job

    async def _run(self, job: Job, work: Work) -> None:
        job.status = RUNNING
        job.report("开始")
        try:
            # 两道闸门都在**作业自己的任务里**握：见模块开头「与写闸门的关系」。
            async with WRITE_GATE.hold_or_fail(action=f"作业 {job.kind} {job.id}", kind="job"):
                with single_flight(self.settings, action=f"作业 {job.kind} {job.id}"):
                    result = await work(job)
            job.result = result or {}
            job.status = DONE
            job.report("完成")
        except asyncio.CancelledError:
            # 取消是**预期**结果（用户点了取消 / 服务在关闭），所以记成 canceled 而不是 failed：
            # 前者不该在页面上显示成红色故障。原因由取消方在 `canceled_by` 里先写好——取消是
            # 异步生效的，任务要到下一个 await 点才真正退出，这里再去判断就晚了。
            job.status = CANCELED
            job.error = job.canceled_by or "任务已被取消"
            job.report(f"已取消：{job.error}")
            log.info("作业被取消：%s（%s）：%s", job.id, job.kind, job.error)
            raise
        except Exception as exc:
            job.status = FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            job.report(f"失败：{job.error}")
            log.warning("作业失败：%s（%s）：%s", job.id, job.kind, exc)
        finally:
            job.finished_at = datetime.now(timezone.utc)
            self._tasks.pop(job.id, None)

    def cancel(self, job: Job) -> Job:
        """请求取消一个仍在跑的作业。

        **请求**而不是「立即结束」：``task.cancel()`` 只是在任务的下一个 ``await`` 点投递
        ``CancelledError``。作业正卡在一次 LLM 调用或沙盒执行上时，要等那一步让出控制权才会
        真的退出，所以调用方（端点）应当如实回答「已请求取消」，让前端继续轮询到状态变成
        ``canceled``。写闸门也会在那时由 ``async with`` 的退出释放——不用手工解锁，
        否则很容易漏掉一条路径把闸门永久占住。

        调用方负责先判断「是否还在跑」；这里只做取消动作。
        """
        job.canceled_by = "用户手动取消"
        job.report("正在取消…")
        task = self._tasks.get(job.id)
        if task is not None:
            task.cancel()
        return job

    def _prune(self) -> None:
        """只留最近 ``MAX_JOBS`` 条**已结束**的；在跑的永不删。"""
        if len(self._jobs) <= MAX_JOBS:
            return
        finished = sorted(
            (j for j in self._jobs.values() if not j.live),
            key=lambda j: j.created_at,
        )
        for job in finished[: max(0, len(self._jobs) - MAX_JOBS)]:
            self._jobs.pop(job.id, None)

    # -- 关闭 -------------------------------------------------------------

    async def shutdown(self) -> None:
        """取消仍在跑的作业并等它们收尾。

        必须取消而不是不管：作业里可能正握着 flock，进程退出前给它一个跑 ``finally`` 的
        机会，否则只会留下「Task was destroyed but it is pending」的噪音与一个未释放的
        沙盒目录。
        """
        pending = [t for t in self._tasks.values() if not t.done()]
        if not pending:
            return
        log.info("关闭中：取消 %d 个未完成的作业", len(pending))
        for job in self._jobs.values():
            if job.live:
                job.canceled_by = "服务正在关闭，任务被取消"
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
