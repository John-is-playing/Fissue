# Fissue 沙盒基础镜像
#
# 用途：作为 fissue 沙盒执行（验证器 / 自动修复）的默认运行时环境。
# 特点：
#   * 预装 Python / Node / Go / Java / Rust 常用测试框架，避免每次执行都联网装依赖
#   * 非 root 用户（uid 1000），与 fissue 的 sandbox.user 默认值对齐
#   * 不含任何凭据；运行时由 fissue 以 --network=none 启动
#
# 构建：
#   docker build -t fissue/sandbox-base:latest docker/sandbox
#
# 注意：镜像内**不要**安装 git 凭据助手或写入 token。仓库代码由 fissue
# 在宿主侧克隆后，以隔离目录挂载进容器。

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=UTC

# ---------------------------------------------------------------------------
# 系统依赖：构建工具 + 常用测试运行时
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git make build-essential \
        libpq-dev libffi-dev libssl-dev \
        nodejs npm \
        golang-go \
        default-jdk-headless maven \
        ruby-full \
        ripgrep jq \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python 测试栈（项目自带依赖仍由项目自己声明，这里只装通用工具）
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir \
        pytest pytest-asyncio pytest-cov pytest-xdist \
        tox nox \
        coverage

# ---------------------------------------------------------------------------
# Node 测试栈
# ---------------------------------------------------------------------------
RUN npm install -g --no-fund --no-audit \
        jest mocha vitest typescript ts-node \
    && npm cache clean --force

# ---------------------------------------------------------------------------
# 非 root 用户（与 fissue sandbox.user 默认 1000:1000 对齐）
# ---------------------------------------------------------------------------
RUN groupadd -g 1000 sandbox \
    && useradd -m -u 1000 -g 1000 -s /bin/bash sandbox \
    && mkdir -p /workspace \
    && chown -R 1000:1000 /workspace

USER 1000:1000
WORKDIR /workspace

# 说明：不在镜像里放 ENTRYPOINT，命令由 fissue 通过 `sh -lc "<command>"` 指定。
CMD ["sh", "-lc", "echo 'fissue sandbox ready'"]
