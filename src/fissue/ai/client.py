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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

import httpx

from ..config import BudgetConfig, LLMConfig, PricingConfig
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

    async def __aenter__(self) -> "LLMClient":
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
            data = await self._post_with_retry(payload, use_json=use_json)

        if data is None:
            # JSON mode 不被支持 → 去掉后重试一次
            payload.pop("response_format", None)
            self._json_mode_supported = False
            async with self._semaphore:
                data = await self._post_with_retry(payload, use_json=False)

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

    async def _post_with_retry(self, payload: dict[str, Any], *, use_json: bool) -> dict[str, Any] | None:
        """POST 并在可重试错误上退避重试。

        返回 ``None`` 表示「JSON mode 不被支持」，由调用方降级重试。
        """
        url = self.config.chat_completions_url
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
    ) -> tuple[dict[str, Any], Usage]:
        """要求模型返回 JSON 对象并解析。

        解析失败时返回 ``default``（默认空 dict），不抛异常——AI 输出不可靠时
        上层应能降级继续，而不是整批任务崩掉。
        """
        resp = await self.chat(
            messages,
            purpose=purpose,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        parsed = extract_json(resp.content)
        if parsed is None:
            log.warning("LLM[%s] 返回内容无法解析为 JSON，长度=%d", purpose, len(resp.content))
            return (default if default is not None else {}), resp.usage
        return parsed, resp.usage

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
