"""日志配置。

用标准库 logging + rich（若可用）输出彩色日志；支持 FISSUE_LOG_LEVEL 环境变量覆盖。

**日志一律写 stderr**：stdout 留给机器可读输出（`fissue status --json`、`export` 之类）。
先前 `RichHandler()` 的默认 console 是 **stdout**（回退分支的 `StreamHandler` 才是 stderr），
于是任何一条 WARNING 都能把 `--json` 的输出打坏——实测 `sandbox.runtime: local` 时
`fissue status --json` 直接 `JSONDecodeError`。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

_CONFIGURED = False

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _resolve_level(level: str | int | None) -> int:
    if level is None:
        level = os.getenv("FISSUE_LOG_LEVEL", "info")
    if isinstance(level, int):
        return level
    return _LEVELS.get(str(level).lower(), logging.INFO)


def setup_logging(level: str | int | None = None, *, force: bool = False) -> None:
    """初始化根日志器（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    resolved = _resolve_level(level)
    handler: logging.Handler
    try:  # rich 是可选依赖，装了就享受彩色输出
        from rich.console import Console
        from rich.logging import RichHandler

        handler = RichHandler(
            # 必须显式指定 stderr：RichHandler 默认自建 Console()，那个落在 **stdout** 上，
            # 会把 `--json` 之类的机器可读输出打坏（见模块说明）。
            console=Console(stderr=True),
            rich_tracebacks=True,
            show_path=False,
            markup=False,
            log_time_format="%H:%M:%S",
        )
        fmt = "%(message)s"
    except Exception:  # pragma: no cover - 无 rich 时降级
        handler = logging.StreamHandler(sys.stderr)
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    root = logging.getLogger()
    root.handlers.clear()
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(resolved)

    # 降噪：第三方库日志压到 WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "apscheduler"):
        logging.getLogger(noisy).setLevel(max(resolved, logging.WARNING))

    _CONFIGURED = True


def get_logger(name: str, **ctx: Any) -> logging.LoggerAdapter:
    """获取带上下文标签的 logger。"""
    setup_logging()
    logger = logging.getLogger(name)
    return logging.LoggerAdapter(logger, ctx)
