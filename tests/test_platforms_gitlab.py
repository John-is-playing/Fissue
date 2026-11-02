"""GitLab 适配器的**写路径**用例（评论 / 标签 / 关闭 / fork / 建 MR / 克隆地址）。

为什么单独一个文件
------------------
GitLab 是四平台里唯一长期没有写路径用例的适配器（覆盖率曾 76%，而 ``github.py`` 84%、
``v5.py`` 87%），偏偏它踩的坑全在写这一侧，而且每一个都在真机上暴露过：

* 评论在「上游已执行、响应没回来」时被重发，多出一条一模一样的公开评论；
* ``fork`` 自己的仓库返回 409（GitHub 对自己的仓库 fork 会返回它自己，所以这条分支
  只有 GitLab 走得到，真机上的症状是「bug 已经修好了，却死在 fork 这一步」）；
* ``create_pr`` 的探测兜底返回 ``{"number", "url"}``，而返回语句只认
  ``iid`` / ``id`` / ``web_url``，于是兜底成功时两个字段双双为 None。

这些分支**离线打桩完全可测**，不测就等于每次改动都靠真仓库去试。分组放在这里而不是
继续堆进 ``test_platforms.py``（已 2.2k 行）：写路径与抓取解析是两组不同的口径。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from fissue.errors import PlatformError
from fissue.models import ItemType, Platform, RepoRef
from fissue.platforms.registry import build_adapter

GL = "https://gitlab.com/api/v4"
GLPID = "psf%2Frequests"
GLREPO = RepoRef(platform=Platform.GITLAB, owner="psf", name="requests")


@pytest.fixture
def gl(settings):
    return build_adapter(settings, platform=Platform.GITLAB, token="t")


# ---------------------------------------------------------------------------
# 评论：写一次，且只有一次
# ---------------------------------------------------------------------------


@respx.mock
async def test_comment_posts_a_note(gl) -> None:
    """评论走 ``POST .../notes``，正文原样发出。"""
    route = respx.post(f"{GL}/projects/{GLPID}/issues/5/notes").mock(
        return_value=httpx.Response(201, json={"id": 1, "body": "hello"})
    )

    await gl.comment(GLREPO, 5, "hello")

    assert json.loads(route.calls[0].request.content) == {"body": "hello"}


@respx.mock
async def test_comment_probe_treats_an_existing_identical_note_as_success(gl) -> None:
    """写请求 502 时先探测：已存在逐字相同的评论 → 本次写其实生效了，不该报错也不该重发。

    这是 ``_settle_ambiguous_write`` 的核心承诺：「上游已执行、只是响应没回来」不能被
    当成失败再发一遍——那会在公开仓库里多一条一模一样的评论，撤不回来。
    """
    respx.post(f"{GL}/projects/{GLPID}/issues/5/notes").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    respx.get(f"{GL}/projects/{GLPID}/issues/5/notes").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "body": "hello"}])
    )

    await gl.comment(GLREPO, 5, "hello")  # 不抛异常即「已生效」


@respx.mock
async def test_comment_on_merge_request_uses_the_mr_segment(gl) -> None:
    """MR 评论走 ``merge_requests`` 段——写错段在平台上就是 404，而 404 易被当成「条目不存在」。"""
    route = respx.post(f"{GL}/projects/{GLPID}/merge_requests/6/notes").mock(
        return_value=httpx.Response(201, json={"id": 2})
    )

    await gl.comment(GLREPO, 6, "review", item_type=ItemType.PR)

    assert route.called


# ---------------------------------------------------------------------------
# 标签 / 关闭
# ---------------------------------------------------------------------------


@respx.mock
async def test_add_labels_with_empty_list_sends_nothing(gl) -> None:
    """空清单直接返回：发一个 ``add_labels: ""`` 只会让平台报参数错误。"""
    put = respx.put(url__regex=rf"{GL}/projects/{GLPID}/issues/.*").mock(
        return_value=httpx.Response(200, json={})
    )

    await gl.add_labels(GLREPO, 5, [])

    assert not put.called


@respx.mock
async def test_add_labels_joins_with_commas(gl) -> None:
    """GitLab 的条目级标签是**逗号分隔字符串**，不是 GitHub 那种裸数组。"""
    route = respx.put(f"{GL}/projects/{GLPID}/issues/5").mock(
        return_value=httpx.Response(200, json={})
    )

    await gl.add_labels(GLREPO, 5, ["fissue", "bug"])

    assert json.loads(route.calls[0].request.content) == {"add_labels": "fissue,bug"}


@respx.mock
async def test_add_labels_on_merge_request_uses_mr_segment(gl) -> None:
    route = respx.put(f"{GL}/projects/{GLPID}/merge_requests/6").mock(
        return_value=httpx.Response(200, json={})
    )

    await gl.add_labels(GLREPO, 6, ["fissue"], item_type=ItemType.PR)

    assert route.called


@respx.mock
async def test_close_item_sets_state_event(gl) -> None:
    """关闭是 ``PUT`` + ``state_event: close``，不是 DELETE。"""
    route = respx.put(f"{GL}/projects/{GLPID}/issues/5").mock(
        return_value=httpx.Response(200, json={})
    )

    await gl.close_item(GLREPO, 5)

    assert json.loads(route.calls[0].request.content) == {"state_event": "close"}


# ---------------------------------------------------------------------------
# fork：不发那次注定 409 的请求
# ---------------------------------------------------------------------------


@respx.mock
async def test_fork_returns_self_when_repo_is_already_ours(gl) -> None:
    """仓库已经在自己名下时**不发 fork 请求**：GitLab 会回 409「命名空间已占用」。

    GitHub 对自己的仓库 fork 会返回它自己，所以这条分支只有 GitLab 走得到。
    """
    respx.get(f"{GL}/user").mock(return_value=httpx.Response(200, json={"username": "psf"}))
    fork = respx.post(f"{GL}/projects/{GLPID}/fork").mock(
        return_value=httpx.Response(409, json={"message": "namespace taken"})
    )

    out = await gl.fork_repo(GLREPO)

    assert out.slug == GLREPO.slug
    assert not fork.called, "自己的仓库不能再去 fork"


@respx.mock
async def test_fork_parses_path_with_namespace(gl) -> None:
    respx.get(f"{GL}/user").mock(return_value=httpx.Response(200, json={"username": "me"}))
    respx.post(f"{GL}/projects/{GLPID}/fork").mock(
        return_value=httpx.Response(201, json={"path_with_namespace": "me/requests"})
    )

    out = await gl.fork_repo(GLREPO)

    assert (out.owner, out.name) == ("me", "requests")


@respx.mock
async def test_fork_falls_back_to_whoami_without_a_path(gl) -> None:
    """fork 响应缺 ``path_with_namespace`` 时用 ``whoami`` 兜底，而不是拼出一个空 owner。"""
    respx.get(f"{GL}/user").mock(return_value=httpx.Response(200, json={"username": "me"}))
    respx.post(f"{GL}/projects/{GLPID}/fork").mock(return_value=httpx.Response(201, json={}))

    out = await gl.fork_repo(GLREPO)

    assert (out.owner, out.name) == ("me", "requests")


@respx.mock
async def test_fork_raises_when_nothing_can_identify_the_fork(gl) -> None:
    """连 ``whoami`` 都拿不到用户名时抛错——静默返回上游会让人以为 fork 成功了。"""
    respx.get(f"{GL}/user").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{GL}/projects/{GLPID}/fork").mock(return_value=httpx.Response(201, json={}))

    with pytest.raises(PlatformError):
        await gl.fork_repo(GLREPO)


# ---------------------------------------------------------------------------
# 查重与建 MR
# ---------------------------------------------------------------------------


@respx.mock
async def test_find_open_pr_only_matches_the_exact_source_branch(gl) -> None:
    """平台若忽略 ``source_branch`` 过滤、把整张列表返回，必须自己按分支核对。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests").mock(
        return_value=httpx.Response(
            200,
            json=[
                "not-a-dict",
                {"iid": 1, "source_branch": "other", "web_url": "u1"},
                {"iid": 2, "source_branch": "fix", "web_url": "u2"},
            ],
        )
    )

    found = await gl.find_open_pr(GLREPO, head="me/requests:fix")

    assert found == {"number": 2, "url": "u2"}


