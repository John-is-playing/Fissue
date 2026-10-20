"""任务详情页的服务端 Markdown 渲染（web/markdown.py）与事件 HTML 渲染。

渲染器只有一种输入：LLM 输出（不可信）。核心保证：**原始 HTML 一律先转义**，
渲染结果里不得出现任何可执行的输入标记。
"""

from __future__ import annotations

from fissue.web.markdown import render_job_event, render_markdown

# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def test_raw_html_is_never_executed() -> None:
    out = render_markdown("<script>alert(1)</script>hello")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "hello" in out


def test_fenced_code_block_kept_verbatim() -> None:
    out = render_markdown("intro\n```python\nx = 1 ** 2  # not bold\n```\nafter")
    assert '<pre class="md-pre"><code class="lang-python">' in out
    assert "x = 1 ** 2  # not bold" in out
    assert "<b>" not in out
    assert out.endswith("<p>after</p>")


def test_inline_markup() -> None:
    out = render_markdown(
        "a **bold** and *italic* and `code(x < y)` and [link](https://example.com/a?b=1&c=2)"
    )
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "<code>code(x &lt; y)</code>" in out
    assert (
        '<a href="https://example.com/a?b=1&amp;c=2" rel="noopener" target="_blank">link</a>'
        in out
    )


def test_headings_lists_quote_and_hr() -> None:
    out = render_markdown("# T1\n## T2\n- one\n- two\n1. first\n> quoted\n---\npara")
    assert '<h1 class="md-h">T1</h1>' in out
    assert '<h2 class="md-h">T2</h2>' in out
    assert "<ul>\n<li>one</li>\n<li>two</li>\n</ul>" in out
    assert "<ol>\n<li>first</li>\n</ol>" in out
    assert '<blockquote class="md-quote">quoted</blockquote>' in out
    assert "<hr>" in out
    assert "<p>para</p>" in out


def test_unsafe_link_is_left_as_text() -> None:
    out = render_markdown("[click](javascript:alert(1))")
    assert "javascript:" in out
    assert "<a " not in out


# ---------------------------------------------------------------------------
# render_job_event
# ---------------------------------------------------------------------------


def _llm_ev(**over) -> dict:
    base = {
        "type": "llm",
        "ts": "2026-01-01T00:00:00",
        "purpose": "eval",
        "model": "m",
        "text": "",
        "done": True,
        "usage": None,
    }
    base.update(over)
    return base


def test_llm_event_json_body_becomes_code_block() -> None:
    ev = render_job_event(
        _llm_ev(text='{"ok": true}', usage={"total_tokens": 9})
    )
    assert '<pre class="md-pre"><code>' in ev["html"]
    # html.escape 把引号转成 &quot;（浏览器显示回来仍是 "）
    assert "&quot;ok&quot;: true" in ev["html"]
    assert "9 tokens" in ev["html"]


def test_llm_event_markdown_body() -> None:
    ev = render_job_event(_llm_ev(text="# 结论\n\n是**真实**缺陷"))
    assert '<h1 class="md-h">结论</h1>' in ev["html"]
    assert "<b>真实</b>" in ev["html"]


def test_llm_event_streaming_marker() -> None:
    ev = render_job_event(_llm_ev(text="", done=False))
    assert "流式输出中…" in ev["html"]
    assert 'class="spinner small"' in ev["html"]


def test_llm_event_partial_json_falls_back_to_markdown() -> None:
    # 流式中途 JSON 不完整：不能报错，降级按 Markdown（此处即纯文本）渲染
    ev = render_job_event(_llm_ev(text='{"a":', done=False))
    assert '<p>{"a":</p>' in ev["html"]


def test_tool_event_escapes_detail_and_marks_ok() -> None:
    ok_ev = render_job_event(
        {"type": "tool", "ts": "2026-01-01T00:00:00", "name": "平台 PR", "detail": "已建 <x>", "ok": True}
    )
    assert ok_ev["html"].startswith('<div class="ev ev-tool ok"')
    assert "已建 &lt;x&gt;" in ok_ev["html"]

    bad_ev = render_job_event(
        {"type": "tool", "ts": "2026-01-01T00:00:00", "name": "验证器", "detail": "失败", "ok": False}
    )
    assert bad_ev["html"].startswith('<div class="ev ev-tool bad"')

    plain = render_job_event(
        {"type": "tool", "ts": "2026-01-01T00:00:00", "name": "修复 Agent", "detail": "读了 a.py"}
    )
    assert plain["html"].startswith('<div class="ev ev-tool"')


def test_log_event_keeps_time_and_text() -> None:
    ev = render_job_event({"type": "log", "ts": "2026-01-01T12:34:56", "text": "step <1>"})
    assert "12:34:56" in ev["html"]
    assert "step &lt;1&gt;" in ev["html"]
