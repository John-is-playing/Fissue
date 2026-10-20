"""任务观察槽：让流水线层把「正在做什么」报告给当前在跑的任务。

为什么用 ContextVar
-------------------
「有没有人在看」是运行时事实：Web 作业（``web.jobs.JobRegistry._run``）与常驻服务轮次
（``service.daemon``）在自己的工作体运行前把它置上；CLI / 独立脚本 / 单测不置。流水线层
（LLM 客户端、修复器、验证阶段）只读上下文变量决定是否上报——工作体、agent、自动修复
**一行都不用改**，避免逐层透传回调参数（那种做法很容易漏一条链路）。

没有人在看时的成本：每个方法一次 ``ContextVar.get()`` 加一次 None 判断，纳秒级——
CLI 路径近似零开销。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Protocol


class JobSink(Protocol):
    """单个任务的观察接口。所有方法都是同步且廉价的（往内存列表里 append）。

    实现者是 ``web.jobs.Job``（结构化类型；本模块不反向 import web，避免依赖循环）。
    """

    def llm_start(self, *, purpose: str, model: str) -> None:
        """一次 LLM 调用开始。此时正文为空，由 :meth:`llm_delta` 逐段补齐。"""

    def llm_delta(self, text: str) -> None:
        """流式正文增量（每块一次）。"""

    def llm_end(self, *, model: str, from_fallback: bool, usage: dict[str, Any] | None) -> None:
        """LLM 调用结束（正常或出错）。``usage`` 是响应 usage 段的 token 统计。"""

    def tool(self, name: str, detail: str, *, ok: bool | None = None) -> None:
        """一个工具动作（agent 读写文件、验证器运行、提 PR 等）。

        ``ok``：``None`` = 无成败含义（纯记录）；``True`` / ``False`` = 明确结果。
        """


#: 当前任务（作业 / 轮次）的观察槽。``None`` = 没有人在看。
current_job_sink: ContextVar[JobSink | None] = ContextVar("fissue_current_job_sink", default=None)
