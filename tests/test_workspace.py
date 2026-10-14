"""``RepoWorkspace`` 的 git 集成测试：真跑 git，不打桩远端。

为什么需要单独一个文件：``push`` 是整条修复链路的最后一步，也是最容易被
「只测了片段」放过的一步——凭据助手脚本单独验过（``git credential fill`` 能取到
令牌），但**参数拼装从未真跑一次**，于是 ``-c`` 放错位置这种问题一路漏到真提 PR
才炸：Agent 明明修好了（验证器与回归门都通过），却死在一个「未知开关 `c'」上。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fissue.workspace import RepoWorkspace


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _repo_with_commit(tmp_path: Path) -> Path:
    """一个已提交过一次的普通仓库（push 需要 HEAD 存在）。"""
    root = tmp_path / "work" / "repo"
    root.mkdir(parents=True)
    _run_git(["init", "-q"], cwd=root)
    _run_git(["config", "user.email", "t@t"], cwd=root)
    _run_git(["config", "user.name", "t"], cwd=root)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    _run_git(["add", "-A"], cwd=root)
    _run_git(["commit", "-qm", "init"], cwd=root)
    return root


def test_push_reaches_a_real_remote(tmp_path: Path) -> None:
    """``push`` 必须真的推得动——只验凭据助手脚本是不够的。

    这条用例就是为「``-c`` 被放在 ``git push`` 之后」那个 bug 补的：``-c`` 是 git
    本体的选项，放错位置会直接报「未知开关 `c'」，推送全线失败、PR 一个也建不出来。
    """
    remote = tmp_path / "remote.git"
    _run_git(["init", "--bare", "-q", str(remote)])
    root = _repo_with_commit(tmp_path)

    result = RepoWorkspace(root=root, slug="a/b").push(
        str(remote), "fissue/test-branch", token="dummy-token"
    )

    assert result.ok, f"push 应当成功，实际 stderr：{result.stderr}"
    branches = _run_git(
        ["--git-dir", str(remote), "branch", "--list", "fissue/test-branch"]
    ).stdout
    assert "fissue/test-branch" in branches, "远端应当真的有这个分支"


def test_push_keeps_token_out_of_argv(tmp_path: Path, monkeypatch) -> None:
    """令牌只经子进程环境传递，绝不出现在命令行参数里。

    命令行对同机**任何用户**可见（``ps`` / ``/proc/<pid>/cmdline``），所以令牌拼进
    URL 或助手脚本文本都是不允许的；助手脚本里只应出现环境变量名。
    """
    remote = tmp_path / "remote.git"
    _run_git(["init", "--bare", "-q", str(remote)])
    root = _repo_with_commit(tmp_path)

    import fissue.workspace as ws_mod

    captured: list[list[str]] = []
    real_run = ws_mod.subprocess.run

    def _spy(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append([str(c) for c in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ws_mod.subprocess, "run", _spy)

    result = RepoWorkspace(root=root, slug="a/b").push(
        str(remote), "fissue/tok-branch", token="SK-SECRET-TOKEN"
    )

    assert result.ok, f"push 应当成功，实际 stderr：{result.stderr}"
    argv = " ".join(" ".join(call) for call in captured)
    assert "SK-SECRET-TOKEN" not in argv, "令牌绝不能出现在命令行上"
    assert "-c" in captured[-1], "应当用 -c 传凭据助手"


def test_clone_passes_token_via_env_not_argv(monkeypatch, tmp_path) -> None:
    """克隆私有仓库必须递凭据，且令牌不进命令行（与 push 同一条规矩）。

    回归：`RepoWorkspace.clone` 原先**从不**递令牌——`clone_url` 不带令牌、`_git` 又
    写死 `GIT_ASKPASS=echo`（禁止交互式索要），于是**公开仓库匿名克隆照常成功，
    私有仓库必然 `HTTP Basic: Access denied`**。实测：极狐上的 Private 仓库，同一个
    令牌直接 `git clone` 成功，只有 Fissue 这条路失败。
    """
    captured: list[tuple[list[str], dict]] = []

    class _Ok:
        ok = True
        stdout = ""
        stderr = ""

    def _spy(self, args, *, cwd=None, timeout=0, extra_env=None):
        captured.append((list(args), dict(extra_env or {})))
        return _Ok()

    monkeypatch.setattr(RepoWorkspace, "_git", _spy)

    RepoWorkspace.clone(
        "https://jihulab.com/o/r.git",
        slug="o/r",
        token="SK-CLONE-TOKEN",
        dest=tmp_path / "repo",
    )

    args, env = captured[0]
    argv = " ".join(args)
    assert "SK-CLONE-TOKEN" not in argv, "令牌绝不能出现在命令行上"
    assert "credential.helper=" in argv, "应当用 -c 传凭据助手"
    # `-c` 是 git 本体的选项，必须在子命令**之前**——放后面会被报「未知开关」
    assert args.index("-c") < args.index("clone"), "-c 必须在 clone 之前"
    assert env.get(RepoWorkspace.PUSH_TOKEN_ENV) == "SK-CLONE-TOKEN", "令牌要经环境变量递"