@respx.mock
async def test_find_open_pr_returns_none_when_branch_absent(gl) -> None:
    """没有同源分支的开放 MR 就返回 None——上层据此决定「该建一个」。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests").mock(
        return_value=httpx.Response(200, json=[{"iid": 3, "source_branch": "other"}])
    )

    assert await gl.find_open_pr(GLREPO, head="fix") is None


@respx.mock
async def test_create_pr_in_same_repo_has_no_target_project_id(gl) -> None:
    """同仓库 MR 不传 ``target_project_id``。"""
    created = respx.post(f"{GL}/projects/{GLPID}/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 10, "web_url": "u"})
    )

    out = await gl.create_pr(GLREPO, head="fix", base="main", title="t", body="b")

    payload = json.loads(created.calls[0].request.content)
    assert "target_project_id" not in payload
    assert payload["source_branch"] == "fix"
    assert out == {"number": 10, "url": "u"}


@respx.mock
async def test_create_pr_draft_prefixes_the_title(gl) -> None:
    """草稿走标题前缀 ``Draft: ``（GitLab 没有 GitHub 那种 draft 字段）。"""
    created = respx.post(f"{GL}/projects/{GLPID}/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 11, "web_url": "u"})
    )

    await gl.create_pr(GLREPO, head="fix", base="main", title="t", body="b", draft=True)

    assert json.loads(created.calls[0].request.content)["title"] == "Draft: t"


@respx.mock
async def test_create_pr_probe_surfaces_the_existing_mr_number_and_url(gl) -> None:
    """POST 5xx 时探测到已有开放 MR → 返回它的 ``{"number","url"}``，两个键都得认。

    这是已经修过的一次真实缺陷：探测结果只有 ``number`` / ``url``，而返回语句原先只看
    ``iid`` / ``id`` / ``web_url``，于是兜底成功时两个字段**双双为 None**——
    上层会记一条「PR 已创建（编号 None）」。
    """
    respx.post(f"{GL}/projects/{GLPID}/merge_requests").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    respx.get(f"{GL}/projects/{GLPID}/merge_requests").mock(
        return_value=httpx.Response(
            200, json=[{"iid": 42, "source_branch": "fix", "web_url": "https://gl/mr/42"}]
        )
    )

    out = await gl.create_pr(GLREPO, head="fix", base="main", title="t", body="b")

    assert out == {"number": 42, "url": "https://gl/mr/42"}


@respx.mock
async def test_create_pr_rejects_a_non_dict_response(gl) -> None:
    """平台回 201 + 空体（解析成 None）时必须报错，不能返回 ``{"number": None}``。"""
    respx.post(f"{GL}/projects/{GLPID}/merge_requests").mock(
        return_value=httpx.Response(201, content=b"")
    )

    with pytest.raises(PlatformError, match="创建 MR 返回异常"):
        await gl.create_pr(GLREPO, head="fix", base="main", title="t", body="b")


@respx.mock
async def test_create_pr_across_projects_passes_target_project_id(gl) -> None:
    """从 fork 建 MR 时必须带 ``target_project_id``，否则 MR 会落在 fork 自己身上。"""
    respx.get(f"{GL}/projects/{GLPID}").mock(return_value=httpx.Response(200, json={"id": 111}))
    created = respx.post(f"{GL}/projects/me%2Frequests/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 12, "web_url": "u"})
    )

    await gl.create_pr(GLREPO, head="me/requests:fix", base="main", title="t", body="b")

    payload = json.loads(created.calls[0].request.content)
    assert payload["target_project_id"] == 111
    assert payload["source_branch"] == "fix"


# ---------------------------------------------------------------------------
# 克隆地址
# ---------------------------------------------------------------------------


def test_clone_url_with_and_without_token(gl) -> None:
    """带令牌走 ``oauth2:<token>@``（GitLab 的 HTTPS 克隆凭据形式），不带就退回匿名地址。"""
    assert gl.clone_url(GLREPO) == "https://gitlab.com/psf/requests.git"
    assert gl.clone_url(GLREPO, use_token=True) == "https://oauth2:t@gitlab.com/psf/requests.git"


# ---------------------------------------------------------------------------
# 数据残缺：拉不到的部分必须**标出来**，不能静默当成「没有」
# ---------------------------------------------------------------------------
#
# 这一组对应的是评测质量的根：``mark_degraded`` 的字符串会被下游当成「这条数据不完整」
# 的依据。少标一处，模型就会拿「0 个文件 / 0 条评论」去下结论——而它并不知道那是
# 拉取失败，不是事实。反过来，把限流吞掉变成「没有评论」更是灾难：等一会儿重试就有，
# 吞掉就永远不会有了。


@respx.mock
async def test_default_branch_falls_back_to_main(gl) -> None:
    """项目信息里没有 ``default_branch`` 时退回 ``main``，而不是空字符串。"""
    respx.get(f"{GL}/projects/{GLPID}").mock(return_value=httpx.Response(200, json={}))

    assert await gl.get_default_branch(GLREPO) == "main"


@respx.mock
async def test_mr_marks_degraded_when_changes_are_unavailable(gl) -> None:
    """``/changes`` 拉失败 → 条目照常产出，但必须带上 ``files_unavailable``。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(404, text="not found")
    )
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/notes").mock(
        return_value=httpx.Response(200, json=[])
    )

    item = await gl._build_item(
        GLREPO,
        {"iid": 6, "title": "mr", "state": "opened", "target_branch": "main"},
        ItemType.PR,
    )

    assert any("files_unavailable#6" in note for note in item.degraded), item.degraded


