"""PRCreator 的「提交并推送」路径测试：守住「只提交 Agent 改过的文件」这道护栏。

这段代码在真跑里起过作用：验证器（含它生成的测试文件）会在本地执行时写进工作区，
若用 ``git add -A`` 暂存，它们会被一起提交并推送进 PR——等于把「判定用的标尺」公开到
上游仓库。README 里记的「两处的改动都只有源码文件、**未夹带验证器测试文件**」就是这条
护栏的产出。

但这些路径此前几乎没有单测守着，所以这里逐条钉住：

* 只暂存 Agent 改过的文件（``stage_paths`` 用白名单，不是 ``-A``）；
* 索引被塞进别的东西时**拒绝提交**（``extras`` 检查）；
* 分支不对、提交失败、推送失败时**如实收场**，不留「看起来成功」的痕迹。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from fissue.fixer.agent import AgentTrace
from fissue.fixer.autofix import PRCreator
from fissue.models import FixOutcome, Platform, VerifierKind, VerifierSpec
from fissue.workspace import GitResult, RepoWorkspace

# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _PushWS:
    """`_commit_and_push` 用到的工作区替身：每一步都可单独摆布。"""

    def __init__(
        self,
        *,
        checkout_ok: bool = True,
        off_branch: str | None = None,
        staged: bool = True,
        staged_files: list[str] | None = None,
        commit_ok: bool = True,
        commit_stderr: str = "",
        push_ok: bool = True,
        push_stderr: str = "",
    ) -> None:
        self.checkout_ok = checkout_ok
        self.off_branch = off_branch
        self.staged = staged
        # 属性名不能叫 staged_files——那会遮蔽同名方法（实例属性优先于类属性）。
        self.will_commit = staged_files if staged_files is not None else ["app.py"]
        self.commit_ok = commit_ok
        self.commit_stderr = commit_stderr
        self.push_ok = push_ok
        self.push_stderr = push_stderr
        self.calls: list[str] = []
        self.staged_paths: list[str] = []
        self.commit_message = ""
        self.pushed: tuple[str, str, bool, str] | None = None

    def checkout(self, ref: str, *, create: bool = False) -> bool:
        self.calls.append("checkout")
        return self.checkout_ok

    def reset_index(self) -> bool:
        self.calls.append("reset_index")
        return True

    def assert_on_branch(self, branch: str) -> str | None:
        self.calls.append("assert_on_branch")
        return self.off_branch

    def stage_paths(self, paths: list[str]) -> bool:
        self.calls.append("stage_paths")
        self.staged_paths = list(paths)
        return self.staged

    def staged_files(self) -> list[str]:
        self.calls.append("staged_files")
        return list(self.will_commit)

    def commit(self, message: str, *, author: str = "") -> GitResult:
        self.calls.append("commit")
        self.commit_message = message
        return GitResult(ok=self.commit_ok, returncode=0 if self.commit_ok else 1,
                         stdout="", stderr=self.commit_stderr)

    def push(self, url: str, branch: str, *, force: bool = False, token: str = "") -> GitResult:
        self.calls.append("push")
        self.pushed = (url, branch, force, token)
        return GitResult(ok=self.push_ok, returncode=0 if self.push_ok else 1,
                         stdout="", stderr=self.push_stderr)


@pytest.fixture
def creator_ctx(fixer_ctx):
    """PRCreator 用的上下文：不需要真平台，适配器用替身即可。"""
    fixer_ctx.settings.auto_fix.pr_strategy.mode = "direct"
    return fixer_ctx


def _repo_cfg():
    from fissue.config import RepoConfig

    return RepoConfig(platform=Platform.GITHUB, owner="psf", name="requests")


def _trace(*written: str) -> AgentTrace:
    return AgentTrace(rounds=1, written_files=list(written), summary="改了 app.py")


async def _commit_and_push(
    creator: PRCreator,
    ws: _PushWS,
    trace: AgentTrace,
    item: Any,
    branch: str = "fissue/fix-1",
):
    return await creator._commit_and_push(
        cast(RepoWorkspace, ws),
        item=item,
        branch=branch,
        head_repo=_repo_cfg(),
        trace=trace,
    )


# ---------------------------------------------------------------------------
# 护栏：只提交 Agent 改过的文件
# ---------------------------------------------------------------------------


async def test_commit_stages_only_agent_modified_files(creator_ctx, sample_issue) -> None:
    """只暂存 Agent 改过的文件——**不能**用 ``git add -A``。

    验证器（含它生成的测试文件）在本地执行时会写进工作区；``-A`` 会把它们一起提交并
    推送进 PR，等于把「判定用的标尺」公开到上游仓库。dry-run 看不到这个问题：补丁走
    ``git diff``，而 git diff 不含未跟踪文件。
    """
    creator = PRCreator(creator_ctx)
    ws = _PushWS(staged_files=["app.py"])

    ok, err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert (ok, err) == (True, "")
    assert ws.staged_paths == ["app.py"], "只能暂存 Agent 改过的文件"
    assert "stage_paths" in ws.calls


async def test_commit_refuses_when_index_has_extra_files(creator_ctx, sample_issue) -> None:
    """待提交内容里出现 Agent 没改过的文件 → **拒绝提交**。

    这是最后一道：多出来的内容说明索引或工作区被别的东西动过（``.git`` 在沙盒里可写）。
    守的是「护栏被悄悄绕过」这件事，不只是上面那条已知路径。
    """
    creator = PRCreator(creator_ctx)
    ws = _PushWS(staged_files=["app.py", "tests/test_reproduce.py"])

    ok, err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ok is False
    assert "未改过的文件" in err and "tests/test_reproduce.py" in err
    assert "commit" not in ws.calls, "拒绝后不该提交"
    assert "push" not in ws.calls, "拒绝后不该推送"


async def test_commit_resets_index_before_staging(creator_ctx, sample_issue) -> None:
    """提交前先把索引拉回 HEAD——否则沙盒里塞进索引的内容会绕过白名单一起被推走。

    ``git commit`` 提交的是**索引**、不是工作区，所以必须先 ``read-tree --reset``。
    """
    creator = PRCreator(creator_ctx)
    ws = _PushWS(staged_files=["app.py"])

    await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ws.calls.index("reset_index") < ws.calls.index("stage_paths"), (
        "必须先 reset_index 再 stage_paths"
    )


# ---------------------------------------------------------------------------
# 收场：每一步失败都要如实报告
# ---------------------------------------------------------------------------


async def test_branch_creation_failure_stops_before_staging(creator_ctx, sample_issue) -> None:
    """建分支失败 → 立刻收场，不提交也不推送。"""
    creator = PRCreator(creator_ctx)
    ws = _PushWS(checkout_ok=False)

    ok, err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ok is False and "创建分支" in err
    assert "stage_paths" not in ws.calls and "commit" not in ws.calls


async def test_wrong_branch_refuses_to_commit(creator_ctx, sample_issue) -> None:
    """``.git/HEAD`` 被改后不在预期分支上 → 拒绝提交。

    否则 ``git commit`` / ``git push`` 会作用到**另一个分支**上，而调用方以为推的是
    自己的修复分支。
    """
    creator = PRCreator(creator_ctx)
    ws = _PushWS(off_branch="当前分支是 main、不是预期分支 fissue/fix-1，拒绝提交")

    ok, err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ok is False and "拒绝提交" in err
    assert "commit" not in ws.calls and "push" not in ws.calls


async def test_no_changes_is_reported_not_pushed(creator_ctx, sample_issue) -> None:
    """没有可提交的改动 → 如实说「没有可提交的改动」，而不是推一个空提交。"""
    creator = PRCreator(creator_ctx)
    ws = _PushWS(staged=False)

    ok, err = await _commit_and_push(creator, ws, _trace(), sample_issue)

    assert ok is False and "没有可提交的改动" in err
    assert "commit" not in ws.calls and "push" not in ws.calls


async def test_empty_commit_is_reported_clearly(creator_ctx, sample_issue) -> None:
    """``git commit`` 说「nothing to commit」→ 报成「没有产生任何改动」，不是原始英文。"""
    creator = PRCreator(creator_ctx)
    ws = _PushWS(commit_ok=False, commit_stderr="nothing to commit, working tree clean")

    ok, err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ok is False and "没有产生任何改动" in err


async def test_commit_failure_reports_stderr(creator_ctx, sample_issue) -> None:
    """提交失败要带上 stderr 片段，否则只剩一句「提交失败」。"""
    creator = PRCreator(creator_ctx)
    ws = _PushWS(commit_ok=False, commit_stderr="fatal: refusing to commit")

    ok, err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ok is False and "refusing to commit" in err
    assert "push" not in ws.calls


async def test_push_failure_reports_stderr(creator_ctx, sample_issue) -> None:
    """推送失败要带上 stderr——远端拒绝的理由（stale info / 权限）全在这里。"""
    creator = PRCreator(creator_ctx)
    ws = _PushWS(push_ok=False, push_stderr="! [rejected] stale info")

    ok, err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ok is False and "stale info" in err


async def test_push_url_carries_no_token(creator_ctx, sample_issue) -> None:
    """推送 URL **不带令牌**：令牌改由 ``workspace.push`` 经环境变量 + 凭据助手递进去。

    命令行参数对同机所有用户可见（``ps``、``/proc/<pid>/cmdline``），环境变量只有本用户
    可读。URL 里内嵌令牌就等于把令牌写进了全局可见的地方。
    """
    creator = PRCreator(creator_ctx)
    ws = _PushWS(staged_files=["app.py"])

    ok, _err = await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ok is True
    assert ws.pushed is not None
    url, branch, force, token = ws.pushed
    assert "@" not in url.split("://", 1)[1], f"URL 里不该有凭据：{url}"
    assert "token=" not in url
    assert branch == "fissue/fix-1"
    assert force is True


async def test_push_token_is_passed_separately(creator_ctx, sample_issue) -> None:
    """令牌要**单独**传给 ``push``（而不是塞进 URL）。"""
    creator_ctx.settings.tokens.github = "ghp_secret_token_value"
    creator_ctx._adapters.clear()          # 适配器带 token 构造，缓存要清掉
    creator = PRCreator(creator_ctx)
    ws = _PushWS(staged_files=["app.py"])

    await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert ws.pushed is not None
    assert ws.pushed[3] == "ghp_secret_token_value", "令牌要作为独立参数传给 push"


async def test_commit_message_marks_ai_generation(creator_ctx, sample_issue) -> None:
    """提交信息要写明「由 Fissue AI 自动修复生成」，并列出改了哪些文件。"""
    creator = PRCreator(creator_ctx)
    ws = _PushWS(staged_files=["app.py"])

    await _commit_and_push(creator, ws, _trace("app.py"), sample_issue)

    assert "Fissue AI" in ws.commit_message
    assert "app.py" in ws.commit_message


# ---------------------------------------------------------------------------
# 提 PR 阶段
# ---------------------------------------------------------------------------


async def _submit(
    creator: PRCreator,
    *,
    item: Any,
    create_pr=None,
    add_labels=None,
    ensure_labels=None,
    push_ok: bool = True,
):
    """跑一遍 ``submit``（推送与文案已被替身接管），返回 attempt。"""
    from fissue.models import FixAttempt

    adapter = await creator.ctx.adapter_for_repo(_repo_cfg())

    async def _fake_push(*args, **kwargs):
        return (push_ok, "" if push_ok else "推送失败")

    async def _fake_text(item, *, trace, spec, f2p_ok, verification_note=""):
        return "fix: 标题", "正文"

    async def _no_open_pr(ref, *, head):
        return None

    async def _create(ref, **kwargs):
        if create_pr is not None:
            return await create_pr(ref, **kwargs)
        return {"number": 7, "url": "https://github.com/psf/requests/pull/7"}

    async def _ensure(ref, labels, **kwargs):
        if ensure_labels is not None:
            return await ensure_labels(ref, labels)
        return None

    async def _add(ref, number, labels, item_type=None):
        if add_labels is not None:
            return await add_labels(ref, number, labels, item_type)
        return None

    creator._commit_and_push = _fake_push          # type: ignore[assignment]
    creator._compose_pr_text = _fake_text          # type: ignore[assignment]
    adapter.find_open_pr = _no_open_pr             # type: ignore[assignment]
    adapter.create_pr = _create                    # type: ignore[assignment]
    adapter.ensure_labels = _ensure                # type: ignore[assignment]
    adapter.add_labels = _add                      # type: ignore[assignment]

    return await creator.submit(
        attempt=FixAttempt(item_key=item.key),
        trace=_trace("app.py"),
        item=item,
        workspace=cast(RepoWorkspace, _PushWS()),
        repo_cfg=_repo_cfg(),
        spec=VerifierSpec(kind=VerifierKind.EXECUTABLE, command="python -m pytest -q"),
        f2p_ok=True,
        diff="diff --git a/app.py b/app.py\n",
    )


async def test_pr_creation_failure_reports_branch_already_pushed(creator_ctx, sample_issue) -> None:
    """建 PR 失败要说明「分支已推送」——否则重跑会以为什么都没发生。"""
    creator = PRCreator(creator_ctx)

    async def _boom(ref, **kwargs):
        raise RuntimeError("403 no permission")

    attempt = await _submit(creator, item=sample_issue, create_pr=_boom)

    assert attempt.outcome is FixOutcome.NEEDS_MANUAL
    assert "分支已推送" in (attempt.error or "")
    assert "403" in (attempt.error or "")
    assert attempt.branch, "分支名要留下，便于人工接着做"


async def test_successful_pr_records_number_and_url(creator_ctx, sample_issue) -> None:
    """成功路径要记下 PR 号与链接。"""
    creator = PRCreator(creator_ctx)

    attempt = await _submit(creator, item=sample_issue)

    assert attempt.outcome is FixOutcome.SUCCESS
    assert attempt.pr_number == 7
    assert attempt.pr_url == "https://github.com/psf/requests/pull/7"


async def test_pr_gets_ai_labels(creator_ctx, sample_issue) -> None:
    """PR 要打上 AI 标签——标识这是 AI 产物。"""
    creator = PRCreator(creator_ctx)
    labelled: list[tuple[Any, ...]] = []

    async def _add(ref, number, labels, item_type=None):
        labelled.append((number, list(labels)))

    attempt = await _submit(creator, item=sample_issue, add_labels=_add)

    assert attempt.outcome is FixOutcome.SUCCESS
    assert labelled, "该给 PR 打 AI 标签"
    number, labels = labelled[0]
    assert number == 7
    assert "ai-generated" in labels


async def test_label_failure_does_not_break_the_pr(creator_ctx, sample_issue) -> None:
    """打标签失败**不影响**主流程——PR 已经建好了，不能因此报成失败。"""
    creator = PRCreator(creator_ctx)

    async def _boom(ref, number, labels, item_type=None):
        raise RuntimeError("标签接口 500")

    attempt = await _submit(creator, item=sample_issue, add_labels=_boom)

    assert attempt.outcome is FixOutcome.SUCCESS, "标签失败不该把已建好的 PR 报成失败"
    assert attempt.pr_url


async def test_no_pr_number_means_no_labels_attempted(creator_ctx, sample_issue) -> None:
    """平台没回 PR 号时不该去打标签（会打到编号 0 上）。"""
    creator = PRCreator(creator_ctx)
    labelled: list[Any] = []

    async def _create(ref, **kwargs):
        return {"url": "https://github.com/psf/requests/pull/9"}

    async def _add(ref, number, labels, item_type=None):
        labelled.append(number)

    await _submit(creator, item=sample_issue, create_pr=_create, add_labels=_add)

    assert labelled == [], "没有 PR 号就不该打标签"


async def test_patch_only_mode_produces_patch_and_no_pr(creator_ctx, sample_issue) -> None:
    """``mode=patch_only``：只出补丁、不提 PR——而且不该去推分支。"""
    creator_ctx.settings.auto_fix.pr_strategy.mode = "patch_only"
    creator = PRCreator(creator_ctx)

    attempt = await _submit(creator, item=sample_issue)

    assert attempt.outcome is FixOutcome.NEEDS_MANUAL
    assert attempt.patch_path, "补丁要留给人工"
    assert attempt.pr_url is None
    assert "patch_only" in (attempt.error or "")
