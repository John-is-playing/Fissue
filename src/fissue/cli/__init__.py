"""命令行入口（Typer）。

``main`` 定义 app 与高频命令；导入 ``ops`` 时会把其余命令注册到同一个 app 上。
"""

from . import (
    doctor,  # noqa: F401,E402  —— 注册 platforms doctor（平台能力体检）
    onboard,  # noqa: F401,E402  —— 注册 onboard（初始化向导）
    ops,  # noqa: F401,E402  —— 注册 report/export/fix/serve/web/status/sandbox
)
from .main import app  # noqa: F401  —— 供 ``fissue.cli.main:app`` 引用

__all__ = ["app"]
