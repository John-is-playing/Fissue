"""任务详情页的小 Markdown 渲染器（服务端）。

为什么不引 ``markdown`` 包
---------------------------
输入只有「LLM 输出」这一种，而且渲染结果会进管理界面的 DOM；依赖清单刻意保持精简
（见 pyproject 的「这里没有、以及为什么没有」）。渲染器必须保证一件事：**输入一律先
HTML 转义**——LLM 输出不可信（可以含 ``<script>``），渲染结果里不得出现可执行标记。

覆盖：围栏代码块 / 行内代码 / 标题 / 粗斜体 / 链接 / 列表 / 引用 / 分隔线 / 段落。
表格、图片、嵌套结构不支持（LLM 输出里很少出现；遇到时按纯文本降级，是安全的）。
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

#: 链接只允许 http(s) / mailto——``javascript:`` 之类不渲染成链接，留作纯文本。
_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:https?|mailto):[^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_FENCE_RE = re.compile(r"^```(\w*)[ \t]*\n(.*?)^\s*```[ \t]*$", re.M | re.S)


def render_markdown(text: str) -> str:
    """把 Markdown 文本变成**安全**的 HTML 片段。

    输入先整体 ``html.escape``，因此输出里不含任何原始输入标记，只有本模块产出的标签。
    """
    text = html.escape(text, quote=False)

    # 围栏代码块先抽成占位符：行内解析不再碰代码内部（代码里没有粗体/链接，
    # 也不该再处理——整体转义时已处理过）。
    blocks: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        blocks.append(_render_fence(m.group(1), m.group(2)))
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    text = _FENCE_RE.sub(_stash, text)

    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag is not None:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in text.splitlines():
        stripped = raw.strip()

        m = re.match(r"^\x00BLOCK(\d+)\x00$", stripped)
        if m:
            flush_para()
            close_list()
            out.append(blocks[int(m.group(1))])
            continue

        if not stripped:
            flush_para()
            close_list()
            continue

        hm = _HEADING_RE.match(stripped)
        if hm:
            flush_para()
            close_list()
            level = len(hm.group(1))
            out.append(f'<h{level} class="md-h">{_inline(hm.group(2))}</h{level}>')
            continue

        if _HR_RE.match(stripped):
            flush_para()
            close_list()
            out.append("<hr>")
            continue

        # ``>`` 已被整体转义成 ``&gt;``
        if stripped.startswith("&gt;"):
            flush_para()
            close_list()
            quote = re.sub(r"^&gt;[ \t]?", "", stripped)
            out.append(f'<blockquote class="md-quote">{_inline(quote)}</blockquote>')
            continue

        ul = _UL_RE.match(stripped)
        if ul:
            flush_para()
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{_inline(ul.group(1))}</li>")
            continue

        ol = _OL_RE.match(stripped)
        if ol:
            flush_para()
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{_inline(ol.group(1))}</li>")
            continue

        para.append(stripped)

    flush_para()
    close_list()
    return "\n".join(out)


def _render_fence(lang: str, code: str) -> str:
    cls = f' class="lang-{lang}"' if lang else ""
    return f'<pre class="md-pre"><code{cls}>{code}</code></pre>'


def _inline(s: str) -> str:
    """行内格式：代码（最高优先）→ 链接 → 粗体 → 斜体。"""
    codes: list[str] = []

    def _code(m: re.Match[str]) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x01CODE{len(codes) - 1}\x01"

    s = _INLINE_CODE_RE.sub(_code, s)
    s = _LINK_RE.sub(r'<a href="\2" rel="noopener" target="_blank">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<i>\1</i>", s)
    for i, c in enumerate(codes):
        s = s.replace(f"\x01CODE{i}\x01", c)
    return s


# ---------------------------------------------------------------------------
# 任务事件 → 安全 HTML（详情页时间线用）
# ---------------------------------------------------------------------------


def render_job_event(ev: dict[str, Any]) -> dict[str, Any]:
    """把一条任务事件渲染成带 ``html`` 字段的形式（**安全**，可直接拼进 DOM）。

    html 一律在**服务端**生成：内容先转义再转换（见 :func:`render_markdown`），
    前端只负责拼接，不接触原始内容。
    """
    ts = str(ev.get("ts") or "")[11:19]
    etype = ev.get("type")
    if etype == "llm":
        text = str(ev.get("text") or "")
        if text:
            body = _render_llm_body(text)
        elif ev.get("done"):
            body = '<span class="muted">（空响应）</span>'
        else:
            body = '<span class="muted">流式输出中…</span>'
        header = (
            f"<b>LLM · {html.escape(str(ev.get('purpose') or '-'))}</b>"
            f'<span class="muted small">{html.escape(str(ev.get("model") or "-"))}'
            + (" · fallback" if ev.get("from_fallback") else "")
            + "</span>"
        )
        usage = ev.get("usage") or {}
        tokens = usage.get("total_tokens") or 0
        tail = f" · {tokens} tokens" if tokens else ""
        state = "" if ev.get("done") else '<span class="spinner small"></span>'
        html_out = (
            f'<div class="ev ev-llm"><div class="ev-head">{state}{header}'
            f'<span class="muted small">{tail} · {ts}</span></div>'
            f'<div class="ev-body md">{body}</div></div>'
        )
    elif etype == "tool":
        ok = ev.get("ok")
        mark = "✓" if ok is True else ("✗" if ok is False else "·")
        cls = "ok" if ok is True else ("bad" if ok is False else "")
        detail = str(ev.get("detail") or "")
        body = f'<pre class="ev-pre">{html.escape(detail)}</pre>' if detail else ""
        html_out = (
            f'<div class="ev ev-tool{(" " + cls) if cls else ""}">'
            f'<div class="ev-head"><span class="tool-mark {cls}">{mark}</span>'
            f"<b>{html.escape(str(ev.get('name') or '工具'))}</b>"
            f'<span class="muted small">{ts}</span></div>{body}</div>'
        )
    else:
        html_out = (
            f'<div class="ev ev-log"><span class="muted small">{ts}</span> '
            f"<span>{html.escape(str(ev.get('text') or ''))}</span></div>"
        )
    return {**ev, "html": html_out}


def _render_llm_body(text: str) -> str:
    """LLM 事件正文：整体可解析为 JSON 时按代码块格式化（评测 / 裁决 / agent
    动作的输出正是 JSON），否则走 Markdown 渲染。流式中途（JSON 不完整）会
    自然落到 Markdown 分支，结束后的下一帧补上代码块形态。"""
    stripped = text.strip()
    if stripped[:1] in {"{", "["}:
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            obj = None
        if obj is not None:
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
            return f'<pre class="md-pre"><code>{html.escape(pretty)}</code></pre>'
    return render_markdown(text)
