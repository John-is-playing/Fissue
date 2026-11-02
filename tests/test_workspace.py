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
from typing import Any

import pytest

from fissue.workspace import GitError, RepoWorkspace


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
# 克隆镜像：先镜像（匿名）→ 再直连（带令牌）
# ---------------------------------------------------------------------------


class _GitStub:
    """把 ``_git`` 换成按 URL 决定成败的探针，两次尝试的实参全记下来。"""

    ok = True
    stdout = ""
    stderr = ""

    def __init__(self, *, fail_substr: str = "", fail_all: bool = False, stderr: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_substr = fail_substr
        self.fail_all = fail_all
        self.stderr = stderr

    def __call__(self, args, *, cwd=None, timeout=0, extra_env=None):  # noqa: ANN001, ARG002
        url = args[-2]
        token = (extra_env or {}).get(RepoWorkspace.PUSH_TOKEN_ENV, "")
        self.calls.append((url, token))
        if self.fail_all or (self.fail_substr and self.fail_substr in url):
            return _stub_result(ok=False, stderr=self.stderr)
        return _stub_result(ok=True)


def _stub_result(*, ok: bool, stderr: str = "") -> Any:
    return type("_R", (), {"ok": ok, "stdout": "", "stderr": stderr})()


DIRECT = "https://github.com/o/r.git"
MIRROR = "https://v6.gh-proxy.org/https://github.com/o/r.git"


def test_clone_tries_mirror_anonymously_then_direct(monkeypatch, tmp_path) -> None:
    """配了镜像就先走镜像，且**镜像那次绝不递令牌**、直连那次才递。

    两条边界都值得钉死：把令牌发给第三方镜像等于替别人做了一次凭据转发；
    反过来，直连那次不带令牌则私有仓库必然 ``Access denied``。
    """
    stub = _GitStub(fail_substr="gh-proxy", stderr="fatal: unable to access '…': Could not connect")
    monkeypatch.setattr(RepoWorkspace, "_git", stub)

    ws = RepoWorkspace.clone(
        DIRECT, mirror_url=MIRROR, token="SK-CLONE-TOKEN", dest=tmp_path / "repo"
    )

    assert [url for url, _ in stub.calls] == [MIRROR, DIRECT], "先镜像、后直连"
    assert stub.calls[0][1] == "", "镜像那次绝不能带令牌"
    assert stub.calls[1][1] == "SK-CLONE-TOKEN", "直连那次要带令牌（私有仓库靠它）"
    assert ws.cloned_from == DIRECT
    assert ws.origin_url == DIRECT


def test_clone_records_direct_url_even_when_mirror_won(monkeypatch, tmp_path) -> None:
    """镜像成功也只跑一次，且 `origin_url` 仍记**真站点**。

    记成镜像就等于让后续 `fetch` / `push` 去镜像上推——而镜像只读，推送必然失败，
    而且失败得毫无道理（`fix` 明明改好了）。
    """
    stub = _GitStub()
    monkeypatch.setattr(RepoWorkspace, "_git", stub)

    ws = RepoWorkspace.clone(DIRECT, mirror_url=MIRROR, token="SK-CLONE-TOKEN", dest=tmp_path / "repo")

    assert [url for url, _ in stub.calls] == [MIRROR], "镜像通了就不该再去打直连"
    assert ws.cloned_from == MIRROR
    assert ws.origin_url == DIRECT, "远端必须是真站点"


def test_clone_failure_names_both_paths_and_points_at_mirror_config(monkeypatch, tmp_path) -> None:
    """两条路都失败时报错要说清**两条**，并对「连不上站点」给出该配什么的指路。

    只报最后一条（直连超时）会让人以为是瞬时抖动而去重跑，重跑还是同一个超时。
    """
    stub = _GitStub(fail_all=True, stderr=(
        "fatal: unable to access 'https://github.com/o/r.git/': "
        "Connection timed out after 300036 milliseconds"
    ))
    monkeypatch.setattr(RepoWorkspace, "_git", stub)

    with pytest.raises(GitError) as err:
        RepoWorkspace.clone(DIRECT, mirror_url=MIRROR, token="tok", dest=tmp_path / "repo")

    message = str(err.value)
    assert "镜像与直连均失败" in message, message
    assert "clone_mirror" in message, "要指到具体配置项，否则人不知道改哪儿"


def test_clone_real_git_falls_back_to_direct_when_mirror_is_dead(tmp_path: Path) -> None:
    """真跑一次（不联网）：镜像地址不通 → 回退直连成功，`.git/config` 里是真站点。

    打桩只能验到「有没有按顺序调」，验不到「失败一次之后工作区还是干净的」——
    上一次尝试会留下半个目标目录，`git clone` 到已存在的非空目录上直接报错。
    """
    src = _repo_with_commit(tmp_path)
    # 用 `file://` 形态而不是裸路径：`harden_git` 写 `.git/config` 时不做转义，
    # 而 Windows 路径里的 `\U` 会被 git 当成非法转义序列（`bad config line`）。
    # 生产上远端恒为 https，碰不到这条；测试没必要顺带把它拽进来。
    origin = src.as_uri()
    mirror = tmp_path / "no-such-mirror"          # 本地路径，必然失败且不联网

    ws = RepoWorkspace.clone(origin, mirror_url=str(mirror), depth=0, dest=tmp_path / "clone")

    assert (ws.root / "a.txt").read_text(encoding="utf-8") == "hi\n"
    assert ws.cloned_from == origin, "最终应当是直连那条路取到的"
    remote = _run_git(["config", "--local", "--get", "remote.origin.url"], cwd=ws.root)
    assert remote.stdout.strip() == origin


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


def test_harden_git_drops_a_poisoned_identity_instead_of_writing_it_back(tmp_path: Path) -> None:
    """`.git/config` 对容器可写：身份值里带换行时**绝不能原样写回**重建后的配置。

    git 配置是**按行**解析的，而 `git config --get` 会把值里的字面换行原样输出（`.strip()`
    只去首尾）。于是往 `user.name` 里塞一段 `[core] fsmonitor = …`，就能在**加固后**的配置里
    凭空造出一个执行入口——而宿主随后正要 `git add` / `git commit`（push 的子进程还带着令牌）。
    """
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b", origin_url="https://example.invalid/o/r.git")

    _run_git(["config", "user.name", "x\n[core]\n\tfsmonitor = sh -c 'touch /tmp/pwned'"], cwd=root)
    _run_git(["config", "user.email", "bot@example.com"], cwd=root)      # 正常值应当保留

    ws.harden_git()

    text = (root / ".git" / "config").read_text(encoding="utf-8")
    assert "fsmonitor" not in text, "被污染的身份值不该出现在重建后的配置里"
    assert "pwned" not in text
    assert "bot@example.com" in text, "正常身份仍要带过来（别一刀切）"

    # 丢掉身份不影响提交：commit() 自己带 -c user.name / -c user.email
    (root / "f.txt").write_text("x", encoding="utf-8")
    _run_git(["add", "f.txt"], cwd=root)
    assert ws.commit("t").ok, "丢掉身份后仍要能提交"


def test_harden_git_drops_a_poisoned_origin_url(tmp_path: Path) -> None:
    """origin url 同样来自可写配置：带换行时整条不写，且不影响正常 url。"""
    root = _repo_with_commit(tmp_path)
    ws = RepoWorkspace(root=root, slug="a/b")            # 不传 origin_url → 回退读配置

    _run_git(
        [
            "config",
            "remote.origin.url",
            "https://example.invalid/o/r.git\n[core]\n\tfsmonitor = sh -c 'touch /tmp/pwned'",
        ],
        cwd=root,
    )
    ws.harden_git()
    text = (root / ".git" / "config").read_text(encoding="utf-8")
    assert "fsmonitor" not in text and "pwned" not in text
    assert '[remote "origin"]' not in text, "不可信的 url 应当整条不写，而不是写一半"

    # 换成正常 url 后应当照常写回（证明没有一刀切）
    _run_git(["config", "remote.origin.url", "https://example.invalid/good/repo.git"], cwd=root)
    ws.harden_git()
    text2 = (root / ".git" / "config").read_text(encoding="utf-8")
    assert "https://example.invalid/good/repo.git" in text2


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


# ---------------------------------------------------------------------------
# 克隆通路的失败面：超时、回退、残留清理、报错脱敏
# ---------------------------------------------------------------------------
#
# 这一组全部打在 `subprocess.run` 上，而不是打在 `_git` 上：`_git` 里那个
# `except TimeoutExpired → GitResult(ok=False)` 才是「不再空等 300 秒」的落点，
# 桩掉 `_git` 就正好把它绕过去了。假执行器一次性抛异常，测试里不 sleep 一秒。


class _SubprocessProbe:
    """假 `subprocess.run`：按被 clone 的 URL 决定「超时 / 报错 / 成功」。

    为什么要带上「超时」这一档：生产上炸的就是 `Connection timed out after 300036
    milliseconds`——真等一次要 300 秒，任何测试都不可能这么跑。用 `TimeoutExpired`
    一次性表达同一个事实，`_git` 的处理分支完全一样。
    """

    def __init__(
        self,
        *,
        timeout_urls: tuple[str, ...] = (),
        fail_urls: tuple[str, ...] = (),
        stderr: str = "",
        returncode: int = 128,
        materialize: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.timeout_urls = timeout_urls
        self.fail_urls = fail_urls
        self.stderr = stderr
        self.returncode = returncode
        # `materialize=True` 时先在目标位置建出目录：真 `git clone` 失败前往往已经
        # 写出了半个工作副本，残留正是要断言清理掉的东西。
        self.materialize = materialize

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        url = cmd[-2] if "clone" in cmd else ""
        if self.materialize and "clone" in cmd:
            target = Path(cmd[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "partial.txt").write_text("half\n", encoding="utf-8")
        if url in self.timeout_urls:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)
        if url in self.fail_urls:
            return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @property
    def urls(self) -> list[str]:
        """被真正尝试过的 URL 顺序（`git clone … <url> <target>` 里的倒数第二位）。"""
        return [cmd[-2] for cmd in self.calls]


def test_git_timeout_turns_into_a_failed_result_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超时必须被 `_git` 收成一个 `ok=False` 的结果，而不是把异常抛到整条流水线上。

    守住的事故：克隆那条 443 被墙时，子进程要干等到超时才返回；若这一层抛异常，
    上游只能看到 `TimeoutExpired`，报错里既没有「克隆失败」也没有该改哪儿。
    """

    import fissue.workspace as ws_mod

    def _boom(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)

    monkeypatch.setattr(ws_mod.subprocess, "run", _boom)

    result = RepoWorkspace(root=tmp_path / "repo")._git(["git", "clone", "https://example.invalid/x.git"])

    assert result.ok is False
    assert "超时" in result.stderr, result.stderr


def test_clone_timeout_reports_a_reason_instead_of_empty_hands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """克隆超时必须以 `GitError` 收场，且原因里带「超时」二字。

    守住的事故：超时曾经表现为一个空 stderr 的失败，条目还挂着「排队中」，看上去
    像还没开始而不是失败了；人只会「再等等」，实际这一步永远不会有结果。
    """
    import fissue.workspace as ws_mod

    probe = _SubprocessProbe(timeout_urls=(DIRECT,))
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)

    with pytest.raises(GitError) as err:
        RepoWorkspace.clone(DIRECT, token="tok", dest=tmp_path / "repo")

    message = str(err.value)
    assert "克隆失败" in message and "超时" in message, message


def test_mirror_timeout_falls_back_to_the_direct_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """镜像那次超时不能就此放弃——继续走直连那次，并只记一条「镜像 + 直连」的尝试序列。

    守住的事故：镜像本身也会挂（代理到期、镜像站点被限流）。把它当终点就等于
    「配了镜像反而更糟」：没配镜像时直连还能通，配了之后一次都不试直连。
    """
    import fissue.workspace as ws_mod

    probe = _SubprocessProbe(timeout_urls=(MIRROR,))
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)

    ws = RepoWorkspace.clone(DIRECT, mirror_url=MIRROR, token="tok", dest=tmp_path / "repo")

    assert probe.urls == [MIRROR, DIRECT], "镜像超时后必须再试直连"
    assert ws.cloned_from == DIRECT, "最终取到代码的是直连那次"


def test_clone_failure_removes_the_half_written_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """克隆失败要把已经写了一半的目标目录删掉（否则下一次尝试直接撞上「目录非空」）。

    守住的事故：镜像 → 直连的两条路共用一个 `dest`；第一次留下的半个工作副本会让
    第二次 `git clone` 报 `destination path already exists and is not an empty
    directory`，于是「回退到直连」这条兜底永远走不通。
    """
    import fissue.workspace as ws_mod

    target = tmp_path / "repo"
    probe = _SubprocessProbe(fail_urls=(DIRECT,), stderr="fatal: early EOF", materialize=True)
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)

    with pytest.raises(GitError):
        RepoWorkspace.clone(DIRECT, token="tok", dest=target)

    assert not target.exists(), "失败留下的半个工作副本必须被清掉"


def test_clone_failure_removes_the_outer_temp_dir_it_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不指定 `dest` 时，失败要连外层临时目录一起删（一次失败漏一个，失败现在有两次）。

    守住的事故：`cleanup()` 只删 `<mkdtemp>/repo`，外层 `fissue-repo-*` 会留下来。
    镜像 + 直连各失败一次就是每次漏两个目录，长跑的服务上磁盘只涨不落。
    """
    import fissue.workspace as ws_mod

    outer = tmp_path / "fissue-repo-abc"

    def _mkdtemp(prefix: str = "") -> str:  # noqa: ARG001
        return str(outer)

    probe = _SubprocessProbe(
        fail_urls=(MIRROR, DIRECT), stderr="fatal: early EOF", materialize=True
    )
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)
    monkeypatch.setattr(ws_mod.tempfile, "mkdtemp", _mkdtemp)

    with pytest.raises(GitError):
        RepoWorkspace.clone(DIRECT, mirror_url=MIRROR, token="tok")

    assert not outer.exists(), "失败后外层临时目录也必须删掉"


def test_clone_failure_redacts_the_token_from_the_reported_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """克隆失败的报错里不能出现令牌原文——它会直接进日志、进 WebUI、进人工报告。

    守住的事故：私有仓库克隆失败时 git 会把整个 URL（含内联凭据）或服务端的
    鉴权回显写进 stderr，而这条 stderr 现在会被 `str(exc)` 原样落档。
    """
    import fissue.workspace as ws_mod

    token = "SK-CLONE-SECRET-123"
    probe = _SubprocessProbe(
        fail_urls=(DIRECT,),
        stderr=f"fatal: unable to access 'https://github.com/o/r.git/': 401 {token}",
    )
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)

    with pytest.raises(GitError) as err:
        RepoWorkspace.clone(DIRECT, token=token, dest=tmp_path / "repo")

    assert token not in str(err.value), "令牌原文漏进了报错"
    assert "***" in str(err.value), "令牌应当被替换成 ***"


