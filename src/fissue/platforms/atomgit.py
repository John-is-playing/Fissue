"""AtomGit 适配器（AtomGit OpenAPI v5）。

文档：https://docs.atomgit.com/
AtomGit OpenAPI 的路径与 Gitee v5 一致，需要
``X-Api-Version: 2023-02-21`` 头，鉴权用 ``Authorization: Bearer <token>``。
"""

from __future__ import annotations

from ..models import Platform
from .v5 import V5Adapter


class AtomGitAdapter(V5Adapter):
    platform = Platform.ATOMGIT
    default_api_base = "https://api.atomgit.com/api/v5"
    web_host = "atomgit.com"
    api_version_header = "2023-02-21"
    pr_segment = "pulls"
