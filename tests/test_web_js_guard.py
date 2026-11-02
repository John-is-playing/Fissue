"""前端轮询的静态守卫：只读文件、正则扫描，不联网、不 import 前端代码。

为什么需要它：项目历史上出过一次前端事故——「点一次『探测沙盒』，永久多出一条并行轮询链」，
以及「切到后台标签页仍在每 2 秒打一次 /status」。这两类错误在浏览器里不报错、不崩，
只会让服务端白白被刷、让「最近更新」在没人看的时候乱变，代码评审也极容易漏掉。
所以用三条静态规则把它们钉死，让后面的改动只要重犯就会在 CI 里红。

规则与取舍：

1. 全仓库前端禁用 ``setInterval(``：它没有「当前这一张票」的概念，想停只能靠
   ``clearInterval``，而续期路径上根本清不掉已经在排的那一份，历史事故就是它造成的重复链。
2. 会自己续期的脚本必须出现 ``clearTimeout(``：没有票的续期 = 一条永久不可清除的链。
   这里只扫「带可见性判断」的脚本（含 ``document.hidden`` 或 ``visibilitychange``），
   也就是真正的轮询调度器；一次性的 ``setTimeout(reload, 1200)`` 这类跳转延时
   天然没有续期路径，硬要求它配 ``clearTimeout`` 只会逼人写无用代码。
3. ``visibilitychange`` 监听器的函数体里必须判 ``document.hidden``：
   否则切到后台照样每几秒打一次服务端（浏览器只节流定时器，不取消请求）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB_ROOT = Path(__file__).resolve().parents[1] / "src" / "fissue" / "web"

# 内联脚本：只要 <script>…</script> 的正文，带 src 的标签没有正文，直接跳过。
_INLINE_SCRIPT = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

Script = tuple[str, str]  # (便于定位的名字, 脚本文本)


def _label(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_scripts(web_root: Path) -> list[Script]:
    """收集要扫的「脚本单元」：static/*.js 整份，加上模板里的每段内联 script。

    按「单元」而不是按整份文件扫，是因为规则 2/3 说的是「这一个脚本里」的配套关系：
    同一个页面里另一段只做跳转的脚本不该替违规的那段顶罪。
    """
    scripts: list[Script] = []
    for path in sorted((web_root / "static").glob("*.js")):
        scripts.append((_label(path, web_root), path.read_text(encoding="utf-8")))
    for path in sorted((web_root / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for index, match in enumerate(_INLINE_SCRIPT.finditer(text), 1):
            if "src=" in match.group("attrs").lower():
                continue
            scripts.append((f"{_label(path, web_root)}#{index}", match.group("body")))
    return scripts


def _is_recurring(body: str) -> bool:
    """判断这段脚本是不是「会自己排下一轮」的轮询调度器。

    用可见性判断作为标志：只有需要考虑前后台切换的脚本才是在反复发请求。
    """
    return "document.hidden" in body or "visibilitychange" in body


def _listener_bodies(source: str) -> list[str]:
    """取出 ``visibilitychange`` 监听器的函数体（花括号配对）。

    监听器写成具名函数引用（没有函数体可看）时跳过——那种情况下由被引用的函数自己负责，
    静态扫描到此为止，不猜。
    """
    bodies: list[str] = []
    for match in re.finditer("visibilitychange", source):
        brace = source.find("{", match.end())
        if brace < 0 or ";" in source[match.end() : brace]:
            continue
        depth = 0
        for index in range(brace, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(source[brace : index + 1])
                    break
    return bodies


def _setinterval_violations(scripts: list[Script]) -> list[str]:
    return [name for name, body in scripts if "setInterval(" in body]


def _missing_clear_violations(scripts: list[Script]) -> list[str]:
    bad: list[str] = []
    for name, body in scripts:
        if "setTimeout(" not in body or not _is_recurring(body):
            continue
        if "clearTimeout(" not in body:
            bad.append(name)
    return bad


def _hidden_check_violations(scripts: list[Script]) -> list[str]:
    bad: list[str] = []
    for name, body in scripts:
        for listener in _listener_bodies(body):
            if "document.hidden" not in listener:
                bad.append(name)
                break
    return bad


SCRIPTS = _iter_scripts(WEB_ROOT)


@pytest.mark.parametrize(("name", "body"), SCRIPTS, ids=[name for name, _ in SCRIPTS])
def test_no_setinterval(name: str, body: str) -> None:
    """禁止 setInterval(。

    它无法像 setTimeout 那样把「当前那一张票」清掉：续期时旧的那一份还挂在浏览器里，
    于是每次续期都多一条链。历史事故——点一次「探测沙盒」永久多一条并行轮询链——就是它造成的。
    """
    assert "setInterval(" not in body, (
        f"{name} 里出现了 setInterval(。它会造出清不掉的并行轮询链（本仓库真出过这个事故）；"
        "请改成「一个定时器变量 + clearTimeout 后再 setTimeout」的单链调度器。"
    )


@pytest.mark.parametrize(("name", "body"), SCRIPTS, ids=[name for name, _ in SCRIPTS])
def test_rescheduling_script_can_be_cancelled(name: str, body: str) -> None:
    """自己排下一轮的脚本必须能清掉当前这张票（出现 clearTimeout(）。"""
    assert not (_is_recurring(body) and "setTimeout(" in body) or "clearTimeout(" in body, (
        f"{name} 会自己续期（反复发请求）却没有 clearTimeout(：这是条无票续期的链，"
        "谁也停不掉、也没法在切后台/补一次时避免重复。请先 clearTimeout 再 setTimeout。"
    )


@pytest.mark.parametrize(("name", "body"), SCRIPTS, ids=[name for name, _ in SCRIPTS])
def test_visibility_listener_checks_hidden(name: str, body: str) -> None:
    """visibilitychange 监听器里必须判 document.hidden。"""
    assert not _hidden_check_violations([(name, body)]), (
        f"{name} 的 visibilitychange 监听器没有判 document.hidden：这样切到后台标签页仍会"
        "每几秒打一次服务端（浏览器只节流定时器，不会取消请求）。请只在回到前台时补一次。"
    )


def test_guard_detects_deliberately_bad_scripts(tmp_path: Path) -> None:
    """反向自检：故意违规的片段必须被判为失败。

    没有这一步，三条规则很容易写成「永远为真」的假守卫（比如路径写错、正则没命中、
    正则失配直接放行）。这里在 tmp_path 造一份假前端，不碰仓库里的真文件。
    """
    static = tmp_path / "static"
    templates = tmp_path / "templates"
    static.mkdir()
    templates.mkdir()
    (static / "bad_interval.js").write_text(
        "setInterval(function () { poll(); }, 2000);\n",
        encoding="utf-8",
    )
    (static / "bad_reschedule.js").write_text(
        "var t = null;\n"
        "function loop() {\n"
        "  if (document.hidden) return;\n"
        "  setTimeout(loop, 2000);\n"
        "}\n"
        "loop();\n",
        encoding="utf-8",
    )
    (templates / "bad_inline.html").write_text(
        "<script>document.addEventListener('visibilitychange', function () { poll(); });</script>\n",
        encoding="utf-8",
    )
    # 正面对照：单链调度器必须被放行，否则守卫会逼着人改坏代码。
    (static / "good_scheduler.js").write_text(
        "var timer = null;\n"
        "function schedule(delay) {\n"
        "  if (timer !== null) clearTimeout(timer);\n"
        "  timer = setTimeout(tick, delay);\n"
        "}\n"
        "function tick() {\n"
        "  if (document.hidden) { schedule(5000); return; }\n"
        "  poll();\n"
        "  schedule(2000);\n"
        "}\n"
        "document.addEventListener('visibilitychange', function () {\n"
        "  if (!document.hidden) schedule(0);\n"
        "});\n"
        "schedule(0);\n",
        encoding="utf-8",
    )
    scripts = _iter_scripts(tmp_path)

    assert "static/bad_interval.js" in _setinterval_violations(scripts)
    assert "static/bad_reschedule.js" in _missing_clear_violations(scripts)
    assert "templates/bad_inline.html#1" in _hidden_check_violations(scripts)
    assert "static/good_scheduler.js" not in _setinterval_violations(scripts)
    assert "static/good_scheduler.js" not in _missing_clear_violations(scripts)
    assert "static/good_scheduler.js" not in _hidden_check_violations(scripts)