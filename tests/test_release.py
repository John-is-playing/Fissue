"""``scripts/release.py`` 的离线测试。

为什么单独盯这个脚本
--------------------
发布是本项目**唯一不可逆**的常规动作：tag 一旦推到两个远端，就只能靠「再发一版」收场。
而 `v1.3.1` 那次发布是手敲的，当场就出现了「AtomGit 的 Release 正文被缩写成另一份」
这种偏差——不是能力问题，是**没把口径写成可执行的代码**。

所以这里的用例钉的是口径而不是「脚本能跑」：

* CHANGELOG 段的**边界**（按下一个二级标题截，不能按 ``---`` 截——发布段里本来就有分隔线）；
* 发布前的三处版本号必须自洽，工作区必须干净（手工发布最容易漏、事后最难查的两件事）；
* 干跑**真的什么都不做**（默认值必须偏向「什么都没发生」）；
* 两个平台的 payload 各自带对该平台才认的字段（GitHub 的 ``draft``、AtomGit 的
  ``release_status``），正文用同一份原文。

加载方式与 ``test_publish_wiki.py`` 一致：脚本不在可 import 的包里，按**文件路径**加载。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _load()

CHANGELOG_SAMPLE = """# 更新日志

## [Unreleased]

> **未发布**：`v9.9.9` 之后的变更。

（暂无）

---

## [1.3.2] - 2026-11-02

### 修复 —— 某件事

- 第一行
- 第二行

---

## [1.3.1] - 2026-11-02

### 变更 —— 旧的一版

- 不该被抽到
"""


# ---------------------------------------------------------------------------
# 取段：边界是「下一个二级标题」，不是分隔线
# ---------------------------------------------------------------------------


def test_section_body_stops_at_the_next_level_two_heading() -> None:
    """正文要止于下一个 ``## [``，且把段尾的 ``---`` 与空行去掉。

    按 ``---`` 截是最容易顺手写错的版本：发布段里本来就散落着分隔线，一截就把
    「修复」和「测试」两节砍掉一半，而 Release 页面看起来仍然正常。
    """
    body = release.section_body(CHANGELOG_SAMPLE, "1.3.2")

    assert body is not None
    assert body.startswith("### 修复 —— 某件事")
    assert "第二行" in body
    assert "旧的一版" not in body, "不能跨到下一版"
    assert not body.rstrip().endswith("---"), "段尾分隔线属于排版，不属于这一版"


def test_section_body_returns_none_when_the_version_is_missing() -> None:
    """没有对应段就返回 None——上层据此把「正文会是空」变成一条体检问题。"""
    assert release.section_body(CHANGELOG_SAMPLE, "2.0.0") is None


def test_section_body_reads_the_real_changelog() -> None:
    """拿真文件再验一次：`## [1.3.1]` 段存在且正文不是空的。

    纯样例能过、真文件却抽不出来（例如标题写成 `## [1.3.1] - 日期` 与正则不匹配），
    是这类脚本最典型的「测了个寂寞」。
    """
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    body = release.section_body(text, "1.3.1")
    assert body is not None
    assert "正式版" in body
    assert "[Unreleased]" not in body


# ---------------------------------------------------------------------------
# 命名与体检
# ---------------------------------------------------------------------------


def test_build_plan_matches_the_established_naming() -> None:
    """分支 ``rel/<版本>-rel``、tag ``v<版本>-rel``，两者指向同一提交。"""
    plan = release.build_plan("1.3.2")

    assert plan["tag"] == "v1.3.2-rel"
    assert plan["branch"] == "rel/1.3.2-rel"
    assert plan["name"] == "Fissue v1.3.2"


def test_versions_are_read_from_pyproject_and_dunder() -> None:
    assert release.read_version('version = "1.3.2"\n') == "1.3.2"
    assert release.read_version('name = "x"\n') is None
    assert release.read_dunder_version('__version__ = "1.3.2"\n') == "1.3.2"


def test_preflight_passes_when_everything_lines_up() -> None:
    problems = release.preflight(
        "1.3.2",
        changelog=CHANGELOG_SAMPLE,
        pyproject='version = "1.3.2"',
        dunder="1.3.2",
        dirty=[],
    )
    assert problems == []


def test_preflight_catches_every_way_a_manual_release_goes_wrong() -> None:
    """三处版本号不一致 + 缺 CHANGELOG 段 + 工作区脏，都要各自点名。

    一次性全部报出来（而不是报第一条就退出）：手工发布时人往往一次漏好几处，
    来回跑五遍才知道还差什么，最后就变成「算了先发出去」。
    """
    problems = release.preflight(
        "1.3.2",
        changelog=CHANGELOG_SAMPLE,
        pyproject='version = "1.3.1"',
        dunder="1.3.0",
        dirty=[" M src/fissue/web/app.py"],
    )

    joined = "\n".join(problems)
    assert "pyproject.toml" in joined
    assert "__version__" in joined
    assert "工作区不干净" in joined

    missing = release.preflight(
        "9.9.9", changelog=CHANGELOG_SAMPLE, pyproject='version = "9.9.9"',
        dunder="9.9.9", dirty=[],
    )
    assert any("CHANGELOG" in p for p in missing)


# ---------------------------------------------------------------------------
# 干跑：默认什么都不做
# ---------------------------------------------------------------------------


def test_dry_run_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """不带 --yes 时必须只打印计划，一条 git 命令都不许发。

    发布脚本最贵的事故是「本来只想看看会做什么」。
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(release, "worktree_changes", lambda: [])
    monkeypatch.setattr(release, "_run_git", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(release, "_load_text", lambda path: _fake_text(path))

    assert release.main(["1.3.2"]) == 0
    assert calls == [], "干跑竟然执行了 git"


def test_dry_run_refuses_when_preflight_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release, "worktree_changes", lambda: [" M pyproject.toml"])
    monkeypatch.setattr(release, "_load_text", lambda path: _fake_text(path))

    assert release.main(["1.3.2"]) == 1