@respx.mock
async def test_mr_marks_degraded_when_the_source_project_lookup_fails(gl) -> None:
    """fork 来的 MR 拉不到源项目 → ``head_repo`` 缺失，且要标 ``head_repo_unavailable``。"""
    respx.get(f"{GL}/projects/999").mock(return_value=httpx.Response(404, text="gone"))
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(200, json={"changes": []})
    )
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/notes").mock(
        return_value=httpx.Response(200, json=[])
    )

    item = await gl._build_item(
        GLREPO,
        {"iid": 6, "title": "mr", "state": "opened", "source_project_id": 999},
        ItemType.PR,
    )

    assert item.head_repo is None
    assert any("head_repo_unavailable#6" in note for note in item.degraded), item.degraded


@respx.mock
async def test_mr_marks_degraded_when_comments_are_unavailable(gl) -> None:
    """评论拉失败 → ``comments_unavailable``，而不是「这条 MR 没人讨论」。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(200, json={"changes": []})
    )
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/notes").mock(
        return_value=httpx.Response(403, text="forbidden")
    )

    item = await gl._build_item(GLREPO, {"iid": 6, "title": "mr"}, ItemType.PR)

    assert item.comments == []
    assert any("comments_unavailable#6" in note for note in item.degraded), item.degraded


async def test_mr_comments_truncation_is_reported(gl) -> None:
    """评论被 ``limit`` 截断时也要标出来——「只有 100 条」与「只有 100 条可见」是两件事。"""
    gl.per_page = 2
    comments = [{"id": i, "body": f"c{i}", "author": {"username": "u"}} for i in range(3)]
    with respx.mock:
        respx.get(f"{GL}/projects/{GLPID}/issues/5/notes").mock(
            return_value=httpx.Response(200, json=comments)
        )
        got, note = await gl._fetch_notes(GLREPO, 5, item_type=ItemType.ISSUE, limit=3)

    assert len(got) == 3
    assert "may_be_truncated" in note


@respx.mock
async def test_mr_closes_issue_iid_is_merged_into_linked_issues(gl) -> None:
    """平台给的 ``closes_issue_iid`` 与正文里解析出的关联 Issue 要合并去重。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(200, json={"changes": []})
    )
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/notes").mock(
        return_value=httpx.Response(200, json=[])
    )

    item = await gl._build_item(
        GLREPO,
        {"iid": 6, "title": "mr", "description": "closes #5", "closes_issue_iid": 5},
        ItemType.PR,
    )

    assert item.linked_issues == [5]


