"""Gitee 适配器（开放 API v5）。

文档：https://gitee.com/api/v5/swagger
Gitee 同时支持 ``Authorization: Bearer`` 与 ``access_token`` 查询参数，
这里优先用查询参数以兼容部分只认该方式的接口。
"""

from __future__ import annotations

from ..models import Platform
from .v5 import V5Adapter


class GiteeAdapter(V5Adapter):
    platform = Platform.GITEE
    default_api_base = "https://gitee.com/api/v5"
    web_host = "gitee.com"
    use_query_token = True
    pr_segment = "pulls"