def _fake_text(path: Path) -> str:
    """把三条路径喂成「自洽的 1.3.2」，让干跑用例只关心副作用。"""
    if path == release.CHANGELOG:
        return CHANGELOG_SAMPLE
    if path == release.PYPROJECT:
        return 'version = "1.3.2"'
    return '__version__ = "1.3.2"'


# ---------------------------------------------------------------------------
# payload：两个平台各认自己的字段
# ---------------------------------------------------------------------------


def test_github_payload_marks_it_as_a_normal_release() -> None:
    payload = release.github_payload(release.build_plan("1.3.2"), "正文")
    assert payload["tag_name"] == "v1.3.2-rel"
    assert payload["target_commitish"] == "rel/1.3.2-rel"
    assert payload["draft"] is False and payload["prerelease"] is False
    assert "release_status" not in payload, "这是 AtomGit 的字段，发到 GitHub 会被忽略或报错"


def test_atomgit_payload_asks_for_the_latest_badge() -> None:
    payload = release.gitlab_like_payload(release.build_plan("1.3.2"), "正文")
    assert payload["release_status"] == "latest"
    assert payload["prerelease"] is False
    assert payload["body"] == "正文"


def test_create_releases_skips_a_platform_without_a_token() -> None:
    """缺令牌 → 如实跳过并给出补法，**不算失败**。

    把它升级成错误只会让人半途而废，留下「一边有 Release 一边没有」的假成功。
    """
    notes = release.create_releases(
        release.build_plan("1.3.2"), "正文", platforms=("atomgit",),
        github_token=None, atomgit_token=None,
    )
    assert notes and "跳过" in notes[0]
    assert "atomgit_api" in notes[0], "跳过时要写清可选补法"


def test_create_releases_reports_http_failures_instead_of_swallowing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release, "_http_post", lambda url, payload, token: (422, "bad tag"))
    notes = release.create_releases(
        release.build_plan("1.3.2"), "正文", platforms=("github",),
        github_token="t", atomgit_token=None,
    )
    assert "失败" in notes[0] and "422" in notes[0]


def test_push_targets_only_the_asked_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--only github` 时绝不能顺手推 AtomGit：两个远端是不同仓库。"""
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        release, "_run_git", lambda args, **kw: calls.append((args, kw))
    )

    release.push_platforms(release.build_plan("1.3.2"), platforms=("github",))

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert release.GITHUB_PUSH_URL in args
    assert "ssh.github.com:443" in release.GITHUB_PUSH_URL, "22 端口在部分网络被拒，通道写死 443"
    assert kwargs["env"]["GIT_SSH_COMMAND"].endswith("BatchMode=yes"), (
        "无人值守时不能挂在交互提示上"
    )