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


def test_remote_branch_sha_reports_existence(tmp_path: Path) -> None:
    """``remote_branch_sha`` 只读地问远端：不存在给空串，存在给 sha。"""
    remote = tmp_path / "remote.git"
    _run_git(["init", "--bare", "-q", str(remote)])
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b")

    assert ws.remote_branch_sha(str(remote), "fissue/nope") == "", "不存在的分支应当是空串"
    assert ws.push(str(remote), "fissue/here").ok
    sha = ws.remote_branch_sha(str(remote), "fissue/here")
    assert sha is not None and len(sha) == 40


def test_push_uses_an_explicit_lease_when_the_branch_already_exists(tmp_path: Path, monkeypatch) -> None:
    """远端已有同名分支时，必须用**精确租约**，而不是裸 ``--force-with-lease``。

    回归背景（真跑实测）：这里推的是 URL、修复分支又从未 fetch 过，本地没有对应的
    remote-tracking ref——裸 lease 拿不到期望值，于是只要远端已有该分支就一律
    ``stale info`` 拒绝：三个探针仓库那一轮 6 次推送全被拒、PR 一个也没建出来。
    而分支名是「条目 + 内容哈希」的确定性名字，**远端已有才是常态**。
    """
    import re

    import fissue.workspace as ws_mod

    remote = tmp_path / "remote.git"
    _run_git(["init", "--bare", "-q", str(remote)])
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b")
    branch = "fissue/dup"
    assert ws.push(str(remote), branch).ok, "第一次推送：远端还没有，普通推送即可"

    captured: list[str] = []
    real_run = ws_mod.subprocess.run

    def _spy(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(" ".join(str(c) for c in cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ws_mod.subprocess, "run", _spy)
    _run_git(["commit", "--allow-empty", "-qm", "second"], cwd=root)

    result = ws.push(str(remote), branch, force=True)

    assert result.ok, f"已有分支时也该推得动，实际 stderr：{result.stderr}"
    push_argv = [a for a in captured if " push " in f" {a} "]
    assert push_argv, "应当真的调用了一次 push"
    argv = push_argv[-1]
    assert re.search(rf"--force-with-lease=refs/heads/{branch}:[0-9a-f]{{40}}", argv), (
        f"应当带远端 sha 的精确租约，实际：{argv}"
    )
    assert " --force-with-lease " not in f" {argv} ", "不该再出现不带期望值的裸 lease"


def test_push_skips_the_lease_for_a_brand_new_branch(tmp_path: Path, monkeypatch) -> None:
    """远端没有这个分支时**不加租约**：本来无可覆盖，普通推送即可。"""
    import fissue.workspace as ws_mod

    remote = tmp_path / "remote.git"
    _run_git(["init", "--bare", "-q", str(remote)])
    root = _repo_with_commit(tmp_path)

    captured: list[str] = []
    real_run = ws_mod.subprocess.run

    def _spy(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(" ".join(str(c) for c in cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ws_mod.subprocess, "run", _spy)

    result = RepoWorkspace(root=root, slug="a/b").push(str(remote), "fissue/brand-new", force=True)

    assert result.ok, result.stderr
    push_argv = [a for a in captured if " push " in f" {a} "]
    assert push_argv and "--force-with-lease" not in push_argv[-1], (
        f"新分支不该带租约，实际：{push_argv[-1] if push_argv else '(没有 push)'}"
    )


def test_push_refuses_to_force_when_the_remote_cannot_be_probed(tmp_path: Path, monkeypatch) -> None:
    """问不到远端状态时**不许强推**——宁可响亮失败，也别给出误导性的 ``fetch first``。

    回归背景（真跑实测）：那条修复分支的 ``ls-remote`` 撞上一次瞬时网络故障，而旧实现把
    「问不到」当成「远端没有这个分支」→ 普通推送 → 报 ``! [rejected] (fetch first)``，
    读起来像「远端有别人的提交」，实际只是探测失败，排查被绕了一圈。
    """
    import fissue.workspace as ws_mod

    remote = tmp_path / "remote.git"
    _run_git(["init", "--bare", "-q", str(remote)])
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b")

    seen: list[str] = []
    real_run = ws_mod.subprocess.run

    class _ProbeFail:
        returncode, stdout, stderr = 128, "", "fatal: 无法访问（模拟网络故障）"

    def _spy(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        argv = " ".join(str(c) for c in cmd)
        seen.append(argv)
        if "ls-remote" in argv:
            return _ProbeFail()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(ws_mod.subprocess, "run", _spy)

    result = ws.push(str(remote), "fissue/cannot-probe", force=True)

    assert not result.ok, "问不到远端状态时不该硬推"
    assert "拒绝强推" in result.stderr, f"要给可操作的原因，实际：{result.stderr}"
    assert not any(" push " in f" {a} " for a in seen), "探测失败时连 push 都不该发起"

    assert ws.remote_branch_sha(str(remote), "fissue/cannot-probe") is None, "探测失败应当返回 None"

    monkeypatch.setattr(ws_mod.subprocess, "run", real_run)
    assert ws.remote_branch_sha(str(remote), "fissue/cannot-probe") == "", (
        "恢复真实执行后确认：远端确实没有这个分支（空串＝确认不存在，与 None 是两回事）"
    )


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


# ---------------------------------------------------------------------------
# 提交前的完整性校验（索引 / HEAD 都在容器可写范围内）
# ---------------------------------------------------------------------------


def test_reset_index_drops_container_staged_extras(tmp_path: Path) -> None:
    """容器塞进索引的内容不能跟着补丁被推送出去。

    攻击面：容器对 `.git` 可写，而 `git commit` 提交的是**索引**、不是工作区——把别的内容
    塞进索引，「只 stage Agent 改过的文件」这条护栏就被绕过了。`reset_index` 只重置索引、
    不动工作区（`read-tree --reset` 不带 `-u`）。
    """
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b")

    evil = root / "evil.txt"
    evil.write_text("注入的内容\n", encoding="utf-8")
    _run_git(["add", "evil.txt"], cwd=root)          # 模拟容器把它塞进索引
    assert ws.staged_files() == ["evil.txt"]

    assert ws.reset_index() is True

    assert ws.staged_files() == [], "索引必须被清回 HEAD"
    assert evil.exists(), "工作区文件不能被删（read-tree 不带 -u）"


def test_staged_files_reports_what_will_commit(tmp_path: Path) -> None:
    """`staged_files` 报出的就是这次 commit 会包含的文件（用于「正好是 Agent 改过的那些」）。"""
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b")
    (root / "a.txt").write_text("hi\nmore\n", encoding="utf-8")

    assert ws.stage_paths(["a.txt"]) is True
    assert ws.staged_files() == ["a.txt"]


def test_assert_on_branch_catches_redirection(tmp_path: Path) -> None:
    """`.git/HEAD` 被改到别的分支时必须拒绝提交——否则会推错分支。"""
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b")

    assert ws.assert_on_branch(ws.current_branch()) is None, "在自己的分支上应当放行"

    msg = ws.assert_on_branch("fissue/other")
    assert msg and "拒绝提交" in msg, f"应当拒绝：{msg}"
