#!/usr/bin/env python3
"""把 ``docs/wiki/`` 发布到平台 Wiki（GitHub / AtomGit）。

为什么需要这么一个脚本
----------------------
平台的 Wiki **不是仓库里的目录，而是另一个 git 仓库**（``<repo>.wiki.git``）。
把 ``docs/wiki/`` 推到 ``main`` 对 Wiki 标签页**没有任何影响**——这一点最容易误解，
于是出现「仓库里明明有一套 wiki，平台上却空空如也」。

而且 GitHub 的 wiki 仓库是**懒创建**的：必须先有人在网页上点「创建第一页」保存一次，
它才存在；在那之前 ``git push`` 会被拒（``Repository not found``），而且**没有任何 API
能代做**（``POST /repos`` 建以 ``.wiki`` 结尾的仓库会被平台直接拒）。所以本脚本在推之前
会先把这件事查清楚，并给出可照做的下一步，而不是把 git 的原始报错丢给你——那个报错
看着像权限问题，其实不是。

转换规则（为什么必须转）
------------------------
* **页间链接去掉 ``.md``**：Wiki 渲染器按页面名互相引用，``Overview.md`` 会被当成一个
  叫「Overview.md」的页面而 404；
* **指向仓库文档的链接换成完整 URL**：``../AUTH.md`` 在 wiki 仓库里没有对应文件，
  只能指向仓库的网页地址（``<base-url>/blob/<branch>/docs/AUTH.md``）；
* ``Home`` / ``_Sidebar`` / ``_Footer`` 文件名**保持不变**（它们正是 wiki 的特殊页）。

两条不越界的规矩
----------------
* **代码块与行内代码里的链接一律不动**：``Home.md`` 里就有一句举例（``[概览](Overview)``
  与 ``../xxx.md`` 写在反引号里），照字面替换会把说明文字本身改坏；
* 认不出来的链接形状**原样保留并告警**，绝不猜着改；源目录（``docs/wiki/``）**只读**。

用法
----
::

    python scripts/publish_wiki.py                      # 干跑：只报告会改什么（默认）
    python scripts/publish_wiki.py --out build/wiki      # 把转换结果写到目录
    python scripts/publish_wiki.py --push --remote ssh://git@ssh.github.com:443/John-is-playing/Fissue.wiki.git
    python scripts/publish_wiki.py --push --base-url https://atomgit.com/qiaoen_python/Fissue --remote <atomgit 的 wiki 地址>
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 默认的仓库网页地址（用于把 `../xxx.md` 拼成完整 URL）。
DEFAULT_BASE_URL = "https://github.com/John-is-playing/Fissue"
#: blob 链接里的分支名。
DEFAULT_BRANCH = "main"
#: 源目录（仓库内的 wiki 源，相对仓库根）。
DEFAULT_SRC = "docs/wiki"

#: markdown 链接：``[文本](目标)``。目标里不取空格（写成 ``<...>`` 的空格链接本就少见）。
_LINK = re.compile(r"\]\(([^)\s]+)\)")
#: 围栏代码块的标记：**必须行首**（最多三个空格缩进）。行内出现的三反引号不算围栏。
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
#: 连续的反引号段（行内代码靠两段等长的反引号配对）。
_BACKTICK_RUN = re.compile(r"`+")
#: 不该被改写的目标开头。
_SKIP_PREFIXES = ("http://", "https://", "mailto:", "/", "#")


@dataclass(frozen=True)
class Change:
    """一处改写：``kind`` 是 ``page``（页间链接）或 ``doc``（仓库文档链接）。"""

    kind: str
    before: str
    after: str


def split_code(text: str) -> list[tuple[bool, str]]:
    """切成 ``(是否代码, 片段)``：围栏代码块**整块**算代码，行内代码逐行配对。

    为什么按行判定、而不是拿一条 ``.*?`` 正则通吃（这里踩过）：

        本仓库的 ``AI-Layer.md`` 正文里**行内**提到过一次三反引号，一条吃全文的正则
        会把它当成围栏开启，于是从那之后代码/非代码的判定整体错位一格——第 42~99 行
        被当成代码块，正文里那条带锚点的链接因此被**静默漏改**（干跑时表现为
        「有的链接改了、有的没改」，很难看出规律）。

    围栏必须出现在行首（最多三个空格缩进）才算，这是 CommonMark 的规则，也让判定变成
    确定性的：一条 `` ``` `` 只影响它自己那一行的行内代码配对，绝不会把后面整片文档吃掉。
    """
    parts: list[tuple[bool, str]] = []
    fence_char: str | None = None      # 当前打开的围栏用的是什么字符
    fence_len = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        marker = match.group(1) if match else ""
        if fence_char is None:
            if marker:
                fence_char, fence_len = marker[0], len(marker)
                parts.append((True, line))          # 围栏行本身也是代码
                continue
            parts.extend(_split_inline_code(line))
        else:
            parts.append((True, line))
            if marker and marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None                    # 收尾围栏
    return parts


def _split_inline_code(line: str) -> list[tuple[bool, str]]:
    """把一行按**行内代码**切开（CommonMark 口径：等长反引号段配对，落单的按普通文本）。

    行内代码不跨行，所以这一步的爆炸半径最多是这一行——这正是上面那个坑不会复发的原因。
    """
    runs = list(_BACKTICK_RUN.finditer(line))
    parts: list[tuple[bool, str]] = []
    cursor = 0
    index = 0
    while index < len(runs):
        close = next(
            (j for j in range(index + 1, len(runs)) if len(runs[j].group(0)) == len(runs[index].group(0))),
            None,
        )
        if close is None:
            break                                    # 落单的反引号：当普通文本
        start, end = runs[index].start(), runs[close].end()
        if start > cursor:
            parts.append((False, line[cursor:start]))
        parts.append((True, line[start:end]))
        cursor = end
        index = close + 1
    if cursor < len(line):
        parts.append((False, line[cursor:]))
    return parts


def rewrite_target(
    target: str, *, base_url: str, branch: str, src: str, repo_root: Path | None = None
) -> tuple[str, Change | None, str | None]:
    """改一个链接目标，返回 ``(新目标, 改写记录, 告警)``。

    路径的判定方式是把目标**相对源目录解析**，而不是去猜前缀：
    ``/docs/wiki`` + ``../AUTH.md`` → ``docs/AUTH.md``（仓库文档）；
    ``/docs/wiki`` + ``Overview.md`` → ``docs/wiki/Overview.md``（wiki 页）。
    这样 ``../``、``../../``、``./`` 甚至更深的层级都由同一套逻辑覆盖。

    仓库内的链接**不限于 ``.md``**：``../../LICENSE``、图片、脚本都算。原先只认 ``.md``，
    其余「保守跳过」——结果是那些链接在 wiki 里一律 404（实测 ``Contributing.md`` 与
    ``_Footer.md`` 各有一条 ``../../LICENSE``）。既然它们的目标在仓库里真实存在，
    就该一并换成完整 URL；给出 ``repo_root`` 时还能区分文件与目录（``blob`` / ``tree``）。
    """
    if not target or target.startswith(_SKIP_PREFIXES):
        return target, None, None

    path, _, anchor = target.partition("#")
    if not path:
        return target, None, None

    resolved = posixpath.normpath(posixpath.join(src, path))
    if resolved.startswith("../") or resolved == "..":
        return target, None, f"链接指向仓库之外，原样保留：{target}"

    if resolved == src or resolved.startswith(f"{src}/"):
        page = "" if resolved == src else resolved[len(src) + 1 :]
        if not page.endswith(".md"):
            # 无扩展名的裸页名 = **已经是 wiki 形式**：静默放过。
            # 这一条是为了可重复运行——否则第二次跑会把「已经转好的链接」全报一遍告警，
            # 而那种告警只会让人以为出了问题。带别的扩展名则是真问题，照样告警。
            if "." not in posixpath.basename(page):
                return target, None, None
            return target, None, f"wiki 目录里非 .md 的链接，原样保留：{target}"
        if "/" in page:
            return target, None, f"wiki 页链接带子目录（wiki 是扁平命名），原样保留：{target}"
        new = page[: -len(".md")] + (f"#{anchor}" if anchor else "")
        return new, Change("page", target, new), None

    is_dir = repo_root is not None and (repo_root / resolved).is_dir()
    kind = "tree" if is_dir else "blob"
    new = f"{base_url.rstrip('/')}/{kind}/{branch}/{resolved}" + (f"#{anchor}" if anchor else "")
    return new, Change("doc", target, new), None


def rewrite_links(
    text: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    branch: str = DEFAULT_BRANCH,
    src: str = DEFAULT_SRC,
    repo_root: Path | None = None,
) -> tuple[str, list[Change], list[str]]:
    """按 wiki 渲染器的要求改写真个文件的链接，返回 ``(新文本, 改写列表, 告警列表)``。"""
    changes: list[Change] = []
    warnings: list[str] = []
    out: list[str] = []

    for is_code, chunk in split_code(text):
        if is_code:                      # 围栏代码块与行内代码：一律原样
            out.append(chunk)
            continue

        def replace(match: re.Match[str]) -> str:
            new, change, warning = rewrite_target(
                match.group(1), base_url=base_url, branch=branch, src=src, repo_root=repo_root
            )
            if change is not None:
                changes.append(change)
            if warning is not None and warning not in warnings:
                warnings.append(warning)
            return f"]({new})"

        out.append(_LINK.sub(replace, chunk))

    return "".join(out), changes, warnings


def transform_all(
    src_dir: Path,
    *,
    base_url: str,
    branch: str,
    src_rel: str,
    repo_root: Path | None = None,
) -> dict[str, tuple[str, list[Change], list[str]]]:
    """转换源目录下的全部 ``*.md``，返回 ``文件名 → (新文本, 改写, 告警)``。"""
    result: dict[str, tuple[str, list[Change], list[str]]] = {}
    for path in sorted(src_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        result[path.name] = rewrite_links(
            text, base_url=base_url, branch=branch, src=src_rel, repo_root=repo_root
        )
    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def report(files: dict[str, tuple[str, list[Change], list[str]]], *, limit: int = 3) -> int:
    """干跑报告。返回「改写了多少处」的总数。"""
    total = 0
    for name, (_, changes, warnings) in files.items():
        pages = [c for c in changes if c.kind == "page"]
        docs = [c for c in changes if c.kind == "doc"]
        total += len(changes)
        print(f"  {name}: 页间链接 {len(pages)} 处、仓库文档链接 {len(docs)} 处")
        for change in changes[:limit]:
            print(f"      {change.before}  →  {change.after}")
        if len(changes) > limit:
            print(f"      …（另有 {len(changes) - limit} 处）")
        for warning in warnings:
            print(f"      ⚠️  {warning}", file=sys.stderr)
    print(f"\n共 {len(files)} 个文件、{total} 处改写")
    return total


def write_out(files: dict[str, tuple[str, list[Change], list[str]]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, (text, _changes, _warnings) in files.items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"已写出 {len(files)} 个文件到 {out_dir}")


# ---------------------------------------------------------------------------
# 推送到平台
# ---------------------------------------------------------------------------


def _git(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """跑一条 git 命令。

    ``GIT_TERMINAL_PROMPT=0`` 不能省：本脚本常在非交互环境里跑，一旦需要认证，
    git 会**卡在提示符上**直到超时，看着像「卡死」而不是「认证失败」。
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, env=env
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：\n{result.stderr.strip()}")
    return result


def _identity() -> tuple[str, str]:
    """提交用身份：优先用仓库里配的，没有就用一个明确标出来的兜底，免得 git 直接拒绝提交。"""
    name = _git(["config", "--get", "user.name"], check=False).stdout.strip() or "AtomCode (deepseek-flash)"
    email = (
        _git(["config", "--get", "user.email"], check=False).stdout.strip() or "noreply@atomgit.com"
    )
    return name, email


def push_to_wiki(
    files: dict[str, tuple[str, list[Change], list[str]]],
    *,
    remote: str,
    web_url: str,
    prune: bool,
    message: str,
) -> None:
    """把转换结果推到 wiki 仓库。

    为什么要先 clone 再覆盖：wiki 仓库可能有网页上改过的内容。默认只**写我们这套页**，
    额外发现的文件只报告、不删；要镜像式同步得显式 ``--prune``——省得脚本顺手删掉
    别人在网页上写的东西。
    """
    with tempfile.TemporaryDirectory(prefix="fissue-wiki-") as tmp:
        target = Path(tmp) / "wiki"
        clone = _git(["clone", "--quiet", remote, str(target)], check=False)
        if clone.returncode != 0:
            raise SystemExit(
                "推不上去：wiki 仓库还不存在（或地址不对）。\n"
                "  git 的原话：" + (clone.stderr.strip().splitlines() or ["(无输出)"])[0] + "\n\n"
                f"这不是权限问题。请先在浏览器打开 {web_url}/wiki 点「创建第一页」并保存一次——\n"
                "  GitHub 的 wiki 仓库是**懒创建**的：在那之前 git 推不上去，也没有任何 API 能代做。\n"
                "  保存完再重跑本命令即可（那第一页会被本次推送覆盖掉）。"
            )

        ours = set(files)
        existing = {p.name for p in target.iterdir() if p.is_file() and p.name != ".git"}
        extra = sorted(existing - ours)
        missing = sorted(ours - existing)

        for name, (text, _changes, _warnings) in files.items():
            (target / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"写入 {len(files)} 个页面" + (f"（新增 {len(missing)} 个）" if missing else ""))
        if extra:
            print(f"wiki 仓库里另有 {len(extra)} 个不在这套源里的文件：{'、'.join(extra)}")
            if prune:
                for name in extra:
                    (target / name).unlink()
                print("  已按 --prune 删除它们")
            else:
                print("  默认保留（要以仓库为准做镜像同步，加 --prune）")

        _git(["add", "-A"], cwd=target)
        if not _git(["status", "--porcelain"], cwd=target).stdout.strip():
            print("内容没有变化，无需推送。")
            return
        name, email = _identity()
        _git(
            ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", message],
            cwd=target,
        )
        _git(["push", "--quiet"], cwd=target)
        sha = _git(["rev-parse", "--short", "HEAD"], cwd=target).stdout.strip()
        print(f"✅ 已推送：{sha} → {remote}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 docs/wiki/ 发布到平台 Wiki（默认只干跑，不写任何东西）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo-root", default=".", help="仓库根（默认当前目录）")
    parser.add_argument("--src", default=DEFAULT_SRC, help=f"wiki 源目录（默认 {DEFAULT_SRC}）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="仓库网页地址，用于拼仓库文档链接")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="仓库默认分支（用于 blob 链接）")
    parser.add_argument("--out", help="把转换结果写到这个目录（不碰 git）")
    parser.add_argument("--push", action="store_true", help="推送到 wiki 仓库（需要 --remote）")
    parser.add_argument("--remote", help="wiki 仓库的 git 地址（如 ssh://git@ssh.github.com:443/OWNER/REPO.wiki.git）")
    parser.add_argument("--prune", action="store_true", help="推送时删除 wiki 仓库里不在这套源里的文件")
    parser.add_argument("--message", default="docs(wiki): 从仓库 docs/wiki/ 同步", help="提交信息")
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    src_dir = (root / args.src).resolve()
    if not src_dir.is_dir():
        print(f"找不到 wiki 源目录：{src_dir}", file=sys.stderr)
        return 2
    if args.push and not args.remote:
        print("--push 需要同时给 --remote <wiki 仓库地址>", file=sys.stderr)
        return 2

    src_rel = Path(args.src).as_posix()
    files = transform_all(
        src_dir, base_url=args.base_url, branch=args.branch, src_rel=src_rel, repo_root=root
    )
    if not files:
        print(f"{src_dir} 里没有任何 .md", file=sys.stderr)
        return 2

    print(f"源：{src_dir}（只读）")
    report(files)

    if args.out:
        write_out(files, Path(args.out))
    if args.push:
        push_to_wiki(
            files,
            remote=args.remote or "",
            web_url=args.base_url.rstrip("/"),
            prune=args.prune,
            message=args.message,
        )
    if not args.out and not args.push:
        print("\n（干跑：什么都没写。加 --out DIR 写出文件，或 --push --remote URL 直接发布。）")
        print("（发布前注意：平台上得先有第一页，wiki 仓库才会存在——见脚本开头的说明。）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