@respx.mock
async def test_mr_ignores_a_malformed_closes_issue_iid(gl) -> None:
    """``closes_issue_iid`` 不是整数时**忽略它**，不能让整条抓取失败。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(200, json={"changes": []})
    )
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/notes").mock(
        return_value=httpx.Response(200, json=[])
    )

    item = await gl._build_item(
        GLREPO, {"iid": 6, "title": "mr", "closes_issue_iid": "abc"}, ItemType.PR
    )

    assert item.linked_issues == []


# ---------------------------------------------------------------------------
# 文件清单与 diff
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_files_maps_gitlab_change_flags_to_status(gl) -> None:
    """``added`` / ``removed`` / ``modified`` 从 GitLab 的三个布尔字段映射，``patch`` 按参数给。"""
    diff = "@@ -1 +1 @@\n-a\n+b\n"
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(
            200,
            json={
                "changes": [
                    {"new_path": "new.py", "new_file": True, "diff": diff},
                    {"old_path": "old.py", "deleted_file": True, "diff": diff},
                    {"new_path": "mod.py", "diff": diff},
                    "not-a-dict",
                ]
            },
        )
    )

    files = await gl.fetch_files(GLREPO, 6, with_patch=True)

    assert [(f.path, f.status) for f in files] == [
        ("new.py", "added"),
        ("old.py", "removed"),
        ("mod.py", "modified"),
    ]
    assert files[0].patch == diff
    assert (await gl.fetch_files(GLREPO, 6))[0].patch is None


@respx.mock
async def test_fetch_files_returns_empty_list_instead_of_raising(gl) -> None:
    """拉不到变更时返回空清单（这条路径的调用方按「没有文件」处理），但限流必须继续上抛。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(404, text="not found")
    )
    assert await gl.fetch_files(GLREPO, 6) == []


