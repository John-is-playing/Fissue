"""AtomGit 适配器（AtomGit OpenAPI v5）。

文档：https://docs.atomgit.com/
AtomGit OpenAPI 的路径与 Gitee v5 一致，需要
``X-Api-Version: 2023-02-21`` 头，鉴权用 ``Authorization: Bearer <token>``。

**但标签写接口的形状与 Gitee 不同**——见下面两个覆写（2026-10-15 实测）。
"""

from __future__ import annotations

from ..errors import PlatformError, RateLimitError
from ..logging_setup import get_logger
from ..models import ItemType, Platform, RepoRef
from .v5 import V5Adapter

log = get_logger(__name__)


class AtomGitAdapter(V5Adapter):
    platform = Platform.ATOMGIT
    default_api_base = "https://api.atomgit.com/api/v5"
    web_host = "atomgit.com"
    api_version_header = "2023-02-21"
    pr_segment = "pulls"

    # -- 标签写接口：形状与 Gitee 不同（实测，别照 v5 默认写）------------------
    #
    # 用 v5 默认做法（JSON 体 + 颜色去掉 `#`）会连撞三个 400：
    #   * `POST /labels` + JSON 体 → `Required request parameter 'name'`
    #     （它在要**表单参数**，不是 JSON 字段）；
    #   * 颜色传裸十六进制       → `Color must be a valid color code`
    #     （它要**带 `#`** 的 6 位；具名颜色如 green 也收）；
    #   * `POST /issues/{n}/labels`（JSON 与表单都试过）→ `Request body parsing error`，
    #     那个端点怎么发都不对。
    #
    # 正确做法：建标签走 **form 表单** + **带 `#` 的颜色**；
    # 给条目打标签走 **PATCH 条目自身**、`labels` 传**逗号分隔的字符串**。

    async def _create_label(
        self, repo: RepoRef, label: str, *, color: str = "0e8a16", description: str = "Fissue AI"
    ) -> bool:
        color = color if color.startswith("#") else f"#{color}"
        try:
            await self._request(
                "POST", f"{self._repo_api(repo)}/labels", data={"name": label, "color": color}
            )
            log.info("[atomgit] 已创建标签 %s", label)
            return True
        except RateLimitError:
            raise
        except PlatformError as exc:
            if self._is_already_exists(exc):
                log.info("[atomgit] 标签 %s 已存在", label)
                return True
            log.warning("[atomgit] 创建标签失败（不计入缓存，稍后会重试）：%s", exc)
            return False

    async def add_labels(
        self, repo: RepoRef, number: int | str, labels: list[str], item_type: ItemType = ItemType.ISSUE
    ) -> None:
        """给条目打标签：PATCH 条目自身，`labels` 传逗号分隔字符串。

        `labels` 为空时直接返回——实测空串会被拒（400），而调用方本来就只加不删。
        """
        if not labels:
            return
        segment = "issues" if item_type is ItemType.ISSUE else self.pr_segment
        await self._patch(
            f"{self._repo_api(repo)}/{segment}/{number}", {"labels": ",".join(labels)}
        )