def test_clone_failure_keeps_a_non_network_error_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """仓库不存在 / 无权限这类错误要原样带出，且**不要**甩一句「去配 clone_mirror」。

    守住的事故：给所有克隆失败都补「连不上站点」的指路，会让一个仓库名写错的
    人在镜像配置上绕半天；指路只在真连不上时才该出现。
    """
    import fissue.workspace as ws_mod

    probe = _SubprocessProbe(
        fail_urls=(DIRECT,),
        stderr="fatal: repository 'https://github.com/o/r.git/' not found",
    )
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)

    with pytest.raises(GitError) as err:
        RepoWorkspace.clone(DIRECT, token="tok", dest=tmp_path / "repo")

    message = str(err.value)
    assert "not found" in message, message
    assert "clone_mirror" not in message, "非网络错误不该被指去配镜像"


def test_clone_skips_the_mirror_when_it_points_at_the_same_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """镜像地址与直连地址相同时只试一次——否则同一个超时会被白等两遍。

    守住的事故：有人把 `clone_mirror` 填成原站点（或填了个会被装配成原样的值），
    于是「镜像 + 直连」等于对同一个不通的地址连试两次，耗时翻倍。
    """
    import fissue.workspace as ws_mod

    probe = _SubprocessProbe()
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)

    ws = RepoWorkspace.clone(DIRECT, mirror_url=DIRECT, token="tok", dest=tmp_path / "repo")

    assert probe.urls == [DIRECT], "镜像与直连是同一个地址时只该试一次"
    assert ws.cloned_from == DIRECT


