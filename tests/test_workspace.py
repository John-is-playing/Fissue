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


# ---------------------------------------------------------------------------
# 容器可写的 `.git` 不得变成宿主的执行入口
# ---------------------------------------------------------------------------


def test_host_git_ignores_container_written_hooks(tmp_path: Path) -> None:
    """容器写下的 `.git/hooks/pre-push` 绝不能在宿主 push 时被执行。

    攻击链：验证器要把生成的测试文件写进工作区，所以工作区是 **rw** 挂进容器的，而容器里
    跑的是不可信 PR 代码与模型生成的验证器、uid 与宿主同用户 → 它能写下 `pre-push`；
    宿主的 `git push` 会在子进程环境里**带着平台令牌**执行它（见 ``RepoWorkspace.push``）。
    也就是说这条钩子等于「容器逃逸到宿主」的一条通路。
    """
    remote = tmp_path / "remote.git"
    _run_git(["init", "--bare", "-q", str(remote)])
    root = _repo_with_commit(tmp_path)

    marker = tmp_path / "pwned"
    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)

    result = RepoWorkspace(root=root, slug="a/b").push(str(remote), "fissue/branch")

    assert result.ok, f"push 应当成功，实际 stderr：{result.stderr}"
    assert not marker.exists(), "容器写下的 pre-push 钩子在宿主被执行了"


def test_harden_git_drops_execution_entries_keeps_remote_and_identity(tmp_path: Path) -> None:
    """`harden_git` 清掉 `.git/config` 里的执行入口，但保留 remote 与提交身份。

    `git add` 会跑 `filter.*.clean`、`git diff` 会跑 `diff.*.textconv` / `diff.external`，
    而这些键都在**容器可写的** `.git/config` 里；`core.hooksPath` 还能把钩子目录指到别处。
    ``harden_git`` 的做法是重建配置（只写自己决定内容的项），并删掉整个 `.git/hooks`。
    """
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b", origin_url="https://example.invalid/o/r.git")
    _run_git(["config", "filter.x.clean", "sh -c 'touch /tmp/pwned'"], cwd=root)
    _run_git(["config", "diff.x.textconv", "sh -c 'touch /tmp/pwned'"], cwd=root)
    _run_git(["config", "core.hooksPath", "/tmp/evil-hooks"], cwd=root)
    (root / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\ntouch x\n", encoding="utf-8")

    ws.harden_git()

    cfg = (root / ".git" / "config").read_text(encoding="utf-8")
    assert "filter" not in cfg, "filter.*.clean 会在 git add 时执行"
    assert "textconv" not in cfg, "diff.*.textconv 会在 git diff 时执行"
    assert "hooksPath" not in cfg
    assert "https://example.invalid/o/r.git" in cfg, "remote.origin.url 要写回（fetch 依赖它）"
    assert "t@t" in cfg, "提交身份要带过来，否则 commit 直接失败"
    assert not (root / ".git" / "hooks").exists(), "钩子目录整个删掉"
    # 加固后仓库仍可用（能读到 remote 配置）
    assert ws._git(["git", "config", "--get", "remote.origin.url"]).stdout.strip() == (
        "https://example.invalid/o/r.git"
    )


def test_harden_git_does_not_follow_symlinked_config(tmp_path: Path) -> None:
    """`.git/config` 被换成符号链接时，绝不能顺着链接把宿主上别的文件覆盖掉。"""
    root = _repo_with_commit(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    cfg = root / ".git" / "config"
    cfg.unlink()
    cfg.symlink_to(victim)

    ws = RepoWorkspace(root=root, slug="a/b", origin_url="https://example.invalid/o/r.git")
    ws.harden_git()

    assert victim.read_text(encoding="utf-8") == "keep me\n", "受害文件被写坏了"
    assert not cfg.is_symlink(), "应当换成一个真实文件"


def test_harden_argv_puts_c_before_subcommand() -> None:
    """`-c` 必须排在子命令**之前**（放后面会被 git 报「未知开关 `c'」）。"""
    argv = RepoWorkspace._harden_argv(["git", "commit", "-m", "x"])
    assert argv[:2] == ["git", "-c"]
    assert argv[2] == f"core.hooksPath={RepoWorkspace.NO_HOOKS_PATH}"
    assert argv.index("-c") < argv.index("commit")
    # 非 git 命令原样返回（例如直接调可执行文件）
    assert RepoWorkspace._harden_argv(["/usr/bin/echo", "hi"]) == ["/usr/bin/echo", "hi"]
