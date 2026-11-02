#!/usr/bin/env python3
"""发布一个版本：抽 CHANGELOG 段 → 建分支与附注 tag → 双平台 push → 双平台 Release。

为什么要有它
------------
`v1.3.1` 那次发布是**照着记忆手敲**的：改 `pyproject.toml` 与 `__version__`、把
`[Unreleased]` 转正、建 `rel/<版本>-rel` 分支、打附注 tag、`git push` 两个远端
（GitHub 还得走 `ssh.github.com:443`，因为 22 端口在**本机**被挡）、再用两套 API 建
Release。每一步都不难，但**顺序与口径**靠人记：漏掉「正文取 CHANGELOG 对应段落」，
两个平台的 Release 就会长得不一样（v1.3.1 的 AtomGit 正文就是手工缩写的）。

既然发布是「一串确定命令 + 两条 HTTP」，就把它写成脚本，让人只决定**版本号**。

设计上的几条硬规矩
------------------
* **默认干跑**：不带 ``--yes`` 只报告会做什么（与 `scripts/publish_wiki.py` 同一套习惯）。
  发布是不可逆动作，默认值必须偏向「什么都没发生」。
* **先体检再动手**：版本号三处（``pyproject.toml`` / ``__version__`` / CHANGELOG 段）
  必须自洽、工作区必须干净。这些正是手工发布最容易漏、且事后最难查的两件事。
* **tag 打在 rel 分支上**：与历来快照一致（`rel/<版本>-b<N>` 一族），分支与 tag 指向
  同一个提交，便于日后按分支重放。
* **平台口径一致**：正文就是 CHANGELOG 那一段原文，不缩写、不改写；某个平台缺令牌时
  **如实跳过并给出补法**，而不是悄悄少发一个 Release。
* **GitHub 走 ssh.github.com:443**：`github.com:22` 与 `:443` 在部分网络里都被拒，
  唯一稳定通道是 `ssh.github.com:443`。脚本把它写成常量，省得每次靠记忆。

用法
----
::

    python scripts/release.py 1.3.2                 # 干跑：体检 + 打印计划
    python scripts/release.py 1.3.2 --yes           # 真做：分支 + tag + push + 双平台 Release
    python scripts/release.py 1.3.2 --yes --only github   # 只推 GitHub（另一个平台稍后补）
    python scripts/release.py 1.3.2 --yes --no-push       # 只建本地分支与 tag
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
VERSION_FILE = REPO_ROOT / "src" / "fissue" / "__init__.py"

GITHUB_SLUG = "John-is-playing/Fissue"
GITHUB_API = "https://api.github.com"
#: 推送地址与「人类用的 remote」分开写：GitHub 的 22 / 443 端口在不少网络里被拒，
#: 唯一稳定通道是 ssh.github.com:443；AtomGit 走 HTTPS（凭据由 credential helper 提供）。
GITHUB_PUSH_URL = "ssh://git@ssh.github.com:443/John-is-playing/Fissue.git"
ATOMGIT_REMOTE = "atomgit"
ATOMGIT_SLUG = "qiaoen_python/Fissue"
ATOMGIT_API = "https://api.atomgit.com/api/v5"


# ---------------------------------------------------------------------------
# 纯函数：解析与体检（不碰网络、不碰 git，单测直接钉）
# ---------------------------------------------------------------------------


def section_body(changelog: str, version: str) -> str | None:
    """取 ``## [<version>]`` 那一段的正文；没有这一段返回 ``None``。

    边界按**下一个二级标题**算，而不是按空行或 ``---``：发布段里本来就散落着
    ``---``（Markdown 分隔线），按它截断会把正文砍掉一半。段尾的 ``---`` 与空行顺手
    去掉——它们属于排版，不属于这一版的说明。
    """
    heading = f"## [{version}]"
    lines = changelog.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith(heading):
            start = index + 1
            break
    if start is None:
        return None

    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## ["):
            break
        body.append(line)
    while body and (not body[-1].strip() or body[-1].strip() == "---"):
        body.pop()
    while body and not body[0].strip():
        body.pop(0)
    return "\n".join(body) if body else None


def read_version(pyproject: str) -> str | None:
    """从 ``pyproject.toml`` 里取 ``version = "x.y.z"``。"""
    for line in pyproject.splitlines():
        stripped = line.strip()
        if stripped.startswith("version ="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def read_dunder_version(source: str) -> str | None:
    """从 ``__init__.py`` 里取 ``__version__ = "x.y.z"``。"""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("__version__"):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def preflight(
    version: str, *, changelog: str, pyproject: str, dunder: str | None, dirty: list[str]
) -> list[str]:
    """发布前体检，返回问题清单（空表示可以发）。

    为什么这些检查值一笔代码：它们全是**手工发布时最容易漏**的点，而且漏了之后
    症状都很难查——版本号不一致会打出一个内容与标签对不上的 Release；CHANGELOG 缺段
    会让 Release 正文变成空；工作区脏则会让「这个 tag 到底对应哪份代码」永久说不清。
    """
    problems: list[str] = []
    project_version = read_version(pyproject)
    if project_version != version:
        problems.append(
            f"pyproject.toml 的 version 是 {project_version!r}，与要发布的 {version!r} 不一致"
        )
    if dunder != version:
        problems.append(
            f"src/fissue/__init__.py 的 __version__ 是 {dunder!r}，与要发布的 {version!r} 不一致"
        )
    if section_body(changelog, version) is None:
        problems.append(
            f"CHANGELOG.md 里没有 `## [{version}]` 段——Release 正文会变成空"
        )
    if dirty:
        problems.append(
            "工作区不干净（发布前先提交，否则 tag 对应的代码与磁盘上的不是同一份）：\n  "
            + "\n  ".join(dirty[:10])
        )
    return problems


def build_plan(version: str) -> dict[str, Any]:
    """发布计划：分支名、tag 名、Release 标题。

    命名口径与历史一致：分支 ``rel/<版本>-rel``、附注 tag ``v<版本>-rel``，两者指向同一
    提交；快照一族用的是 ``rel/<版本>-b<N>``，正式发布用 ``-rel`` 后缀区分。
    """
    tag = f"v{version}-rel"
    return {
        "version": version,
        "tag": tag,
        "branch": f"rel/{version}-rel",
        "name": f"Fissue v{version}",
    }


# ---------------------------------------------------------------------------
# 有副作用的部分：git 与两条 HTTP（调用点薄，逻辑都在上面）
# ---------------------------------------------------------------------------


def _run_git(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """跑一条 git 命令。失败直接抛，由调用方决定停在哪儿。"""
    merged = {**os.environ, **(env or {})}
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（exit {proc.returncode}）："
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc


def worktree_changes() -> list[str]:
    """当前工作区里未提交的文件（发布前必须为空）。"""
    proc = _run_git(["status", "--porcelain"])
    return [line for line in proc.stdout.splitlines() if line.strip()]


def push_platforms(plan: dict[str, Any], *, platforms: tuple[str, ...]) -> list[str]:
    """把分支与 tag 推到指定平台，返回做了哪些事（便于干跑与日志对照）。"""
    done: list[str] = []
    base = [plan["branch"], plan["tag"]]
    if "github" in platforms:
        _run_git(
            ["push", GITHUB_PUSH_URL, *base],
            env={
                # BatchMode：没有可用公钥时立刻失败，而不是挂在交互提示上（无人值守会直接超时）。
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        done.append(f"github ← {plan['branch']} / {plan['tag']}")
    if "atomgit" in platforms:
        _run_git(["push", ATOMGIT_REMOTE, *base], env={"GIT_TERMINAL_PROMPT": "0"})
        done.append(f"atomgit ← {plan['branch']} / {plan['tag']}")
    return done


def _http_post(url: str, payload: dict[str, Any], token: str) -> tuple[int, str]:
    """POST 一条 JSON，返回 (状态码, 响应文本)。

    单独抽出来是为了让用例能只替换这一层：发布脚本的网络逻辑本来就该薄到「没有可测的
    东西」，可测的是「发出去的 payload 长什么样」。
    """
    import httpx
    import truststore

    # 系统信任库：企业代理 / 抓包工具的根证书不在 certifi 里（见 fissue/net.py 的同一坑）。
    truststore.inject_into_ssl()
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        return resp.status_code, resp.text


def gitlab_like_payload(plan: dict[str, Any], body: str) -> dict[str, Any]:
    """AtomGit（GitLab v5 形状）建 Release 的 payload。"""
    return {
        "tag_name": plan["tag"],
        "target_commitish": plan["branch"],
        "name": plan["name"],
        "body": body,
        "prerelease": False,
        # AtomGit 用这个字段决定「Latest」角标；GitHub 侧靠「最新发布」自动推断。
        "release_status": "latest",
    }


def github_payload(plan: dict[str, Any], body: str) -> dict[str, Any]:
    """GitHub 建 Release 的 payload。"""
    return {
        "tag_name": plan["tag"],
        "target_commitish": plan["branch"],
        "name": plan["name"],
        "body": body,
        "draft": False,
        "prerelease": False,
    }


def create_releases(
    plan: dict[str, Any],
    body: str,
    *,
    platforms: tuple[str, ...],
    github_token: str | None,
    atomgit_token: str | None,
) -> list[str]:
    """在两个平台上各建一条 Release，返回「做了/跳过」的说明清单。

    缺令牌**不作为失败**：那是一个环境事实（本机 AtomGit 令牌就是空的），把它升级成
    错误只会让人半途而废、留下「一边有 Release 一边没有」的假成功。这里如实跳过并写明
    补法，退出码仍由「有没有真的失败」决定。
    """
    if not platforms:
        return []
    notes: list[str] = []
    if "github" in platforms:
        if not github_token:
            notes.append("github：跳过（没有 FISSUE_GITHUB_TOKEN）")
        else:
            status, text = _http_post(
                f"{GITHUB_API}/repos/{GITHUB_SLUG}/releases",
                github_payload(plan, body),
                github_token,
            )
            if status >= 300:
                notes.append(f"github：失败（HTTP {status}）{text[:200]}")
            else:
                notes.append(f"github：已创建 {plan['tag']}")
    if "atomgit" in platforms:
        if not atomgit_token:
            notes.append(
                "atomgit：跳过（FISSUE_ATOMGIT_TOKEN 为空）。可选：在仓库页手工建，"
                "或用带凭据的 atomgit_api 工具 POST /repos/qiaoen_python/Fissue/releases"
            )
        else:
            status, text = _http_post(
                f"{ATOMGIT_API}/repos/{ATOMGIT_SLUG}/releases",
                gitlab_like_payload(plan, body),
                atomgit_token,
            )
            if status >= 300:
                notes.append(f"atomgit：失败（HTTP {status}）{text[:200]}")
            else:
                notes.append(f"atomgit：已创建 {plan['tag']}")
    return notes


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="发布一个版本：分支 + 附注 tag + 双平台 push + 双平台 Release（默认干跑）",
    )
    parser.add_argument("version", help="版本号，如 1.3.2（不带 v 前缀）")
    parser.add_argument("--yes", action="store_true", help="真的执行（默认只体检并打印计划）")
    parser.add_argument(
        "--only",
        choices=["github", "atomgit"],
        action="append",
        default=None,
        help="只处理某个平台（可重复）；默认两个都做",
    )
    parser.add_argument("--no-push", action="store_true", help="只建本地分支与 tag，不推远端")
    parser.add_argument("--no-release", action="store_true", help="只推分支与 tag，不建 Release")
    args = parser.parse_args(argv)

    version = args.version.lstrip("v")
    plan = build_plan(version)
    changelog = _load_text(CHANGELOG)
    pyproject = _load_text(PYPROJECT)
    dunder = read_dunder_version(_load_text(VERSION_FILE))
    body = section_body(changelog, version)
    platforms = tuple(args.only) if args.only else ("github", "atomgit")

    try:
        dirty = worktree_changes()
    except RuntimeError as exc:  # 不在 git 仓库里 / git 不可用
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    problems = preflight(
        version, changelog=changelog, pyproject=pyproject, dunder=dunder, dirty=dirty
    )
    print(f"发布计划：{plan['name']}")
    print(f"  分支  {plan['branch']}")
    print(f"  tag   {plan['tag']}（附注 tag，打在分支同一提交）")
    print(f"  正文  CHANGELOG `## [{version}]` 段，{len(body or '')} 字符")
    print(f"  平台  {', '.join(platforms)}")
    if problems:
        print("\n✗ 体检未通过：")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if not args.yes:
        print("\n（干跑：加 --yes 才会真的建分支/tag/Release）")
        return 0

    try:
        _run_git(["branch", plan["branch"]])
        _run_git(["tag", "-a", plan["tag"], "-m", plan["name"]])
        print(f"✓ 本地已建分支与 tag：{plan['branch']} / {plan['tag']}")
        if not args.no_push:
            for line in push_platforms(plan, platforms=platforms):
                print(f"✓ {line}")
        if not args.no_release:
            for note in create_releases(
                plan,
                body or "",
                platforms=platforms,
                github_token=os.environ.get("FISSUE_GITHUB_TOKEN"),
                atomgit_token=os.environ.get("FISSUE_ATOMGIT_TOKEN"),
            ):
                print(f"· {note}")
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())