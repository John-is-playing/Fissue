"""把 LLM 用量归到"当前正在处理的条目"上。

为什么用 ContextVar 而不是给每个调用点加参数
--------------------------------------------
`llm_usage` 原先只有 `repo_key`：能回答"这个仓库今天花了多少"，回答不了
"**这条 PR** 花了多少"——而后者正是用户最关心的数（一条修了 ¥0.8 还是 ¥8）。

给 `client.chat(...)` 加 `item_key` 参数看起来更直白，但调用点有十几处
（评测/验证器生成/裁决/修复/多轮 agent），漏一处那条的成本就悄悄丢进空桶里，
而且这种漏**不会报错**。反过来，用 ContextVar 在"每次处理一条"的入口处设一次，
所有下游调用（含将来新增的）自动继承——漏的成本归因变成"显式没设"，比"显式忘了传"好查。

约定：不设就是空串（部署规划、批量定论这类跨条目调用本来就该没有归属）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: 当前正在处理的条目 key（形如 `github:owner/name#12`）。空串 = 没有归属。
current_item_key: ContextVar[str] = ContextVar("fissue_current_item_key", default="")


def item_key_now() -> str:
    """当前归属的条目 key；没有则为空串。"""
    return current_item_key.get()


@contextmanager
def attribute_to(item_key: str) -> Iterator[None]:
    """在这个上下文里产生的 LLM 用量都归到 ``item_key``。

    用 token 复位而不是 ``set("")``：嵌套调用（比如修复内部又调一次评测）
    退出后必须回到外层归属，而不是把外层也清掉。
    """
    token = current_item_key.set(item_key or "")
    try:
        yield
    finally:
        current_item_key.reset(token)
