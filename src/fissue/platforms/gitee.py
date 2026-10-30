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

    #: Gitee 的 ``POST /issues/{n}/labels`` **只认裸 JSON 数组**。
    #:
    #: 实测（2026-10-30，私有靶子 `ccqtp/fissue-write-probe`，`#IKM265`）：
    #: ``{"labels": ["x"]}`` → ``400 Problems parsing JSON``；``["x"]`` → ``201``。
    #: 在此之前本项目一直按 dict 形状发，于是「打标签失败」被记成「Gitee 不支持条目级写」，
    #: 而真相是**形状发错了**——同一个端点换个体就是通的。见 BENCHMARK.md §4.16。
    item_label_body = "list"
