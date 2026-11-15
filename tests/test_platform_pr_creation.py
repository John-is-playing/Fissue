"""提 PR 的跨平台细节：网页段名、链接回退、409「已存在」复用。

这几条都是 **AtomGit 真实仓库上实测暴露出来的**（探针仓库
`qiaoen_python/fissue-platform-probe`，PR #1/#2/#3）：

1. `POST /pulls` 的响应里**没有** `html_url`，只有一个 `web_url`，而那个 `web_url`
   是**列表页**（`…/merge_requests`，不带编号）——拿它当 PR 链接会把人送错地方；
2. PR 的**网页段是 `/merge_requests`**，而 **API 段是 `/pulls`**：两者混用会让条目页
   上的 PR 链接 404；
3. 同一分支重复提交时平台回 **409「already exists」**——这不是失败，目标状态已经达成。
"""

from __future__ import annotations

import pytest

from fissue.errors import PlatformError
from fissue.models import ItemType, Platform, RepoRef
from fissue.platforms.registry import build_adapter

REF = RepoRef(platform=Platform.ATOMGIT, owner="qiaoen_python", name="fissue-platform-probe")


@pytest.fixture
def atomgit(settings):
    return build_adapter(settings, platform=Platform.ATOMGIT, token="t")


@pytest.fixture
def gitee(settings):
    return build_adapter(settings, platform=Platform.GITEE, token="t")


def test_atomgit_pr_web_segment_is_merge_requests(atomgit) -> None:
    """网页段与 API 段必须分开：AtomGit 是 `/merge_requests` + `/pulls`。"""
    assert atomgit.pr_segment == "pulls", "API 端点仍要走 /pulls"
    assert (
        atomgit.item_url(REF, 2, ItemType.PR)
        == "https://atomgit.com/qiaoen_python/fissue-platform-probe/merge_requests/2"
    )
    assert (
        atomgit.item_url(REF, 1, ItemType.ISSUE)
        == "https://atomgit.com/qiaoen_python/fissue-platform-probe/issues/1"
    )


def test_gitee_pr_web_segment_unchanged(gitee) -> None:
    """回归：别把「网页段独立」这件事顺手套到 Gitee 上——它的网页就是 `/pulls/n`。"""
    ref = RepoRef(platform=Platform.GITEE, owner="o", name="n")
    assert gitee.pr_web_segment == "pulls"
    assert gitee.item_url(ref, 2, ItemType.PR) == "https://gitee.com/o/n/pulls/2"


@pytest.mark.asyncio
async def test_create_pr_falls_back_to_item_url(atomgit, monkeypatch) -> None:
    """平台不给链接（AtomGit 的 POST 就只回 web_url 列表页）时要自己拼一条。"""

    async def fake_post(url, payload, *, probe=None):
        return {"number": 7, "id": 123, "web_url": "https://atomgit.com/o/n/merge_requests"}

    monkeypatch.setattr(atomgit, "_post", fake_post)
    result = await atomgit.create_pr(
        REF, head="fissue/fix-1", base="main", title="t", body="b"
    )
    assert result["number"] == 7
    assert result["url"].endswith("/merge_requests/7")


@pytest.mark.asyncio
async def test_create_pr_reuses_existing_pr_on_409(atomgit, monkeypatch) -> None:
    """409「同一分支已有 open PR」不是失败：复用那个 PR，别记成失败反复重试。"""

    async def boom(url, payload, *, probe=None):
        raise PlatformError(
            "AtomGit API 错误 409：POST …/pulls -> "
            '{"error_message":"Another open merge request already exists '
            'for this source branch: !3"}'
        )

    async def fake_find(upstream, *, head):
        return {"number": 3, "url": None}

    monkeypatch.setattr(atomgit, "_post", boom)
    monkeypatch.setattr(atomgit, "find_open_pr", fake_find)

    result = await atomgit.create_pr(
        REF, head="fissue/probe-fix3", base="main", title="t", body="b"
    )
    assert result["number"] == 3
    assert result["url"].endswith("/merge_requests/3")


@pytest.mark.asyncio
async def test_create_pr_still_raises_on_unrelated_409(atomgit, monkeypatch) -> None:
    """409 也可能是别的原因（分支保护等）：那种必须照原样抛出去，不能被吞。"""

    async def boom(url, payload, *, probe=None):
        raise PlatformError("AtomGit API 错误 409：protected branch")

    monkeypatch.setattr(atomgit, "_post", boom)
    with pytest.raises(PlatformError):
        await atomgit.create_pr(REF, head="b", base="main", title="t", body="b")


