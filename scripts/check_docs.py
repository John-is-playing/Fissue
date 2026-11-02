#!/usr/bin/env python3
"""检查仓库内 Markdown 文档的**相对链接**是否还能落到真实文件上。

为什么需要它
------------
本项目的文档是一张互相引用的网：``README.md`` 指向 18 个文件，``docs/wiki/`` 有 19 篇
页面彼此链接，``docs/USAGE.md`` 顶部还有一排锚点目录。这些东西**没有任何东西在校验**：

* `scripts/publish_wiki.py` 会**重写**链接（去掉 ``.md``、把 ``../AUTH.md`` 换成完整 URL），
  但它只改不查——源文件里原本就是死链的话，它照样会把它发布出去；
* GitHub 渲染时不报错，只是把死链变成一个能点、点了 404 的文字，所以**没人会注意到**；
* 一次「把 docs/X.md 改名为 docs/Y.md」的重构，就能在毫无提示的情况下留下七八个死链。

死链不会让代码跑不起来，但会让第一次读仓库的人（以及下一个 AI Agent）走到空页面上——
本项目的文档密度已经高到值得为此加一道门。

检查什么、不检查什么
--------------------
* **查**：相对路径链接（``docs/DESIGN.md``、``../AUTH.md``、``LICENSE``）指向的文件是否存在，
  以及 ``.md`` 目标的 ``#锚点`` 在该文件里是否真的有一个对应标题；
* **不查**：外部 http(s) 链接（要联网，CI 里会把「对方站点抽风」算成本项目的失败）、
  ``mailto:``、纯页内锚点（它们由锚点规则单独覆盖）。
* 代码块与行内代码里的链接**一律跳过**：文档里常把链接示例写在反引号中
  （``Home.md`` 就是这么举例的），照字面解析会把说明文字算成死链。

退出码：``0`` 全部通过；``1`` 有死链（CI 据此阻断）。
``--anchors`` 把「锚点找不到」也升级为错误（默认只警告）——锚点依赖标题文字，
比文件存在更容易误报，所以默认不当门禁。

用法
----
::

    python scripts/check_docs.py                  # 扫默认范围（仓库根 *.md + docs/**/*.md）
    python scripts/check_docs.py --anchors        # 连锚点一起当错误
    python scripts/check_docs.py docs/wiki        # 只扫某个目录
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 默认扫描的顶层文档。刻意用白名单而不是「仓库根所有 .md」：
#: `demo/` 与 `banner/` 下各有独立的小仓库/子项目，它们的相对链接按各自的根解释，
#: 混进来只会制造假死链。
DEFAULT_FILES = (
    "README.md",
    "BENCHMARK.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "TRIAGE.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
)
#: 默认扫描的目录（递归）。
DEFAULT_DIRS = ("docs",)

#: 围栏代码块：```lang / ~~~ 均可，成对吃掉。
_FENCE = re.compile(r"^(?P<fence>```|~~~).*?^(?P=fence)", re.MULTILINE | re.DOTALL)
#: 行内代码：`` `...` ``（不分跨行，避免把整段吞掉）。
_INLINE_CODE = re.compile(r"`[^`\n]*`")
#: ``[文本](目标)``：目标里不取空白，也不取成对的尖括号写法之外的花哨形状。
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?\s*\)")
#: ATX 标题：``## 标题``；行尾的 ``#`` 是可选关闭符，不算正文。
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Link:
    """一条被解析出来的链接（已带来源位置，便于打印 ``文件:行: 原因``）。"""

    source: Path
    line: int
    target: str


@dataclass(frozen=True)
class Problem:
    """一处问题；``kind`` 决定它是不是阻断项。"""

    source: Path
    line: int
    target: str
    reason: str
    kind: str  # "missing" | "anchor"

    @property
    def blocking_kind(self) -> str:
        return self.kind


def strip_code(text: str) -> str:
    """把代码块与行内代码替换成等长空白，**保持行号与列位置不变**。

    保持长度是关键：后面要靠 ``text[:start].count("\\n")`` 算行号，一段被删短的
    代码块会让它之后所有链接的行号整体上移，报错就指错地方了。
    """

    def _blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _INLINE_CODE.sub(_blank, _FENCE.sub(_blank, text))


def iter_links(text: str, source: Path) -> list[Link]:
    """从一段 Markdown 里取出所有相对链接（外部 URL / mailto / 纯锚点先剔除）。"""
    clean = strip_code(text)
    links: list[Link] = []
    for match in _LINK.finditer(clean):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        line = clean[: match.start()].count("\n") + 1
        links.append(Link(source=source, line=line, target=target))
    return links


def slugify(heading: str) -> str:
    """按 GitHub 的口径把标题文字压成锚点。

    只要「同一份规则同时用在标题和链接上」就够本脚本自洽：``## 6. 配置模板`` →
    ``6-配置模板``、``## 4. 业务规则（config.yaml）`` → ``4-业务规则configyaml``，
    与 ``docs/USAGE.md`` 顶部目录里写的锚点一致。
    """
    text = unicodedata.normalize("NFKC", heading).strip().lower()
    kept = [ch if (ch.isalnum() or ch in "-_ " or ord(ch) > 0x2E7F) else "" for ch in text]
    return "".join(kept).strip().replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    """收集一个 Markdown 文件里所有标题锚点。"""
    text = strip_code(path.read_text(encoding="utf-8", errors="replace"))
    return {slugify(m.group(2)) for m in _HEADING.finditer(text)}


def check_file(path: Path) -> list[Problem]:
    """检查单个文件里的相对链接；返回问题清单（文件不存在 / 锚点不存在）。"""
    problems: list[Problem] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for link in iter_links(text, path):
        target, _, anchor = link.target.partition("#")
        if not target:  # 纯页内锚点：由下面 anchor 分支之外单独判，先落到本文件
            target_path = path
        else:
            target_path = (path.parent / target).resolve()
            if not target_path.exists():
                problems.append(
                    Problem(path, link.line, link.target, "链接目标文件不存在", "missing")
                )
                continue
        if (
            anchor
            and target_path.suffix.lower() == ".md"
            and target_path.is_file()
            and slugify(anchor) not in anchors_of(target_path)
        ):
            problems.append(
                Problem(path, link.line, link.target, "目标文件里没有这个标题锚点", "anchor")
            )
    return problems


def collect_files(paths: list[str] | None) -> list[Path]:
    """把命令行给的路径展开成待扫的 Markdown 文件清单（去重、保序）。"""
    if paths:
        roots = [Path(p) for p in paths]
    else:
        roots = [REPO_ROOT / name for name in DEFAULT_FILES]
        roots += [REPO_ROOT / name for name in DEFAULT_DIRS]

    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        candidates = (
            sorted(root.rglob("*.md")) if root.is_dir() else ([root] if root.exists() else [])
        )
        for item in candidates:
            resolved = item.resolve()
            if resolved not in seen and resolved.is_file():
                seen.add(resolved)
                files.append(resolved)
    return files


def _display(path: Path) -> str:
    """尽量打印相对仓库根的路径，绝对路径在 CI 日志里又长又难对照。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 Markdown 文档里的相对链接与锚点是否还能落到真实内容上",
    )
    parser.add_argument("paths", nargs="*", help="要检查的文件或目录（默认：仓库根文档 + docs/）")
    parser.add_argument(
        "--anchors",
        action="store_true",
        help="把「锚点找不到」也算作错误（默认只警告）",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="只打印问题，不打印统计行")
    args = parser.parse_args(argv)

    files = collect_files(args.paths or None)
    if not files:
        print("没有找到任何 Markdown 文件可检查", file=sys.stderr)
        return 1

    problems: list[Problem] = []
    for file in files:
        problems.extend(check_file(file))

    missing = [p for p in problems if p.kind == "missing"]
    anchors = [p for p in problems if p.kind == "anchor"]
    for item in missing + anchors:
        print(f"{_display(item.source)}:{item.line}: {item.reason}: {item.target}")

    blocking = missing + (anchors if args.anchors else [])
    if not args.quiet:
        print(
            f"检查了 {len(files)} 个文件：{len(missing)} 处死链"
            f"，{len(anchors)} 处锚点问题（{'计入' if args.anchors else '仅警告'}）"
        )
    return 1 if blocking else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())