def test_clone_omits_the_shallow_flag_when_depth_is_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`depth=0` 表示要完整历史：命令行上不能出现 `--depth`（否则 git 直接报参数错）。

    守住的事故：要跑 `git log` / 追溯提交的场合必须拿全量历史；拼参数时把
    `--depth 0` 发出去，git 会以 `depth 0 is not a positive number` 拒绝克隆。
    """
    import fissue.workspace as ws_mod

    probe = _SubprocessProbe()
    monkeypatch.setattr(ws_mod.subprocess, "run", probe)

    RepoWorkspace.clone(DIRECT, depth=0, dest=tmp_path / "repo")

    assert "--depth" not in probe.calls[0], probe.calls[0]


# ---------------------------------------------------------------------------
# 清理路径：异常退出时也要把工作区收掉
# ---------------------------------------------------------------------------


def test_context_manager_cleans_up_even_when_the_body_raises(tmp_path: Path) -> None:
    """`with` 块里抛异常时工作区照样要删掉——否则每条失败条目都在磁盘上留一份仓库。

    守住的事故：验证器/修复器在 `with RepoWorkspace(...)` 里跑，LLM 报错、断言失败
    都会从中间炸出去；若清理只写在正常返回那一路，失败条目就会持续堆积整份仓库
    （还带着 `.git` 里的令牌可用的远端配置）。
    """
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"), RepoWorkspace(root=root, slug="o/r"):
        raise RuntimeError("boom")

    assert not root.exists(), "异常退出后工作区必须被删掉"


def test_cleanup_tolerates_a_workspace_that_is_already_gone(tmp_path: Path) -> None:
    """重复清理 / 目录已不存在时 `cleanup` 不能抛——它常在 `finally` 里被调用。

    守住的事故：清理放在异常处理路径上，自己再抛 `FileNotFoundError` 就会把真正的
    失败原因盖掉，日志上只剩「删不掉目录」，查不到最初为什么失败。
    """
    root = tmp_path / "gone"
    ws = RepoWorkspace(root=root, slug="o/r")

    ws.cleanup()
    ws.cleanup()

    assert not root.exists()
