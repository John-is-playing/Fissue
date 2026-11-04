#!/usr/bin/env python3
"""容器健康检查：容器内的 Web/API 到底有没有在应答。

为什么用 Python 而不是 `curl`：本镜像基于 python:3.12-slim，装 curl 只为一次探活不值得
（多一个二进制、多一份 CVE 面），而 Python 本来就在。

探的端点：
  * `/api/v1/health` —— 公开端点（不经过鉴权依赖），返回 `{"status": "ok", "version": ...}`；
  * 退路 `/login` —— 万一有人把 `api.enabled` 关了，健康检查不该跟着报死。
    401/403 也算「活着」：那说明服务在，只是要登录。

退出码即结论：0 = 健康，1 = 不健康（Docker 记为 unhealthy，`restart: unless-stopped`
不会因此重启容器，但 `docker compose ps` 与编排工具能看见）。
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 4.0
PATHS = ("/api/v1/health", "/login")


def main() -> int:
    port = os.environ.get("FISSUE_WEB_PORT", "8000")
    base = f"http://127.0.0.1:{port}"
    for path in PATHS:
        try:
            with urllib.request.urlopen(base + path, timeout=TIMEOUT_SECONDS) as resp:
                if resp.status < 400:
                    return 0
        except urllib.error.HTTPError as exc:
            # 服务活着，只是这个端点要身份（或没注册）：换下一个退路。
            if exc.code in (401, 403):
                return 0
        except OSError:
            # 连不上 / 超时：换下一个退路；都不行才算不健康。
            continue
    print(f"healthcheck: {base} 无应答（试过 {'、'.join(PATHS)}）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
