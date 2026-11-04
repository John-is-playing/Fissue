# Fissue 服务端镜像：把 fissue 装进镜像，用 `docker compose up -d` 一条命令起服务
# （面向「不想在本机装 Python 环境」的用户；说明见 docs/COMPOSE.md）。
#
# 三份 Dockerfile 的分工（别合并，需求正好相反）：
#   docker/sandbox/Dockerfile  沙盒执行环境：预装 python/node/go/java 测试栈，
#                              运行时禁网跑不可信代码，**不含 fissue**
#   docker/self/Dockerfile     「Fissue 维护自己」用的沙盒镜像：在 sandbox-base 上再装
#                              本仓库依赖，同样**刻意不装 fissue**（要 import 挂载进来的源码）
#   docker/runtime/Dockerfile  ← 这份：服务端。要小、要装好 fissue、不依赖任何挂载源码
#
# 构建（compose 会自动做）：
#   docker build -f docker/runtime/Dockerfile -t fissue/fissue:local .
# 沙盒 profile 用的变体（多一层 Docker CLI）：
#   docker build -f docker/runtime/Dockerfile --target sandbox -t fissue/fissue:local-sandbox .

# ---------------------------------------------------------------------------
# 国内镜像加速（可用 --build-arg 覆盖）。口径与 docker/sandbox/Dockerfile 一致：
# 默认 deb.debian.org / pypi.org 在国内极慢，换成下面这些可在数十秒内完成。
# ---------------------------------------------------------------------------
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
ARG PYTHON_VERSION=3.12

# ===========================================================================
# runtime：服务本体（默认 profile）
# ===========================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG APT_MIRROR
ARG PIP_INDEX

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=UTC

# git 是硬依赖：抓取到的条目要 clone 到工作区才能验证/修复（见 workspace.py）。
# ca-certificates 供 httpx / git 走 HTTPS（pip 装出来的镜像并不保证有）。
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再装本体：依赖清单变了才重装（pyproject 与 src 分开 COPY）。
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -i "${PIP_INDEX}" \
        $(python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; print(' '.join(p['dependencies']))")

COPY src ./src
# 装本体（非 -e）：镜像要能脱离源码目录自洽运行，不像沙盒那样 import 挂载进来的工作区。
RUN pip install --no-cache-dir -i "${PIP_INDEX}" --no-deps .

# ---------------------------------------------------------------------------
# 容器内的路径约定
#
# /data 是**唯一**的持久化位置（compose 里挂具名卷）：SQLite、会话签名密钥、一次性引导
# 口令、补丁与沙盒产物、以及界面改配置时写下的 .env 与 config.local.yaml 全在这里。
#
# 三个 FISSUE_* 把这三件事钉死，否则界面上的「保存」会写进镜像层——容器一重建就没了：
#   FISSUE_CONFIG       /app/config.yaml       容器默认配置（web.host=0.0.0.0 等，见该文件）
#   FISSUE_CONFIG_LOCAL /data/config.local.yaml 覆盖层：WebUI / onboard 只写这一份
#   FISSUE_ENV          /data/.env              密钥：WebUI 的「设置」页写这一份
# ---------------------------------------------------------------------------
RUN mkdir -p /data /run/fissue-sandbox \
    && groupadd -g 1000 fissue \
    && useradd -m -u 1000 -g 1000 -s /bin/bash fissue \
    && chown 1000:1000 /data /run/fissue-sandbox

ENV FISSUE_CONFIG=/app/config.yaml \
    FISSUE_CONFIG_LOCAL=/data/config.local.yaml \
    FISSUE_ENV=/data/.env

COPY docker/runtime/container.yaml /app/config.yaml
COPY docker/runtime/entrypoint.py /app/entrypoint.py
COPY docker/runtime/healthcheck.py /app/healthcheck.py

EXPOSE 8000

# 刻意**不写** USER：入口需要 root 才能把 Docker 自动创建的宿主 bind 目录（属主 root）
# 修成运行用户，随后立刻 setuid 降到 1000 再 exec 服务本体（见 entrypoint.py）。
# 这不是 `privileged`：没有额外 capability，服务本体（uvicorn / 常驻流水线 / 转发组件）
# 都以 1000 运行；只有 `docker exec` 进来的交互与 healthcheck 探针是镜像用户。
#
# healthcheck 写在 compose.yaml 里（compose 是唯一入口，免得两处口径打架）。
ENTRYPOINT ["python", "/app/entrypoint.py"]

# ===========================================================================
# sandbox：给 compose 的 `profiles: ["sandbox"]` 用 —— 多一层 Docker CLI
#
# 转发组件（`fissue sandbox serve`）要用 docker 命令在**宿主** daemon 上起沙盒容器，
# 所以这份变体装了 CLI。它必须挂宿主的 /var/run/docker.sock，等于把宿主 Docker 的
# 控制权交给容器（能起特权容器、能挂任何宿主目录）——因此默认 profile 不用它。
# ===========================================================================
FROM runtime AS sandbox

ARG APT_MIRROR

USER root

# 用发行版打包的 docker.io（含 CLI）而不是 download.docker.com 的静态包：
# 前者走同一个 apt 镜像源，国内可用性更好。整包会带上 containerd 等用不到的东西
# （约 +200MB），只影响这一份可选变体。
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
