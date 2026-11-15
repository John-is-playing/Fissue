"""各平台的**已知能力**：静态结论，不是探测结果。

为什么需要它
------------
``fissue platforms doctor`` 只探测「可逆」的写（建/删标签、推分支）。建 Issue 会
在仓库里留下一条要人理的记录，所以它**刻意不探**——于是「Gitee 建/改 Issue 返回
404」这条实测结论只活在 ``BENCHMARK.md`` 与 ``README.md`` 里，用户要自己撞上 404
才知道。把结论写成数据，``doctor`` 与 Web 设置页就能在**动手之前**说出来。

口径（三态，缺一不可）
----------------------
* ``True``：实测通过；
* ``False``：实测不可用，原因写在 :attr:`PlatformCapabilities.known_limit`；
* ``None``：**尚未测量**。

``None`` 不能并进 ``False``：「没测过」和「测过不行」是两件事，混在一起的后果是
有人拿着一张「全部不可用」的表去排查一个根本没试过的能力；反过来把 ``None`` 写成
``True`` 更糟——那是替平台担保。
"""

from __future__ import annotations

from typing import NamedTuple

from ..models import Platform


class PlatformCapabilities(NamedTuple):
    """一个平台在 Fissue 用到的各条链路上「能不能做」。"""

    read: bool | None
    clone: bool | None
    write_labels: bool | None
    write_comments: bool | None
    push_branch: bool | None
    #: 建/改 Issue 是 Fissue 暂时唯一用不到的能力（它读 Issue、给 Issue 打标签、
    #: 但它自己不提 Issue）。留着这两栏是为了让「这个平台这块不行」有地方写——
    #: 真有一天要提 Issue，结论已经在这儿了。
    create_issue: bool | None
    update_issue: bool | None
    #: 提 PR —— Fissue 的**核心产出**。此前这一栏根本不存在，于是「这个平台能不能提 PR」
    #: 只能靠读代码或真跑一遍才知道。填 True 只在实测通过之后。
    create_pr: bool | None = None
    #: 一句话说明「已知做不到的那部分」；空串表示没有已知限制。
    known_limit: str = ""


#: 实测结论（来源见 BENCHMARK.md §5「四平台」一行与 §4.16）。
CAPABILITIES: dict[Platform, PlatformCapabilities] = {
    Platform.GITHUB: PlatformCapabilities(
        read=True, clone=True, write_labels=True, write_comments=True,
        push_branch=True, create_issue=True, update_issue=True, create_pr=True,
    ),
    Platform.GITEE: PlatformCapabilities(
        read=True, clone=True, write_labels=True, write_comments=True,
        push_branch=True, create_issue=False, update_issue=False, create_pr=None,
        known_limit=(
            "建/改 Issue 返回 404：已排除令牌类型、仓库可见性与 Bearer 通道，"
            "待企业版空间复核（BENCHMARK §4.16）"
        ),
    ),
    # AtomGit 的提 PR **已实测通过**（2026-11-15，探针仓库 qiaoen_python/fissue-platform-probe：
    # 建 PR #1/#2/#3 成功、同分支重复提交回 409 后复用已有 PR）。
    # 但实测同时暴露两处适配缺陷，已一并修掉：POST 响应不给 `html_url`（链接要自己拼）、
    # PR 网页段是 `/merge_requests` 而 API 段是 `/pulls`。
    # 建/改 Issue 仍然没试过，留 None。
    Platform.ATOMGIT: PlatformCapabilities(
        read=True, clone=True, write_labels=True, write_comments=True,
        push_branch=True, create_issue=None, update_issue=None, create_pr=True,
    ),
    # GitLab 的提 PR（Merge Request）**仍未实测**：它的适配器是独立实现，与 v5 不同源，
    # 不能拿 AtomGit 的结论外推。
    Platform.GITLAB: PlatformCapabilities(
        read=True, clone=True, write_labels=True, write_comments=True,
        push_branch=True, create_issue=None, update_issue=None, create_pr=None,
    ),
}

#: 未登记平台的兜底：全部「未测量」。
UNKNOWN = PlatformCapabilities(
    read=None, clone=None, write_labels=None, write_comments=None,
    push_branch=None, create_issue=None, update_issue=None, create_pr=None,
)

#: 能力字段 → 界面用的人话。README 与 doctor 共用同一套叫法，免得两处各起一个名字。
CAPABILITY_LABELS: tuple[tuple[str, str], ...] = (
    ("read", "只读抓取"),
    ("clone", "克隆仓库"),
    ("write_labels", "打标签"),
    ("write_comments", "评论"),
    ("push_branch", "推分支"),
    ("create_issue", "建 Issue"),
    ("update_issue", "改 Issue"),
    ("create_pr", "提 PR"),
)


def describe(platform: Platform) -> PlatformCapabilities:
    """取某个平台的已知能力（未登记的平台按「全部未测量」处理）。"""
    return CAPABILITIES.get(platform, UNKNOWN)


def known_limits(platform: Platform) -> list[tuple[str, bool | None]]:
    """列出该平台上**不是 ``True``** 的能力，供界面标注「做不到 / 没测过」。"""
    caps = describe(platform)
    return [
        (label, getattr(caps, field))
        for field, label in CAPABILITY_LABELS
        if getattr(caps, field) is not True
    ]


def known_limit_text(platform: Platform) -> str:
    """给界面用的一句话：这个平台已知做不到什么、或哪些还没测。

    没有这些结论时返回空串——界面据此决定「不显示这一块」，而不是显示一句
    「一切正常」（那句话在 ``None`` 上就是撒谎）。
    """
    caps = describe(platform)
    if caps.known_limit:
        return caps.known_limit
    unknown = [label for label, ok in known_limits(platform) if ok is None]
    return ("尚未测量：" + "、".join(unknown)) if unknown else ""