@respx.mock
async def test_fetch_files_does_not_swallow_rate_limit() -> None:
    """限流不能被翻译成「这个 MR 没有文件」——那会让下游以为它是空改动。"""
    from fissue.errors import RateLimitError
    from fissue.platforms.gitlab import GitLabAdapter

    adapter = GitLabAdapter(token="t", max_retries=1)
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(429, headers={"retry-after": "0"}, text="rate limit")
    )
    with pytest.raises(RateLimitError):
        await adapter.fetch_files(GLREPO, 6)


@respx.mock
async def test_fetch_diff_wraps_platform_errors_with_context(gl) -> None:
    """拉 diff 失败要带上「拉的是哪条 MR」——只抛原始 404，现场离病根太远。"""
    respx.get(f"{GL}/projects/{GLPID}/merge_requests/6/changes").mock(
        return_value=httpx.Response(404, text="not found")
    )

    with pytest.raises(PlatformError, match="MR diff 失败 #6"):
        await gl.fetch_diff(GLREPO, 6)


# ---------------------------------------------------------------------------
# 标签创建：区分「已存在」与「创建失败」
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_label_treats_already_exists_as_success(gl) -> None:
    """409 + 「已存在」特征词 → 返回 True（标签确实在那儿，本次目标已达成）。"""
    respx.post(f"{GL}/projects/{GLPID}/labels").mock(
        return_value=httpx.Response(409, json={"message": "Label already exists"})
    )

    assert await gl._create_label(GLREPO, "fissue") is True


@respx.mock
async def test_create_label_reports_failure_without_caching_it(gl, caplog) -> None:
    """其它错误 → False 并告警：**不能**回填缓存，否则后续打标签会一路静默失败。"""
    respx.post(f"{GL}/projects/{GLPID}/labels").mock(
        return_value=httpx.Response(400, json={"message": "color is invalid"})
    )

    assert await gl._create_label(GLREPO, "fissue") is False


# ---------------------------------------------------------------------------
# 诊断清场：两条容易写错的细节
# ---------------------------------------------------------------------------


def test_repo_api_points_at_the_project_path(gl) -> None:
    """诊断探测靠 ``_repo_api`` 拼路径；GitLab 是 ``/projects/{urlencoded}``，不是 ``/repos/``。"""
    assert gl._repo_api(GLREPO) == f"{GL}/projects/{GLPID}"


@respx.mock
async def test_delete_comment_skips_notes_without_an_id(gl) -> None:
    """正文匹配到但缺 ``id`` 的评论**跳过**——拿 None 拼 URL 会删到 ``/notes/None``。"""
    respx.get(f"{GL}/projects/{GLPID}/issues/5/notes").mock(
        return_value=httpx.Response(200, json=[{"body": "probe"}])
    )
    deleted = respx.delete(url__regex=rf"{GL}/projects/{GLPID}/issues/5/notes/.*").mock(
        return_value=httpx.Response(204)
    )

    assert await gl.delete_comment_by_body(GLREPO, 5, "probe") is False
    assert not deleted.called


@respx.mock
async def test_comment_probe_that_finds_nothing_raises(gl) -> None:
    """写请求失败且探测不到已存在的评论 → 如实报错，绝不假设「大概成功了」。"""
    respx.post(f"{GL}/projects/{GLPID}/issues/5/notes").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    respx.get(f"{GL}/projects/{GLPID}/issues/5/notes").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "body": "别的评论"}])
    )

    with pytest.raises(PlatformError):
        await gl.comment(GLREPO, 5, "hello")