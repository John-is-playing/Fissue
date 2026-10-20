"""OpenAI 兼容大模型客户端。

特性
----
* 只依赖 ``/chat/completions``（OpenAI 兼容协议），换供应商只改 base_url + model。
* 并发受 ``llm.concurrency`` 限制；失败按指数退避重试，主模型连续失败可降级到 fallback。
* 每次调用都记录 token 与成本，写入 usage sink（供预算控制与统计）。
* 预算熔断：超出每仓库每日 token / 金额上限时抛 :class:`BudgetExceeded`。
* 结构化输出：优先用 ``response_format={"type": "json_object"}``，不支持时自动降级为
  从文本里提取 JSON（含 ```json 围栏）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..config import BudgetConfig, LLMConfig
from ..errors import BudgetExceeded, LLMError
from ..logging_setup import get_logger
from ..net import httpx_verify, is_ssl_error, ssl_help_text

log = get_logger(__name__)

Message = dict[str, str]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """一次调用的用量。"""

    purpose: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    success: bool = True

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    """模型返回。"""

    content: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)
    from_fallback: bool = False


def _merge_usage(total: Usage, part: Usage) -> Usage:
    """把一次调用的用量累加进累计值（重试多轮时逐次累计，便于计费与排障）。"""
    total.prompt_tokens += part.prompt_tokens
    total.completion_tokens += part.completion_tokens
    total.cost_usd += part.cost_usd
    total.model = part.model or total.model
    total.purpose = part.purpose or total.purpose
    total.success = total.success and part.success
    return total


#: 长度截断后的加码下限：一次不够就翻倍，且不低于这个值
_TOKEN_ESCALATE_FLOOR = 16384


def _escalate_tokens(current: int, ceiling: int) -> int:
    """长度截断后计算下一轮的 ``max_tokens``：先翻倍，再保证不低于 16384。

    推理长度随提示波动很大（同一提示实测 14207~17264 字符），固定预算迟早撞顶；
    截断说明「推理 + 正文」比预算长，翻倍并设下限能一次跳出多数截断。
    """
    nxt = max(int(current) * 2, _TOKEN_ESCALATE_FLOOR)
    return min(nxt, max(int(ceiling), int(current)))


UsageSink = Callable[[Usage], None | Awaitable[None]]
UsageGetter = Callable[[], dict[str, Any]]


# ---------------------------------------------------------------------------
# 预算控制
# ---------------------------------------------------------------------------


class BudgetGuard:
    """每仓库每日预算熔断。

    ``usage_getter`` 返回 ``{"total_tokens": …, "cost_usd": …}``（当日累计）。
    """

    def __init__(
        self,
        budget: BudgetConfig,
        *,
        usage_getter: UsageGetter | None = None,
        repo_label: str = "",
    ) -> None:
        self.budget = budget
        self.usage_getter = usage_getter
        self.repo_label = repo_label

    def check(self, *, estimated_tokens: int = 0) -> None:
        """调用前检查；超限则抛 :class:`BudgetExceeded`。"""
        if self.usage_getter is None:
            return
        try:
            used = self.usage_getter() or {}
        except Exception as exc:  # 统计失败不应阻断主流程
            log.warning("读取用量失败（忽略预算检查）：%s", exc)
            return

        tokens = int(used.get("total_tokens") or 0) + estimated_tokens
        cost = float(used.get("cost_usd") or 0.0)

        over_tokens = self.budget.daily_tokens_per_repo and tokens > self.budget.daily_tokens_per_repo
        over_cost = self.budget.daily_usd_per_repo and cost > self.budget.daily_usd_per_repo
        if not (over_tokens or over_cost):
            return

        detail = (
            f"仓库 {self.repo_label or '(全局)'} 已超当日预算："
            f"tokens={tokens}/{self.budget.daily_tokens_per_repo}，"
            f"cost=${cost:.4f}/${self.budget.daily_usd_per_repo}"
        )
        if self.budget.hard_stop:
            raise BudgetExceeded(detail)
        log.warning("%s（hard_stop=false，继续执行）", detail)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class LLMEmitter(Protocol):
    """LLM 流式输出观察者（可选；不设置时客户端完全不流式）。

    实现是 ``pipeline.context`` 里的桥：它把回调转给「当前任务槽」
    （见 ``observers``）。客户端自己不知道有没有人在看——那是运行时事实。
    """

    def llm_start(self, *, purpose: str, model: str) -> None: ...

    def llm_delta(self, text: str) -> None: ...

    def llm_end(self, *, model: str, from_fallback: bool, usage: dict[str, Any] | None) -> None: ...


class LLMClient:
    """异步 OpenAI 兼容客户端。"""

    def __init__(
        self,
        config: LLMConfig,
        *,
        usage_sink: UsageSink | None = None,
        budget_guard: BudgetGuard | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.usage_sink = usage_sink
        self.budget_guard = budget_guard
        self._semaphore = asyncio.Semaphore(max(1, config.concurrency))
        self._client = client
        self._owns_client = client is None
        self._json_mode_supported: bool | None = None
        #: 流式输出观察者。任务要实时展示 LLM 输出时挂上桥；CLI / 无观察 = None（走原路）。
        self.sink: LLMEmitter | None = None

    # -- 生命周期 ---------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json", "User-Agent": "Fissue/0.1"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=20.0),
                headers=headers,
                verify=httpx_verify(),
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- 核心调用 ---------------------------------------------------------

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        purpose: str = "",
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """发起一次对话补全。"""
        if not self.config.api_key:
            raise LLMError("缺少 LLM API Key（请设置 FISSUE_LLM_API_KEY）")

        models = [model or self.config.model]
        if not model and self.config.fallback_model and self.config.fallback_model != self.config.model:
            models.append(self.config.fallback_model)

        last_exc: Exception | None = None
        for idx, mdl in enumerate(models):
            try:
                return await self._chat_once(
                    messages,
                    model=mdl,
                    purpose=purpose,
                    json_mode=json_mode,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    from_fallback=idx > 0,
                )
            except BudgetExceeded:
                raise
            except Exception as exc:
                last_exc = exc
                if idx + 1 < len(models):
                    log.warning("模型 %s 调用失败，降级到 %s：%s", mdl, models[idx + 1], exc)
                    continue
                raise LLMError(f"模型调用失败（{mdl}）：{exc}") from exc
        raise LLMError(f"模型调用失败：{last_exc}")  # pragma: no cover

    async def _chat_once(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        purpose: str,
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
        from_fallback: bool,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        use_json = json_mode and self._json_mode_supported is not False
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        async with self._semaphore:
            if self.budget_guard is not None:
                self.budget_guard.check(estimated_tokens=_estimate_tokens(messages))
            data = await self._request(
                payload, use_json=use_json, purpose=purpose, from_fallback=from_fallback
            )

        if data is None:
            # JSON mode 不被支持 → 去掉后重试一次
            payload.pop("response_format", None)
            self._json_mode_supported = False
            async with self._semaphore:
                data = await self._request(
                    payload, use_json=False, purpose=purpose, from_fallback=from_fallback
                )

        if data is None:
            raise LLMError("模型返回为空")

        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"模型响应格式异常：{str(data)[:400]}") from exc

        usage_raw = data.get("usage") or {}
        prompt_tokens = int(usage_raw.get("prompt_tokens") or 0)
        completion_tokens = int(usage_raw.get("completion_tokens") or 0)
        usage = Usage(
            purpose=purpose,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=self.config.pricing.cost(prompt_tokens, completion_tokens),
        )
        await self._record(usage)

        log.debug(
            "LLM[%s] model=%s tokens=%d/%d cost=$%.5f",
            purpose, model, prompt_tokens, completion_tokens, usage.cost_usd,
        )
        return LLMResponse(
            content=content,
            model=model,
            usage=usage,
            raw=data,
            from_fallback=from_fallback,
        )

    async def _request(
        self,
        payload: dict[str, Any],
        *,
        use_json: bool,
        purpose: str,
        from_fallback: bool,
    ) -> dict[str, Any] | None:
        """按「有没有观察者在场」选传输方式，返回**同形状**的响应 dict。

        有观察者（``sink`` 已挂）→ SSE 流式，正文逐块推给任务事件；没有 → 原一次性
        POST。两条路返回同样的「非流式响应」结构，下游对 content / usage /
        finish_reason / tool_calls 的解析零改动。
        """
        if self.sink is None:
            return await self._post_with_retry(payload, use_json=use_json)
        return await self._stream_with_retry(
            payload,
            use_json=use_json,
            purpose=purpose,
            from_fallback=from_fallback,
        )

    @staticmethod
    def _parse_sse(line: str) -> dict[str, Any] | None:
        """解析一行 SSE 帧：``data: {...}`` → dict；``data: [DONE]`` / 空行 / 非 data → None。"""
        line = line.strip()
        if not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    async def _stream_with_retry(
        self,
        payload: dict[str, Any],
        *,
        use_json: bool,
        purpose: str,
        from_fallback: bool,
    ) -> dict[str, Any] | None:
        """流式 POST（SSE）＋退避重试，把流组装成「非流式响应」dict 返回。

        仅在 ``sink`` 已设置时使用（有任务要实时展示 LLM 输出）。重试语义与
        :meth:`_post_with_retry` 一致（同样的可重试错误、同样的退避）；流中断按网络
        错误处理、整次调用重发——已推出的部分正文留在任务事件里，作为「失败的第一次
        尝试」记录，这是最诚实的展示。

        ``usage`` 依赖 ``stream_options.include_usage`` 的最后一帧：兼容端点不支持该
        参数时会静默忽略，本次统计就是 0——**调用前**的预算检查（budget_guard.check）
        不受影响，只是事后统计偏少。
        """
        url = self.config.chat_completions_url
        # 与 _post_with_retry 同一道闸：max_retries=0 意味着一次请求都不发。
        if self.config.max_retries < 1:
            raise LLMError(
                f"llm.max_retries={self.config.max_retries} 不会发出任何请求（它表示总尝试次数，至少为 1）；"
                "想「不重试」请填 1。"
            )
        emitter = self.sink
        if emitter is None:
            # _request 只在 sink 在场时才分发到这里；这道闸只为类型收窄与防未来误用。
            raise LLMError("流式传输未挂观察桥（内部错误）")
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}
        model = str(stream_payload["model"])
        delay = 1.0
        for attempt in range(1, self.config.max_retries + 1):
            parts: list[str] = []
            usage_raw: dict[str, Any] = {}
            finish_reason = ""
            emitter.llm_start(purpose=purpose, model=model)
            try:
                async with self.client.stream("POST", url, json=stream_payload) as resp:
                    if resp.status_code == 200:
                        async for line in resp.aiter_lines():
                            chunk = self._parse_sse(line)
                            if chunk is None:
                                continue
                            choices = chunk.get("choices") or []
                            if not choices:
                                # include_usage 的最后一帧（只有 usage、没有 choices）
                                usage_raw = chunk.get("usage") or usage_raw
                                continue
                            delta = choices[0].get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                parts.append(str(piece))
                                emitter.llm_delta(str(piece))
                            fr = choices[0].get("finish_reason")
                            if fr:
                                finish_reason = str(fr)
                    else:
                        body = (await resp.aread()).decode("utf-8", "replace")[:500]
                        emitter.llm_end(
                            model=model, from_fallback=from_fallback, usage=usage_raw or None
                        )
                        # JSON mode 不支持 → 交给上层降级（与 _post_with_retry 同口径）
                        if use_json and resp.status_code in (400, 422) and "response_format" in body.lower():
                            log.info("该端点不支持 response_format=json_object，降级为文本解析")
                            return None
                        if resp.status_code in (429, 500, 502, 503, 504):
                            if attempt >= self.config.max_retries:
                                raise LLMError(f"LLM 服务错误 {resp.status_code}：{body}")
                            retry_after = _retry_after(resp)
                            log.warning("LLM 限流/服务错误 %s，%.1fs 后重试", resp.status_code, retry_after)
                            await asyncio.sleep(retry_after or delay)
                            delay = min(delay * 2, 30.0)
                            continue
                        raise LLMError(f"LLM 请求失败 {resp.status_code}：{body}")
            except httpx.HTTPError as exc:
                emitter.llm_end(model=model, from_fallback=from_fallback, usage=usage_raw or None)
                # TLS 问题不重试：立即给出可操作提示，避免白等几轮退避
                if is_ssl_error(exc):
                    raise LLMError(f"连接失败（{url}）：{exc}\n{ssl_help_text()}") from exc
                if attempt >= self.config.max_retries:
                    raise LLMError(f"网络错误：{exc}") from exc
                log.warning("LLM 网络异常（第 %d 次）：%s", attempt, exc)
                await asyncio.sleep(delay + random.uniform(0, 0.5))
                delay = min(delay * 2, 30.0)
                continue
            emitter.llm_end(model=model, from_fallback=from_fallback, usage=usage_raw or None)
            return {
                "choices": [
                    {
                        "message": {"content": "".join(parts), "tool_calls": []},
                        "finish_reason": finish_reason or "stop",
                    }
                ],
                "usage": usage_raw,
            }
        raise LLMError("LLM 请求重试耗尽（流式）")  # pragma: no cover

    async def _post_with_retry(self, payload: dict[str, Any], *, use_json: bool) -> dict[str, Any] | None:
        """POST 并在可重试错误上退避重试。

        返回 ``None`` 表示「JSON mode 不被支持」，由调用方降级重试。
        """
        url = self.config.chat_completions_url
        # `max_retries` 是**总尝试次数**（下面按 `attempt >= max_retries` 判最后一次），
        # 所以 0 会让 `range(1, 1)` 全空：一次请求都不发出，然后直接掉到函数末尾那句
        # 「LLM 请求重试耗尽」——报错里完全看不出「配置成了 0」，现象却是「所有调用都失败」。
        # 实测踩过一次：探活为了快速失败设了 0，结果它永远失败，连 401 都问不出来。
        if self.config.max_retries < 1:
            raise LLMError(
                f"llm.max_retries={self.config.max_retries} 不会发出任何请求（它表示总尝试次数，至少为 1）；"
                "想「不重试」请填 1。"
            )
        delay = 1.0
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = await self.client.post(url, json=payload)
            except httpx.HTTPError as exc:
                # TLS 问题不重试：立即给出可操作提示，避免白等几轮退避
                if is_ssl_error(exc):
                    raise LLMError(f"连接失败（{url}）：{exc}\n{ssl_help_text()}") from exc
                if attempt >= self.config.max_retries:
                    raise LLMError(f"网络错误：{exc}") from exc
                log.warning("LLM 网络异常（第 %d 次）：%s", attempt, exc)
                await asyncio.sleep(delay + random.uniform(0, 0.5))
                delay = min(delay * 2, 30.0)
                continue

            if resp.status_code == 200:
                return resp.json()

            body = resp.text[:500]
            # JSON mode 不支持 → 交给上层降级
            if use_json and resp.status_code in (400, 422) and "response_format" in body.lower():
                log.info("该端点不支持 response_format=json_object，降级为文本解析")
                return None

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt >= self.config.max_retries:
                    raise LLMError(f"LLM 服务错误 {resp.status_code}：{body}")
                retry_after = _retry_after(resp)
                log.warning("LLM 限流/服务错误 %s，%.1fs 后重试", resp.status_code, retry_after)
                await asyncio.sleep(retry_after or delay)
                delay = min(delay * 2, 30.0)
                continue

            raise LLMError(f"LLM 请求失败 {resp.status_code}：{body}")
        raise LLMError("LLM 请求重试耗尽")  # pragma: no cover

    async def chat_json(
        self,
        messages: Sequence[Message],
        *,
        purpose: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        default: dict[str, Any] | None = None,
        max_parse_attempts: int = 2,
    ) -> tuple[dict[str, Any], Usage]:
        """要求模型返回 JSON 对象并解析。

        解析失败时**带原始输出重试一次**，再失败才返回 ``default``（默认空 dict）
        且不抛异常——AI 输出不可靠时上层应能降级继续，而不是整批任务崩掉。

        为什么要重试：推理模型会先用掉大量 token 写 ``reasoning_content``，
        一旦 ``max_tokens`` 被推理吃光，``content`` 直接为空
        （实测 ``finish_reason=length``、``content`` 长度 0）。这类「截断导致的
        空/半 JSON」是**偶发**的，原样重问一次通常就能拿到完整对象；
        而此前直接返回空 dict，等于让上层白跑一轮（多余的重生成/重试）。

        被长度截断时还会**自动加大 max_tokens** 重试（见 ``_escalate_tokens``）：
        推理长度会剧烈波动（实测同一提示 14207~17264 字符），固定预算迟早撞顶。
        """
        attempts = max(1, max_parse_attempts)
        usage = Usage(purpose=purpose)
        last_content = ""
        last_finish = ""
        cur_max_tokens = max_tokens
        tool_call_seen = False                     # 上一轮是不是把回复放进了工具调用

        for attempt in range(1, attempts + 1):
            msgs = list(messages)
            if attempt > 1:
                # 把上一轮的原始输出喂回去，并明确要求「只给 JSON、别写解释」
                msgs = list(messages) + [
                    {"role": "assistant", "content": last_content or "(上一轮返回为空)"},
                    {
                        "role": "user",
                        "content": (
                            f"上面这轮没有返回可解析的 JSON"
                            f"{f'（finish_reason={last_finish}）' if last_finish else ''}。"
                            + (
                                "**你返回的是工具调用，而这里不需要任何工具**——"
                                "不要调用工具或函数，直接把结果写成 JSON。"
                                if tool_call_seen
                                else ""
                            )
                            + "请**只输出一个 JSON 对象**，不要任何解释、前言或 Markdown 代码块；"
                            "务必输出完整闭合的 JSON（不要被长度截断）。"
                        ),
                    },
                ]

            resp = await self.chat(
                msgs,
                purpose=purpose,
                json_mode=True,
                temperature=temperature,
                max_tokens=cur_max_tokens,
            )
            usage = _merge_usage(usage, resp.usage)
            last_content = resp.content or ""
            last_finish = str((resp.raw.get("choices") or [{}])[0].get("finish_reason") or "")
            message = ((resp.raw.get("choices") or [{}])[0].get("message") or {})
            tool_calls = message.get("tool_calls") or []

            parsed = extract_json(resp.content)
            if parsed is not None:
                return parsed, usage

            if tool_calls:
                # 模型把回复放进了**工具调用**里（实测：finish_reason=tool_calls、content 长度 0）。
                # Fissue 从不需要工具，这个协议只会让每一轮都拿到空内容——报出来，别让运维
                # 对着一句「无法解析为 JSON」猜。下一轮的重试提示会明确要求纯 JSON。
                tool_call_seen = True
                names = "、".join(
                    str((c.get("function") or {}).get("name") or "?") for c in tool_calls
                )[:200]
                preview = str(((tool_calls[0].get("function") or {}).get("arguments")) or "")[:200]
                log.warning(
                    "LLM[%s] 这一轮回的是**工具调用**而不是 JSON（%s；参数前 200 字：%s）——"
                    "Fissue 不需要任何工具，若持续如此请在端点/模型侧关掉工具调用",
                    purpose, names, preview,
                )

            log.warning(
                "LLM[%s] 返回内容无法解析为 JSON，长度=%d finish_reason=%s（第 %d/%d 次）",
                purpose, len(last_content), last_finish or "-", attempt, attempts,
            )

            # 被长度截断 → 下一轮加大预算，给推理+正文留出余量
            if last_finish == "length":
                cur_max_tokens = _escalate_tokens(cur_max_tokens or self.config.max_tokens,
                                                  self.config.max_tokens_ceiling)
                log.warning("上一轮因长度截断，下一轮 max_tokens 提到 %d", cur_max_tokens)

        return (default if default is not None else {}), usage

    async def _record(self, usage: Usage) -> None:
        if self.usage_sink is None:
            return
        try:
            result = self.usage_sink(usage)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # 记账失败不影响主流程
            log.warning("记录 LLM 用量失败：%s", exc)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中稳健地提取 JSON 对象。

    依次尝试：直接解析 → 去围栏 → 截取首个平衡花括号块。
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]

    for m in _JSON_FENCE.finditer(text):
        candidates.append(m.group(1).strip())

    block = _balanced_object(text)
    if block:
        candidates.append(block)

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
    return None


def _balanced_object(text: str) -> str | None:
    """找到第一个括号平衡的 ``{...}`` 片段（跳过字符串内的花括号）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _estimate_tokens(messages: Sequence[Message]) -> int:
    """粗略估算提示 token（4 字符 ≈ 1 token，中文按 1.5 字符算）。"""
    chars = sum(len(m.get("content") or "") for m in messages)
    return max(1, int(chars / 3))


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return max(1.0, min(float(raw), 60.0))
        except ValueError:
            pass
    return 0.0


def clamp01(value: Any, default: float = 0.5) -> float:
    """把任意值收敛到 [0,1]。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


def clamp_score(value: Any, default: int = 50, lo: int = 0, hi: int = 100) -> int:
    """把任意值收敛到评分区间（兼容 0-1 的小数评分）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if 0 < num <= 1 and isinstance(value, float):
        num *= 100  # 模型偶尔返回 0.85 这种比例
    num = int(round(num))
    return max(lo, min(hi, num))
