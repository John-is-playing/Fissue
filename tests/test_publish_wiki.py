"""``scripts/publish_wiki.py`` 的离线测试。

为什么这么加载它
----------------
那个脚本是**仓库维护工具**，不是要发布的包内容，所以它待在 ``scripts/`` 而不是
``src/fissue/`` 里。代价是它不在一个可 import 的包内，这里按**文件路径**把它加载进来
（比给 ``scripts/`` 加 ``__init__.py`` 再依赖命名空间包更直白，也让「这是个脚本」
这件事在测试里看得见）。

为什么这些测试重要
------------------
平台 wiki 与仓库里的 ``docs/wiki/`` 是同一份内容的两个拷贝，而**链接规则不同**：
wiki 里页间链接不能带 ``.md``、指向仓库文档的链接必须换成完整 URL。转换写错的话，
平台上会出现一批点不动的链接——而这类错误在仓库里预览时**完全看不出来**。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "publish_wiki.py"
WIKI = REPO_ROOT / "docs" / "wiki"

BASE = "https://github.com/John-is-acting/Fissue"      # 用一个假地址，免得断言依赖真账号名
SRC = "docs/wiki"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_wiki", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # **必须先登记进 sys.modules**：`@dataclass` 在处理字段类型时要用
    # `sys.modules[cls.__module__]` 去找当前模块的命名空间；按文件路径加载的模块
    # 默认不在 sys.modules 里，于是 `sys.modules.get(...)` 返回 None、装饰器当场
    # `AttributeError: 'NoneType' object has no attribute '__dict__'`。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load()


def _rewrite(text: str, *, src: str = SRC, repo_root: Path | None = None) -> tuple[str, list, list]:
    return publish.rewrite_links(text, base_url=BASE, branch="main", src=src, repo_root=repo_root)


# ---------------------------------------------------------------------------
# 改写规则
# ---------------------------------------------------------------------------


def test_page_links_lose_the_md_suffix() -> None:
    """页间链接必须去掉 `.md`——wiki 渲染器按页面名互相引用，带上后缀就是 404。"""
    out, changes, warnings = _rewrite("[概览](Overview.md) 与 [架构](Architecture.md#状态机)")
    assert out == "[概览](Overview) 与 [架构](Architecture#状态机)"
    assert [c.kind for c in changes] == ["page", "page"]
    assert not warnings


def test_repo_doc_links_become_full_urls() -> None:
    """指向仓库文档的链接要换成完整 URL——wiki 仓库里没有 `../AUTH.md` 这个文件。"""
    out, changes, _ = _rewrite("[鉴权](../AUTH.md#1-背景) 与 [许可](../../SECURITY.md)")
    assert out == (
        f"[鉴权]({BASE}/blob/main/docs/AUTH.md#1-背景)"
        f" 与 [许可]({BASE}/blob/main/SECURITY.md)"
    )
    assert [c.kind for c in changes] == ["doc", "doc"]


def test_repo_links_are_not_limited_to_markdown() -> None:
    """**回归**：`../../LICENSE` 这类非 `.md` 的仓库链接也要换。

    原先只认 `.md`、其余「保守跳过」，实测 `Contributing.md` 与 `_Footer.md` 各有一条
    `../../LICENSE`，在平台上直接 404。
    """
    out, changes, warnings = _rewrite("[MIT 许可](../../LICENSE)")
    assert out == f"[MIT 许可]({BASE}/blob/main/LICENSE)"
    assert [c.kind for c in changes] == ["doc"]
    assert not warnings


def test_directories_link_to_tree_not_blob() -> None:
    """目录要用 `/tree/`：`/blob/` 指目录在平台上打不开。"""
    text = "[文档目录](../../docs/) 与 [设计](../../docs/DESIGN.md)"
    out, changes, _ = _rewrite(text, repo_root=REPO_ROOT)
    assert out == f"[文档目录]({BASE}/tree/main/docs) 与 [设计]({BASE}/blob/main/docs/DESIGN.md)"
    assert [c.kind for c in changes] == ["doc", "doc"]


def test_external_and_anchor_links_are_untouched() -> None:
    text = "[外链](https://example.com/a.md) · [同页](#3-鉴权) · [绝对](/whatever.md)"
    out, changes, warnings = _rewrite(text)
    assert out == text
    assert not changes and not warnings


def test_links_outside_the_repo_warn_and_stay() -> None:
    """指向仓库之外的链接**原样保留并告警**——绝不猜着改。"""
    out, changes, warnings = _rewrite("[外面](../../../outside.md)")
    assert out == "[外面](../../../outside.md)"
    assert not changes
    assert warnings and "仓库之外" in warnings[0]


# ---------------------------------------------------------------------------
# 代码块与行内代码：一个字都不能动
# ---------------------------------------------------------------------------


def test_fenced_code_block_is_left_alone() -> None:
    text = "说明：\n\n```markdown\n[概览](Overview.md) 与 [设计](../DESIGN.md)\n```\n\n之后 [真链接](Overview.md)。\n"
    out, changes, _ = _rewrite(text)
    assert "```markdown\n[概览](Overview.md) 与 [设计](../DESIGN.md)\n```" in out, "代码块里的示例被改了"
    assert out.endswith("之后 [真链接](Overview)。\n")
    assert len(changes) == 1, "只有代码块外那一条该被改"


def test_inline_code_is_left_alone() -> None:
    """行内代码里的链接是**说明文字**，改了等于把文档写坏。"""
    text = "发布时要按 `[概览](Overview.md)` 与 `../xxx.md` 的形式改写。\n\n真实链接：[概览](Overview.md)\n"
    out, changes, _ = _rewrite(text)
    assert "`[概览](Overview.md)`" in out and "`../xxx.md`" in out
    assert out.endswith("[概览](Overview)\n")
    assert len(changes) == 1


def test_an_inline_triple_backtick_does_not_shift_the_split() -> None:
    """**回归**：正文里**行内**出现的三反引号不能把后面的判定带偏。

    这是实测踩到的坑：一条吃全文的正则把它当成围栏开启，于是之后代码/非代码的分类整体
    错位一格，正文里的链接被静默漏改（干跑表现为「有的链接改了、有的没改」）。
    """
    text = "行内提一句 ``` 这种写法是合法的。\n\n这段里的链接 [概览](Overview.md) 必须被改。\n"
    out, changes, warnings = _rewrite(text)
    assert "[概览](Overview)" in out, "行内三反引号把后面的正文误判成代码了"
    assert len(changes) == 1
    assert not warnings


def test_double_backtick_inline_code_is_recognised() -> None:
    """`` ``x`` `` 也是行内代码（CommonMark：等长反引号段配对），里面的链接同样不能动。"""
    text = "写法：``[概览](Overview.md)``\n\n真实：[概览](Overview.md)\n"
    out, changes, _ = _rewrite(text)
    assert "``[概览](Overview.md)``" in out
    assert len(changes) == 1


# ---------------------------------------------------------------------------
# 幂等与全量自检
# ---------------------------------------------------------------------------


def test_rewriting_is_idempotent() -> None:
    """重复运行必须稳定：文本不变、**也不再告警**。

    这条不只为「跑两次没事」——发布流程本来就是反复跑，第二次冒一堆没意义的告警，
    只会让人以为哪里坏了。
    """
    text = "[概览](Overview.md) · [鉴权](../AUTH.md) · [许可](../../LICENSE)\n"
    once, _, _ = _rewrite(text)
    twice, changes, warnings = _rewrite(once)
    assert twice == once
    assert not changes, "第二次不该再有可改的"
    assert not warnings, "第二次不该再告警"


def _link_targets(text: str) -> list[str]:
    """取出**非代码**位置的链接目标。"""
    targets: list[str] = []
    for is_code, chunk in publish.split_code(text):
        if not is_code:
            targets.extend(publish._LINK.findall(chunk))
    return targets


#: 发布后允许剩下的链接形态：完整 URL，或**裸页名**（可带锚点）。
_ALLOWED = re.compile(r"^(?:https?://\S+|[A-Za-z0-9._-]+(?:#[^#]*)?)$")


def test_the_real_wiki_set_rewrites_cleanly() -> None:
    """把真实的 `docs/wiki/` 整份转一遍：不留相对链接、不留 `.md` 页链、零告警。

    这是最强的一条——它跑在**真实语料**上，而不是我构造的样例。
    """
    files = publish.transform_all(
        WIKI, base_url=BASE, branch="main", src_rel=SRC, repo_root=REPO_ROOT
    )

    assert len(files) == 21, "docs/wiki 的页面数变了，确认是不是新增/删除了页面"
    assert {"Home.md", "_Sidebar.md", "_Footer.md"} <= set(files)

    violations: list[str] = []
    warnings: list[str] = []
    for name, (text, _changes, warns) in files.items():
        warnings += [f"{name}: {w}" for w in warns]
        for target in _link_targets(text):
            if not _ALLOWED.match(target):
                violations.append(f"{name}: {target}")

    assert not violations, "转换后仍有不合规的链接目标（发布到平台会点不动）：\n  " + "\n  ".join(
        violations[:10]
    )
    assert not warnings, "转换过程有告警：\n  " + "\n  ".join(warnings[:10])


def test_real_pages_keep_their_code_examples_and_fix_their_links() -> None:
    """两个真实页面的具体回归：说明文字没被改坏、该改的链接真的改了。"""
    files = publish.transform_all(
        WIKI, base_url=BASE, branch="main", src_rel=SRC, repo_root=REPO_ROOT
    )

    home = files["Home.md"][0]
    assert "`页面名.md`" in home, "Home.md 里反引号包着的示例被改坏了"
    assert "`../xxx.md`" in home, "Home.md 里反引号包着的示例被改坏了"

    ai = files["AI-Layer.md"][0]
    assert "](Benchmark#提示词重写前后的对照同一批待修条目)" in ai, (
        "那条带锚点的页间链接没被改写——正文里行内出现的三反引号把判定带偏了"
    )
    assert "](Benchmark.md#" not in ai

    footer = files["_Footer.md"][0]
    assert f"]({BASE}/blob/main/LICENSE)" in footer


# ---------------------------------------------------------------------------
# 输出与入口
# ---------------------------------------------------------------------------


def test_write_out_materialises_every_page(tmp_path: Path) -> None:
    files = publish.transform_all(
        WIKI, base_url=BASE, branch="main", src_rel=SRC, repo_root=REPO_ROOT
    )
    publish.write_out(files, tmp_path)

    written = sorted(p.name for p in tmp_path.glob("*.md"))
    assert written == sorted(files), "写出的文件集合与原文件不一致"
    assert (tmp_path / "Home.md").read_text(encoding="utf-8") == files["Home.md"][0]


def test_cli_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """默认是**干跑**：报告改了什么，但一个文件都不写。"""
    before = {p.name: p.stat().st_mtime_ns for p in WIKI.glob("*.md")}

    code = publish.main(["--repo-root", str(REPO_ROOT), "--base-url", BASE])

    assert code == 0
    out = capsys.readouterr().out
    assert "处改写" in out and "干跑" in out
    assert {p.name: p.stat().st_mtime_ns for p in WIKI.glob("*.md")} == before, "干跑动了源文件"


def test_cli_refuses_to_push_without_a_remote(capsys: pytest.CaptureFixture[str]) -> None:
    """`--push` 少了 `--remote` 要当场拒绝，而不是推到一个猜出来的地址上。"""
    code = publish.main(["--repo-root", str(REPO_ROOT), "--push"])
    assert code == 2
    assert "--remote" in capsys.readouterr().err


def test_push_gives_an_actionable_error_when_the_wiki_is_missing() -> None:
    """wiki 仓库还不存在时，报错必须**说清下一步**（去网页建第一页），而不是丢 git 原文。

    这里故意用一个不存在的远端：`git clone` 会失败，正是「还没建第一页」的现实情形。
    """
    files = publish.transform_all(
        WIKI, base_url=BASE, branch="main", src_rel=SRC, repo_root=REPO_ROOT
    )
    with pytest.raises(SystemExit) as exc:
        publish.push_to_wiki(
            files,
            remote=str(REPO_ROOT / "does-not-exist.wiki.git"),
            web_url=BASE,
            prune=False,
            message="test",
        )
    message = str(exc.value)
    assert "第一页" in message and "不是权限问题" in message