def test_pr_exists_error_recognizes_platform_wording(atomgit) -> None:
    for text in (
        "already exists",
        "already been taken",
        "another open merge request",
        "该分支的合并请求已存在",
    ):
        assert atomgit._is_pr_exists_error(PlatformError(text)) is True
    assert atomgit._is_pr_exists_error(PlatformError("权限不足")) is False


def test_capabilities_record_create_pr() -> None:
    """能力矩阵必须有「提 PR」这一栏：它是 Fissue 的核心产出。"""
    from fissue.platforms.capabilities import CAPABILITIES, CAPABILITY_LABELS

    assert any(field == "create_pr" for field, _ in CAPABILITY_LABELS)
    assert CAPABILITIES[Platform.GITHUB].create_pr is True
    # AtomGit 已实测通过；Gitee / GitLab 仍未测过，必须留 None（不是 False）
    assert CAPABILITIES[Platform.ATOMGIT].create_pr is True
    assert CAPABILITIES[Platform.GITEE].create_pr is None
    assert CAPABILITIES[Platform.GITLAB].create_pr is None


# ---------------------------------------------------------------------------
# GitLab：同一 source_branch 重复建 MR 回 **400**（不是 409），文案与 AtomGit 同源
# ---------------------------------------------------------------------------


@pytest.fixture
def gitlab(settings):
    return build_adapter(settings, platform=Platform.GITLAB, token="t")


@pytest.mark.asyncio
async def test_gitlab_create_mr_reuses_existing_on_400(gitlab, monkeypatch) -> None:
    """GitLab 对「已有 open MR」回 400——这不是失败，要复用而不是记成失败。"""
    gl_ref = RepoRef(platform=Platform.GITLAB, owner="acme", name="web")

    async def boom(method, url, **kwargs):
        raise PlatformError(
            "GitLab API 错误 400：POST …/merge_requests -> "
            '{"message":["Another open merge request already exists '
            'for this source branch: !5"]}'
        )

    async def fake_find(upstream, *, head):
        return {"number": 5, "url": None}

    monkeypatch.setattr(gitlab, "_request", boom)
    monkeypatch.setattr(gitlab, "find_open_pr", fake_find)

    result = await gitlab.create_pr(
        gl_ref, head="fissue/fix-1", base="main", title="t", body="b"
    )
    assert result["number"] == 5
    assert result["url"] == "https://gitlab.com/acme/web/-/merge_requests/5"


@pytest.mark.asyncio
async def test_gitlab_create_mr_returns_url_fallback(gitlab, monkeypatch) -> None:
    """平台没给 web_url 时也要有链接（否则条目页 PR 链接是空的）。"""
    gl_ref = RepoRef(platform=Platform.GITLAB, owner="acme", name="web")

    async def fake_request(method, url, **kwargs):
        return {"iid": 9, "id": 4321}

    monkeypatch.setattr(gitlab, "_request", fake_request)
    result = await gitlab.create_pr(
        gl_ref, head="fissue/fix-9", base="main", title="t", body="b"
    )
    assert result["number"] == 9
    assert result["url"] == "https://gitlab.com/acme/web/-/merge_requests/9"


@pytest.mark.asyncio
async def test_gitlab_create_mr_still_raises_on_other_400(gitlab, monkeypatch) -> None:
    gl_ref = RepoRef(platform=Platform.GITLAB, owner="acme", name="web")

    async def boom(method, url, **kwargs):
        raise PlatformError("GitLab API 错误 400：source branch does not exist")

    monkeypatch.setattr(gitlab, "_request", boom)
    with pytest.raises(PlatformError):
        await gitlab.create_pr(gl_ref, head="b", base="main", title="t", body="b")


def test_gitlab_item_url_uses_merge_requests(gitlab) -> None:
    gl_ref = RepoRef(platform=Platform.GITLAB, owner="acme", name="web")
    assert gitlab.item_url(gl_ref, 5, ItemType.PR) == (
        "https://gitlab.com/acme/web/-/merge_requests/5"
    )
    assert gitlab.item_url(gl_ref, 5, ItemType.ISSUE) == "https://gitlab.com/acme/web/-/issues/5"
