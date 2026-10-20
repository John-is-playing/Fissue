"""任务事件流：Job 结构化事件、观察槽接线、LLM 流式路径。

覆盖三层：
* ``Job`` 本身实现 :class:`observers.JobSink`（事件记录与上限）；
* ``JobRegistry._run`` 用 ContextVar 把工作体内的「当前任务」暴露给流水线层；
* ``LLMClient`` 挂上观察桥后走 SSE 流式（重试语义与非流式一致），
  ``RuntimeContext.create`` 自动按上下文接线。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx

from fissue.ai.client import LLMClient
from fissue.config import LLMConfig
from fissue.net import httpx_verify
from fissue.observers import current_job_sink
from fissue.web.jobs import (
    CANCELED,
    DONE,
    FAILED,
    MAX_EVENTS,
    MAX_LLM_TEXT,
    Job,
    JobRegistry,
)

# ---------------------------------------------------------------------------
# Job：结构化事件
# ---------------------------------------------------------------------------


def test_job_records_structured_events() -> None:
    job = Job(id="abc12345", kind="evaluate")
    job.report("开始评测")
    job.llm_start(purpose="evaluation", model="test-model")
    job.llm_delta("part ")
    job.llm_delta("two")
    job.llm_end(model="test-model", from_fallback=False, usage={"total_tokens": 5})
    job.tool("验证器", "base 验证：已复现", ok=True)

    assert [e["type"] for e in job.events] == ["log", "llm", "tool"]
    llm_ev = job.events[1]
    assert llm_ev["text"] == "part two"
    assert llm_ev["done"] is True
    assert llm_ev["usage"] == {"total_tokens": 5}
    # 原 log 保留（列表页 / 状态页 / 吐司读它）
    assert job.log == ["开始评测"]
    assert job.to_dict()["events"][1]["text"] == "part two"


def test_job_caps_events_and_llm_text() -> None:
    job = Job(id="cap00001", kind="scan")
    for i in range(MAX_EVENTS + 50):
        job.report(f"line {i}")
    assert len(job.events) == MAX_EVENTS
    # 从头裁，末尾是最新
    assert job.events[-1]["text"] == f"line {MAX_EVENTS + 49}"

    job.llm_start(purpose="p", model="m")
    job.llm_delta("z" * (MAX_LLM_TEXT + 100))
    text = job.events[-1]["text"]
    assert len(text) == MAX_LLM_TEXT
    assert text.endswith("z" * 100)


def test_begin_and_finish_round(settings) -> None:  # noqa: ANN001 - conftest fixture
    reg = JobRegistry(settings)
    job = reg.begin_round("常驻服务：测试轮次")
    assert job.kind == "round"
    assert job.status == "running"
    assert reg.to_dict()["current"] is not None

    reg.finish_round(job, status=DONE, result={"repos": 1})
    assert job.status == DONE
    assert job.result == {"repos": 1}
    assert reg.to_dict()["current"] is None

    # 重复收尾是幂等的（daemon 的异常分支与正常分支可能都想收尾）
    reg.finish_round(job, status=FAILED, error="x")
    assert job.status == DONE
    reg.finish_round(job, status=CANCELED)
    assert job.status == DONE
    assert job.result == {"repos": 1}


# ---------------------------------------------------------------------------
# JobRegistry._run：观察槽的置上 / 复位
# ---------------------------------------------------------------------------


async def test_registry_run_exposes_the_job_as_sink(settings) -> None:  # noqa: ANN001
    reg = JobRegistry(settings)
    job = Job(id="wiring01", kind="scan")
    seen: dict[str, Any] = {}

    async def work(j: Job) -> dict[str, Any]:
        seen["sink"] = current_job_sink.get()
        current_job_sink.get().tool("测试", "动作", ok=True)
        return {"ok": 1}

    await reg._run(job, work)
    assert seen["sink"] is job
    assert job.result == {"ok": 1}
    assert any(e["type"] == "tool" for e in job.events)
    # _run 结束后必须复位，否则复用该任务上下文的下一个工作体会看到死槽
    assert current_job_sink.get() is None


# ---------------------------------------------------------------------------
# LLM 流式：delta 推给观察槽
# ---------------------------------------------------------------------------


class _RecSink:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def llm_start(self, *, purpose: str, model: str) -> None:
        self.events.append(("start", purpose, model))

    def llm_delta(self, text: str) -> None:
        self.events.append(("delta", text))

    def llm_end(self, *, model: str, from_fallback: bool, usage: dict[str, Any] | None) -> None:
        self.events.append(("end", model, from_fallback, usage))

    def tool(self, name: str, detail: str, *, ok: bool | None = None) -> None:
        self.events.append(("tool", name, detail, ok))


def _default_cfg() -> LLMConfig:
    return LLMConfig(
        api_key="test-key",
        base_url="https://llm.invalid/v1",
        model="test-model",
        max_retries=2,
        timeout_seconds=5,
    )


def _sse(content_chunks: list[str], *, usage: dict[str, Any] | None = None) -> httpx.Response:
    """拼一个合法的 SSE 流响应（含 include_usage 的尾帧与 [DONE]）。"""
    frames: list[dict[str, Any]] = [
        {"choices": [{"delta": {"role": "assistant", "content": ""}, "index": 0}]}
    ]
    for i, piece in enumerate(content_chunks):
        frame: dict[str, Any] = {
            "choices": [{"delta": {"content": piece}, "index": 0}]
        }
        if i == len(content_chunks) - 1:
            frame["choices"][0]["finish_reason"] = "stop"
        frames.append(frame)
    if usage is not None:
        frames.append({"choices": [], "usage": usage})
    body = "".join(f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames)
    body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def _llm(responses: list[httpx.Response]):
    """LLMClient + MockTransport；返回 (llm, calls, sink)。"""
    calls: list[dict[str, Any]] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return queue.pop(0)

    # 显式 verify：项目守卫（test_net）要求每个 httpx 客户端都传——MockTransport 不会
    # 真连网，但约定就是约定，也防未来换成真 transport 时掉坑。
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), verify=httpx_verify())
    llm = LLMClient(_default_cfg(), client=client)
    sink = _RecSink()
    llm.sink = sink
    return llm, calls, sink


async def test_streaming_pushes_deltas_and_usage_to_sink() -> None:
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    llm, calls, sink = _llm(responses=[_sse(["Hello ", "**world**"], usage=usage)])
    resp = await llm.chat([{"role": "user", "content": "hi"}], purpose="test-purpose")

    assert resp.content == "Hello **world**"
    assert sink.events[0] == ("start", "test-purpose", "test-model")
    assert ("delta", "Hello ") in sink.events
    assert ("delta", "**world**") in sink.events
    assert sink.events[-1] == ("end", "test-model", False, usage)
    payload = calls[0]
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


async def test_streaming_json_mode_falls_back_when_endpoint_rejects() -> None:
    reject = httpx.Response(400, content=b'{"error": "response_format is not supported"}')
    ok = _sse(['{"ok":', " true}"])
    llm, calls, _sink = _llm(responses=[reject, ok])
    data, _usage = await llm.chat_json(
        [{"role": "user", "content": "give me json"}], purpose="eval"
    )
    assert data == {"ok": True}
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]


async def test_streaming_retries_on_429(monkeypatch) -> None:  # noqa: ANN001
    r429 = httpx.Response(429, content=b"rate limited")
    ok = _sse(["ok"])
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fast(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast)
    llm, calls, sink = _llm(responses=[r429, ok])
    resp = await llm.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert len(calls) == 2
    assert sleeps  # 429 后确实退避过
    # 失败的第一次尝试要收尾（否则任务事件里会挂着一个永远「进行中」的 llm 事件）
    ends = [e for e in sink.events if e[0] == "end"]
    assert len(ends) == 2


async def test_non_streaming_path_untouched_when_no_sink() -> None:
    body = json.dumps(
        {
            "choices": [{"message": {"content": "plain"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()
    resp_once = httpx.Response(200, json=json.loads(body))
    llm, calls, _sink = _llm(responses=[resp_once])
    llm.sink = None  # 本条要测的是「无观察者 → 走原非流式路径」
    assert llm.sink is None
    resp = await llm.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "plain"
    assert "stream" not in calls[0]


# ---------------------------------------------------------------------------
# RuntimeContext：观察槽的自动接线
# ---------------------------------------------------------------------------


def test_runtime_context_picks_up_the_job_sink(settings) -> None:  # noqa: ANN001
    from fissue.pipeline.context import RuntimeContext, _LLMBridge

    job = Job(id="ctx12345", kind="scan")
    token = current_job_sink.set(job)
    try:
        ctx = RuntimeContext.create(settings)
    finally:
        current_job_sink.reset(token)
    assert ctx.job_sink is job
    assert isinstance(ctx.llm.sink, _LLMBridge)

    bare = RuntimeContext.create(settings)
    assert bare.job_sink is None
    assert isinstance(bare.llm.sink, _LLMBridge)


def test_llm_bridge_forwards_to_the_sink() -> None:
    from fissue.pipeline.context import _LLMBridge

    calls: list[tuple] = []
    sink = SimpleNamespace(
        llm_start=lambda **kw: calls.append(("start", kw)),
        llm_delta=lambda t: calls.append(("delta", t)),
        llm_end=lambda **kw: calls.append(("end", kw)),
    )
    bridge = _LLMBridge(SimpleNamespace(job_sink=sink))
    bridge.llm_start(purpose="p", model="m")
    bridge.llm_delta("x")
    bridge.llm_end(model="m", from_fallback=False, usage={"total_tokens": 1})
    assert calls == [
        ("start", {"purpose": "p", "model": "m"}),
        ("delta", "x"),
        ("end", {"model": "m", "from_fallback": False, "usage": {"total_tokens": 1}}),
    ]


def test_llm_bridge_is_a_noop_without_sink() -> None:
    from fissue.pipeline.context import _LLMBridge

    bridge = _LLMBridge(SimpleNamespace(job_sink=None))
    bridge.llm_start(purpose="p", model="m")  # 不得抛异常
    bridge.llm_delta("x")
    bridge.llm_end(model="m", from_fallback=False, usage=None)
