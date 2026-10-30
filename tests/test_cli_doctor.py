"""``fissue platforms doctor`` 的单元测试。

这个命令的价值全在「说真话」上：探测结果必须能区分**通过 / 失败 / 没探测**，
而且写探测**必须真的把痕迹擦掉**。所以这里的断言不看退出码，而是看：

* 三态显示没被压平成两态（把「跳过」显示成 ✅ 等于把没验过的说成验过了）；
* 写探测的**调用顺序**——仓库标签必须最后删（Gitee 给条目打标签时会自动建同名
  仓库标签，先删后建等于白删，实测留下过一个残留标签）；
* 残留时**必须报出来**，不能因为删除调用没抛异常就宣称干净。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import fissue.cli  # noqa: F401  —— 触发命令注册
from fissue.cli.doctor import (
    PROBE_BRANCH,
    PROBE_LABEL,
    _display,
    _probe_clone,
    _probe_push,
    _probe_read,
    _probe_write,
    _resolve_targets,
    _summary,
)
from fissue.cli.main import app
from fissue.config import Settings
from fissue.errors import ConfigError
from fissue.models import Platform, RepoRef

REF = RepoRef(platform=Platform.GITHUB, owner="psf", name="requests")


class _FakeAdapter:
    """只记账、不联网的适配器替身。

    ``calls`` 记录的是**调用顺序**——顺序正是写探测最容易出错的地方。
    """

    def __init__(
        self,
        *,
        fail: dict[str, Exception] | None = None,
        label_survives: bool = False,
        label_list_fails: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.fail = fail or {}
        # **仓库标签与条目标签必须是两个集合**：混在一起时，「条目上移除标签」会把
        # 仓库标签也顺手删掉，于是「残留」这个场景永远模拟不出来——第一版替身就是这样
        # 让「删不掉要报出来」这条用例假通过的。
        self.repo_labels: set[str] = set()
        self.item_labels: set[str] = set()
        self.label_survives = label_survives
        self.label_list_fails = label_list_fails

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def clone_url(self, _repo: RepoRef, *, use_token: bool = False) -> str:  # noqa: ARG002
        # 真实实现返回**不带令牌**的 URL（令牌另由 clone 的 token 参数传），
        # 替身照这个形状给，免得把「URL 里塞令牌」这种更糟的写法固化进测试。
        self.calls.append("clone_url")
        return "https://example.invalid/psf/requests.git"

    async def whoami(self) -> str:
        self._record("whoami")
        return "tester"

    async def get_default_branch(self, _repo: RepoRef) -> str:
        self._record("get_default_branch")
        return "main"

    async def fetch_items(self, _repo: RepoRef, limit: int = 1) -> Any:
        self._record("fetch_items")
        for _ in range(limit):
            yield object()

    async def fetch_repo_labels(self, _repo: RepoRef) -> list[str]:
        if self.label_list_fails:
            self.calls.append("fetch_repo_labels")
            raise RuntimeError("查不到标签列表")
        return sorted(self.repo_labels)

    async def ensure_label(self, _repo: RepoRef, label: str, **_kw: Any) -> None:
        self._record("ensure_label")
        self.repo_labels.add(label)

    async def delete_repo_label(self, _repo: RepoRef, label: str) -> None:
        self._record("delete_repo_label")
        if not self.label_survives:
            self.repo_labels.discard(label)

    async def add_labels(self, _repo: RepoRef, _n: Any, labels: list[str]) -> None:
        self._record("add_labels")
        self.item_labels.update(labels)

    async def remove_label(self, _repo: RepoRef, _n: Any, label: str) -> None:
        self._record("remove_label")
        self.item_labels.discard(label)

    async def comment(self, _repo: RepoRef, _n: Any, _body: str) -> None:
        self._record("comment")

    async def delete_comment_by_body(self, _repo: RepoRef, _n: Any, _body: str) -> bool:
        self._record("delete_comment_by_body")
        return True


# ---------------------------------------------------------------------------
# 三态显示
# ---------------------------------------------------------------------------


def test_display_keeps_three_states_distinct() -> None:
    assert _display(True) == "✅"
    assert _display(False) == "❌"
    # None = 「没探测」，**不能**显示成通过
    assert _display(None) == "－"
    assert len({_display(True), _display(False), _display(None)}) == 3


def test_summary_counts_skips_separately_from_failures() -> None:
    rows = [
        {"repo": "r", "probe": "a", "result": "✅", "detail": ""},
        {"repo": "r", "probe": "b", "result": "❌", "detail": ""},
        {"repo": "r", "probe": "c", "result": "－", "detail": ""},
    ]
    assert _summary(rows) == (1, 1)


# ---------------------------------------------------------------------------
# 只读探测
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_probes_all_green() -> None:
    rows = await _probe_read(_FakeAdapter(), REF)
    assert [r["probe"] for r in rows] == [
        "只读/whoami",
        "只读/默认分支",
        "只读/条目",
        "只读/仓库标签",
    ]
    assert all(r["result"] == "✅" for r in rows)


@pytest.mark.asyncio
async def test_read_probe_failure_does_not_abort_the_rest() -> None:
    """一项挂了其余照测：体检报告的价值在**一张完整的矩阵**，不是第一个错就停下。"""
    adapter = _FakeAdapter(fail={"get_default_branch": RuntimeError("boom")})
    rows = await _probe_read(adapter, REF)
    by_probe = {r["probe"]: r for r in rows}
    assert by_probe["只读/默认分支"]["result"] == "❌"
    assert "boom" in by_probe["只读/默认分支"]["detail"]
    assert by_probe["只读/条目"]["result"] == "✅"
    assert len(rows) == 4


# ---------------------------------------------------------------------------
# 写探测：顺序、跳过、残留
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_probes_are_skipped_without_number() -> None:
    """没给 --number 时两条条目级探测必须显示「－」，而不是 ✅。"""
    adapter = _FakeAdapter()
    rows = await _probe_write(adapter, REF, None)
    by_probe = {r["probe"]: r for r in rows}
    assert by_probe["写/条目打标签"]["result"] == "－"
    assert by_probe["写/条目评论"]["result"] == "－"
    assert "add_labels" not in adapter.calls
    assert "comment" not in adapter.calls


@pytest.mark.asyncio
async def test_write_probe_deletes_repo_label_after_item_probes() -> None:
    """仓库标签必须**最后**删。

    这是实测踩出来的：Gitee 在 `add_labels` 时会自动创建同名仓库标签，
    若先删仓库标签、后给条目打标签，标签会被重新建出来——第一次真机跑就留下了一个，
    而命令当时还报「建 + 删均通过」。
    """
    adapter = _FakeAdapter()
    rows = await _probe_write(adapter, REF, "1")

    assert all(r["result"] == "✅" for r in rows), rows
    idx_delete = adapter.calls.index("delete_repo_label")
    assert idx_delete > adapter.calls.index("add_labels"), adapter.calls
    assert idx_delete > adapter.calls.index("comment"), adapter.calls


@pytest.mark.asyncio
async def test_write_probe_reports_residue_instead_of_claiming_clean() -> None:
    """删不掉时必须说出来——「调用没报错」不等于「痕迹没了」。"""
    adapter = _FakeAdapter(label_survives=True)
    rows = await _probe_write(adapter, REF, "1")
    repo_label_row = next(r for r in rows if r["probe"] == "写/仓库标签")
    assert "清不掉" in repo_label_row["detail"]
    assert PROBE_LABEL in repo_label_row["detail"]


@pytest.mark.asyncio
async def test_write_probe_treats_unknowable_state_as_residue() -> None:
    """连「标签还在不在」都查不清时，按**可能残留**报——谎报干净比报残留更糟。"""
    adapter = _FakeAdapter(label_list_fails=True)
    rows = await _probe_write(adapter, REF, "1")
    repo_label_row = next(r for r in rows if r["probe"] == "写/仓库标签")
    assert "清不掉" in repo_label_row["detail"]


@pytest.mark.asyncio
async def test_write_probe_failure_is_reported_not_raised() -> None:
    adapter = _FakeAdapter(fail={"add_labels": RuntimeError("403 无权限")})
    rows = await _probe_write(adapter, REF, "1")
    row = next(r for r in rows if r["probe"] == "写/条目打标签")
    assert row["result"] == "❌"
    assert "403" in row["detail"]


# ---------------------------------------------------------------------------
# 目标解析
# ---------------------------------------------------------------------------


def test_resolve_targets_defaults_to_configured_repos(settings: Settings) -> None:
    targets = _resolve_targets(settings, [])
    assert [(t.platform, t.owner, t.name) for t in targets] == [
        (Platform.GITHUB, "psf", "requests")
    ]


def test_resolve_targets_accepts_explicit_platform_prefix(settings: Settings) -> None:
    targets = _resolve_targets(settings, ["gitee:ccqtp/fissue-write-probe"])
    assert targets[0].platform is Platform.GITEE
    assert targets[0].name == "fissue-write-probe"


def test_resolve_targets_matches_configured_repo_without_prefix(settings: Settings) -> None:
    """不写平台前缀时去配置里找——常见用法是「体检我现在配的那几个」，不该逼人抄前缀。"""
    targets = _resolve_targets(settings, ["psf/requests"])
    assert targets[0].platform is Platform.GITHUB


def test_resolve_targets_rejects_unknown_repo(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="platform:owner/name"):
        _resolve_targets(settings, ["nobody/unknown"])


# ---------------------------------------------------------------------------
# CLI 装配
# ---------------------------------------------------------------------------


def test_doctor_is_registered_under_platforms() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["platforms", "doctor", "--help"])
    assert result.exit_code == 0, result.output
    assert "--write" in result.output
    assert "--number" in result.output


# ---------------------------------------------------------------------------
# 克隆与推分支探测
# ---------------------------------------------------------------------------


class _FakeWorkspace:
    """克隆/推送探测用的工作副本替身（不碰 git、不联网）。"""

    instances: list[_FakeWorkspace] = []

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.root = Path("/tmp/fissue-repo-fake/repo")
        self.cleaned = False
        _FakeWorkspace.instances.append(self)

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch, *, fail_tokens: set[str] | None = None) -> None:
        """把 ``RepoWorkspace.clone`` 换成本替身。

        ``fail_tokens`` 里的令牌值会触发克隆失败——用来模拟「私有仓库 / 权限不对」，
        并且能按令牌区分成功与失败，这正是「匿名重试」要判断的东西。
        """
        bad = fail_tokens or set()
        cls.instances = []

        def fake_clone(_url: str, *, slug: str = "", token: str = "", depth: int = 1):  # noqa: ARG001
            if token in bad:
                raise RuntimeError("remote: The token is not authorized to access this project")
            return cls()

        from fissue.workspace import RepoWorkspace

        monkeypatch.setattr(RepoWorkspace, "clone", classmethod(lambda c, *a, **k: fake_clone(*a, **k)))

    # -- 被探测代码用到的方法 -------------------------------------------
    def current_branch(self) -> str:
        return "main"

    def cleanup(self) -> None:
        self.cleaned = True

    def checkout(self, ref: str, *, create: bool = False) -> bool:  # noqa: ARG002
        self.calls.append("checkout")
        return True

    def write(self, rel: str, content: str) -> Path:  # noqa: ARG002
        self.calls.append("write")
        return self.root / rel

    def stage_all(self) -> bool:
        self.calls.append("stage_all")
        return True

    def commit(self, message: str, **_kw: Any) -> Any:  # noqa: ARG002
        self.calls.append("commit")
        return _result(ok=True)

    def push(self, url: str, branch: str, **_kw: Any) -> Any:  # noqa: ARG002
        self.calls.append("push")
        return _result(ok=True)

    def delete_remote_branch(self, url: str, branch: str, **_kw: Any) -> Any:  # noqa: ARG002
        self.calls.append("delete_remote_branch")
        return _result(ok=True)

    def sanitize(self, text: str) -> str:
        return text


def _result(*, ok: bool, stderr: str = "") -> Any:
    from fissue.workspace import GitResult

    return GitResult(ok=ok, returncode=0 if ok else 1, stdout="", stderr=stderr)


@pytest.mark.asyncio
async def test_probe_clone_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeWorkspace.install(monkeypatch)
    row = await _probe_clone(_FakeAdapter(), REF, "tok")
    assert row["result"] == "✅"
    assert "克隆成功" in row["detail"]
    assert _FakeWorkspace.instances[0].cleaned is True, "探测完必须清理工作副本"


@pytest.mark.asyncio
async def test_probe_clone_blames_credentials_when_anonymous_works(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """带令牌被拒、匿名反而成功 → 锅在**我们传凭据**这一步，不是令牌权限。

    这两种原因的处理方式完全相反（一个去查代码、一个去平台开权限），
    所以命令必须自己分辨，而不是只丢一句「克隆失败」。
    """
    _FakeWorkspace.install(monkeypatch, fail_tokens={"bad-token"})
    row = await _probe_clone(_FakeAdapter(), REF, "bad-token")
    assert row["result"] == "❌"
    assert "问题在凭据传递" in row["detail"]


@pytest.mark.asyncio
async def test_probe_clone_blames_token_scope_when_anonymous_also_fails(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """带令牌与匿名都失败 → 私有仓库，令牌缺项目权限（平台侧配置问题）。"""
    fake = _FakeWorkspace()
    fake.current_branch = lambda: "main"  # type: ignore[method-assign]

    from fissue.workspace import RepoWorkspace

    def always_fail(_cls, _url, **_kw):  # noqa: ANN001, ARG001
        raise RuntimeError("remote: The token is not authorized to access this project")

    monkeypatch.setattr(RepoWorkspace, "clone", classmethod(always_fail))
    row = await _probe_clone(_FakeAdapter(), REF, "tok")
    assert row["result"] == "❌"
    assert "projects" in row["detail"]


@pytest.mark.asyncio
async def test_probe_push_deletes_the_probe_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """推分支探测必须自清：推成功之后一定删远端分支。

    留一条 ``fissue-doctor-probe`` 在别人仓库里，比不探测更糟——探测的价值是
    「不留痕迹地知道能做什么」。
    """
    _FakeWorkspace.install(monkeypatch)
    row = await _probe_push(_FakeAdapter(), REF, "tok")

    assert row["result"] == "✅", row
    calls = _FakeWorkspace.instances[0].calls
    assert "push" in calls and "delete_remote_branch" in calls
    assert calls.index("delete_remote_branch") > calls.index("push")
    assert _FakeWorkspace.instances[0].cleaned is True


@pytest.mark.asyncio
async def test_probe_push_reports_when_branch_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """推上去了却删不掉 → 必须报失败并点名分支，不能报「通过」。"""
    _FakeWorkspace.install(monkeypatch)
    ws_cls = _FakeWorkspace

    def bad_delete(self, _url, _branch, **_kw):  # noqa: ANN001, ARG002
        self.calls.append("delete_remote_branch")
        return _result(ok=False, stderr="permission denied")

    monkeypatch.setattr(ws_cls, "delete_remote_branch", bad_delete)
    row = await _probe_push(_FakeAdapter(), REF, "tok")

    assert row["result"] == "❌"
    assert "fissue-doctor-probe" in row["detail"] or PROBE_BRANCH in row["detail"]
