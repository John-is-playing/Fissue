"""命令行入口（Typer）。

``main`` 定义 app 与高频命令；导入 ``ops`` 时会把其余命令注册到同一个 app 上。
"""

from .main import app  # noqa: F401  —— 供 ``fissue.cli.main:app`` 引用
from . import ops  # noqa: F401,E402  —— 注册 report/export/fix/serve/web/status/sandbox

__all__ = ["app"]
