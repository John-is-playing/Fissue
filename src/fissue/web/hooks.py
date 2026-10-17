"""入站 webhook：把平台的推送**验明正身**，再翻译成「哪一条条目被动了」。

安全边界（先读这段）
--------------------
这个端点**不接 bearer 令牌**——平台不会带 `Authorization`，它带的是自己的签名。所以：

* **签名/密钥就是身份证明**。某个平台没配密钥时，它的请求一律 403（fail-closed，见
  :func:`signature_ok`）；配置层在启动时也会拒绝这种状态（`WebhookConfig`）。
* 一律**常数时间**比对（`hmac.compare_digest`），不给时间侧信道留缝。
* 通知里说哪个仓库不算数：只有 `repos` 里**登记且启用**的仓库才会被接受，其余一律忽略
  （调用方按 `HookEvent.repo_slug` 自己核对）。
* 事件体只被**解析**、不被信任：解析出来的仓库/编号会再走一遍正常的抓取口径（游标、过滤、
  早停都在），所以伪造一条「新 Issue」最多是让我们多拉一次，不会绕过任何东西。

各平台怎么校验
--------------
=========  ============================  ==========================================
平台        头                           校验
=========  ============================  ==========================================
GitHub      ``X-Hub-Signature-256``       ``sha256=<hex>``，HMAC-SHA256(secret, body)
GitLab      ``X-Gitlab-Token``            与密钥**逐字节相等**
Gitee       ``X-Gitee-Token``             同上
AtomGit     ``X-Gitee-Token``             同上（v5 同形状；**未实测**，见下方注释）
=========  ============================  ==========================================

事件体的形状各平台不同（GitHub 一层、GitLab/Gitee 另一层），所以解析是**形状容忍**的：认得
`repository.full_name` / `project.path_with_namespace`，认得 `issue.number` /
`pull_request.number` / `object_attributes.iid`。认不出就返回带 `reason` 的事件（调用方回
202 并写明忽略原因），而不是猜一个编号出来——**猜错等于抓错条目**。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger
from ..models import ItemType, Platform

log = get_logger(__name__)

#: 共享密钥头：GitLab 用它，Gitee / AtomGit 也用它（v5 同形状）。
TOKEN_HEADER = {
    Platform.GITLAB: "x-gitlab-token",
    Platform.GITEE: "x-gitee-token",
    Platform.ATOMGIT: "x-gitee-token",      # 未实测：AtomGit 的 webhook 文档页取不到
    Platform.GITHUB: "",
}

#: GitHub 的签名头（sha256 是默认；老式的 sha1 头不再接受——现在的 GitHub 两个都发）。
GITHUB_SIGNATURE_HEADER = "x-hub-signature-256"


@dataclass
class HookEvent:
    """一次推送翻译出来的结果。``reason`` 非空表示「收到了但不处理」。"""

    platform: Platform
    repo_slug: str = ""
    number: int | str | None = None
    item_type: ItemType = ItemType.ISSUE
    action: str = ""
    reason: str = ""

    @property
    def usable(self) -> bool:
        return not self.reason and bool(self.repo_slug) and self.number is not None


def _header(headers: Mapping[str, Any], name: str) -> str:
    """取头（大小写不敏感）。Starlette 的 `Headers` 与普通 dict 都认。"""
    value = headers.get(name) if hasattr(headers, "get") else None
    if value:
        return str(value)
    wanted = name.lower()
    for key, val in dict(headers).items():
        if str(key).lower() == wanted:
            return str(val)
    return ""


def signature_ok(platform: Platform, secret: str, body: bytes, headers: Mapping[str, Any]) -> bool:
    """校验这次推送是不是平台发来的。

    **没配密钥一律 False**（fail-closed）：这个端点没有别的身份证明，密钥为空就等于大门敞开，
    宁可拒绝所有推送（操作方会看到 403 与一条日志），也不能让任何人都能触发抓取。
    """
    if not secret:
        return False

    if platform is Platform.GITHUB:
        provided = _header(headers, GITHUB_SIGNATURE_HEADER)
        if not provided.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided[len("sha256=") :].strip(), expected)

    name = TOKEN_HEADER.get(platform, "")
    if not name:
        return False
    provided = _header(headers, name)
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


def _dig(payload: Any, *path: str) -> Any:
    """按路径取嵌套值；中途不是 dict 就返回 None（不抛错——事件体是外部输入）。"""
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _repo_slug(payload: Any) -> str:
    """仓库标识：GitHub 是 `repository.full_name`，GitLab/Gitee 是 `project.path_with_namespace`。"""
    slug = _first_str(
        _dig(payload, "repository", "full_name"),
        _dig(payload, "repository", "path_with_namespace"),
        _dig(payload, "project", "path_with_namespace"),
        _dig(payload, "project", "path"),
        _dig(payload, "repository", "fullName"),
    )
    return slug if "/" in slug else ""


def parse_event(platform: Platform, payload: Any) -> HookEvent:
    """把事件体翻译成「哪个仓库、哪一条、Issue 还是 PR」。

    认不出就返回带 ``reason`` 的结果——**不猜**：猜错编号等于去抓错条目。
    """
    if not isinstance(payload, dict):
        return HookEvent(platform=platform, reason="事件体不是 JSON 对象")

    slug = _repo_slug(payload)
    action = _first_str(
        payload.get("action"),
        _dig(payload, "object_attributes", "action"),
        _dig(payload, "object_attributes", "state"),
    )

    pr_number = _first_str(
        _dig(payload, "pull_request", "number"),
        _dig(payload, "object_attributes", "iid"),
        _dig(payload, "object_attributes", "number"),
        _dig(payload, "merge_request", "number"),
    )
    issue_number = _first_str(_dig(payload, "issue", "number"), _dig(payload, "issue", "iid"))

    if pr_number:
        number: int | str = int(pr_number) if pr_number.isdigit() else pr_number
        item_type = ItemType.PR
    elif issue_number:
        number = int(issue_number) if issue_number.isdigit() else issue_number
        item_type = ItemType.ISSUE
    else:
        return HookEvent(
            platform=platform, repo_slug=slug, action=action,
            reason="事件体里没有 Issue/PR 编号（不是条目类事件？）",
        )

    if not slug:
        return HookEvent(
            platform=platform, number=number, item_type=item_type, action=action,
            reason="事件体里没有仓库标识",
        )

    return HookEvent(
        platform=platform, repo_slug=slug, number=number, item_type=item_type, action=action
    )
