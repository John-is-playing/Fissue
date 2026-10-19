# 快速开始

> 目标：从零到跑通「抓取 → 评测 → 沙盒验证 → 批量定论 → 自动修复」。
> 本页是浓缩版；每一步的完整参数与更多场景见 [CLI 参考](CLI-Reference.md) 与 [使用场景与配方](Workflows.md)。

---

## 1. 前置条件

| 项 | 要求 | 检查命令 | 缺失时 |
|---|---|---|---|
| Python | ≥ 3.10 | `python --version` | 装 Python 3.10+ |
| LLM API Key | **必需**（`eval / verify / fix`） | — | 见第 3 节 |
| Docker | 建议（沙盒验证需要隔离） | `docker info` | 可降级本地执行，但**无隔离** |
| PostgreSQL | 可选 | `psql --version` | 本地试跑可用 SQLite |

> 单测（`python -m pytest -q`）**完全离线**，不需要 LLM Key，也不需要 Docker。

---

## 2. 安装

```bash
python -m venv .venv

# Windows（Git Bash / PowerShell 视 shell 而定）
source .venv/Scripts/activate
# Linux / macOS
source .venv/bin/activate

pip install -e ".[dev]"      # 需要跑测试则带 dev 组
fissue --help
```

`fissue --help` 应列出命令与命令组：`fetch eval verify flush fix report export status serve web db sandbox bench onboard requeue auth`。

---

## 3. 准备配置

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

**最少只填一项**即可跑通全流程：

```bash
# .env
FISSUE_LLM_API_KEY=sk-你的真实key
```

其余键的完整清单见[配置参考](Configuration.md)。三项常用替换：

```bash
# 换模型供应商（只改 BASE_URL + MODEL）
FISSUE_LLM_BASE_URL=https://api.openai.com/v1
FISSUE_LLM_MODEL=gpt-4o-mini

# 数据库：本地试跑用 SQLite（零依赖）
FISSUE_DATABASE_URL=sqlite:///E:/Fissue/data/fissue.db

# 平台 Token（公开仓库可留空只读抓取；自动提 PR 必须配写权限 Token）
FISSUE_GITHUB_TOKEN=ghp_xxx
```

> **提示**：Web 工作台与 `fissue onboard` 改配置时**只写 `config.local.yaml` 覆盖层**，
> 所以人手写的 `config.yaml` 及其注释**一字不动**。生效优先级：
> 进程环境 > `.env` > 覆盖层 > `config.yaml` > 内置默认值。

`config.yaml` 里**必改**的是登记仓库：

```yaml
repos:
  - platform: github          # github | gitee | atomgit | gitlab
    owner: psf
    name: requests
    base_branch: main         # 留空则自动探测
    enabled: true
    collect: [issue, pr]
    labels_exclude: [wontfix, duplicate]
    since_days: 30            # 只抓最近 30 天；0 = 不限
    test_hint: null           # 留空则由 AI 读仓库自行判断测试方式
```

---

## 4. 准备沙盒镜像

沙盒镜像预装了 Python / Node / Go / Java / Ruby 的测试框架，避免每次验证都要联网装依赖（容器默认**禁网**）。

```bash
docker build -t fissue/sandbox-base:latest docker/sandbox
fissue sandbox check
```

期望输出：

```
Docker：Docker 26.x
执行后端：inline（进程内调用）
镜像：fissue/sandbox-base:latest
隔离：网络=none｜只读根=True｜用户=1000:1000｜CPU=2.0｜内存=2048MB｜超时=600s
```

> 不想现在构建也能跑（`sandbox.runtime: local` 降级为本地子进程），但**完全没有容器隔离**，不可信代码有风险，仅限本机开发。
> 生产 Linux 部署建议另起转发组件：`fissue sandbox serve`（Windows 不支持 Unix socket，会自动走 inline）。

---

## 5. 初始化数据库

```bash
fissue db init        # 建表（幂等）
fissue db check       # 检查连通性
```

重置（危险，清空所有数据）：`fissue db init --drop`

---

## 6. 核心流程：五步走

**建议先在单仓库、小批量上验证效果，再放开。** 每一步都可用 `-v` 看调试日志。

### 步骤 1 — 抓取（fetch）

```bash
fissue fetch --repo psf/requests --limit 50   # 单仓库（不写 --platform 默认 github）
fissue fetch                                   # 抓 config.yaml 里登记的所有仓库
fissue fetch --repo gitee:oschina/git-osc --platform gitee
fissue fetch --full                            # 忽略增量游标，全量重抓
```

自动做三件事：**增量**（`since_days` 控制窗口，首次全量）、**去重**（靠 `content_hash`）、**过滤**（`labels_include/exclude`）。

### 步骤 2 — 评测（eval）

```bash
fissue eval                                    # 评测状态为 new 的
fissue eval --all --limit 100                  # 评测所有未评测的
fissue eval --repo psf/requests --all
fissue eval --all --deep                       # PR 额外拉 diff 评代码质量（更贵更准）
fissue eval --all --json > /tmp/evals.json
```

产出：分类（BUG/FEATURE）、**四维评分**（各 0-100 + 理由 + 引用证据）、难度评估、反刷子判定、优先级（tier1/tier2/none）。

> **FEATURE 类到这里就结束了**——只打标签等开发者，不生成验证器、不自动修复。

### 步骤 3 — 验证（verify）

```bash
fissue verify --repo psf/requests --limit 10
fissue verify --key github:psf/requests#6712   # 只验证一条
fissue verify --repo psf/requests --json
```

关键判据是 **F2P**：

| 阶段 | 代码状态 | 期望结果 | 含义 |
|---|---|---|---|
| base | 未修复 | **必须失败** | 证明问题真实存在、可复现 |
| fix | 修复/合并后 | **必须通过** | 证明修复有效 |

三种失败处置（**都不会**进入自动修复）：base 就通过 → 让 AI 修验证器（最多 `verifier.max_rounds` 轮）→ 仍不行标 `needs_manual`；base 报错/超时 → 标 `needs_manual`；仓库无测试框架 → 降级 checklist。细节见[验证器与 F2P](Verifier-and-F2P.md)。

### 步骤 4 — 批量定论（flush）

三种自动触发（无需干预）：数量阈值（`flush_size`，默认 10）/ 空闲超时（`idle_flush_seconds`，默认 300 秒）/ 队列排空。也可手动：

```bash
fissue flush                  # 强制 flush 全部队列
fissue flush --queue verify
fissue flush --auto           # 按触发条件判断（不强制），常用于 cron
```

flush 的产物：**打标签**到平台（`reproduced` / `ai-verified` / `needs-review` / `fissue` 等）；并把结论为修且 tier1/tier2 的 Issue 状态置 `fix_queued`。

### 步骤 5 — 自动修复（fix）

```bash
fissue fix --plan --dry-run --limit 5    # 先试跑：只产补丁与报告，不建分支不提 PR
fissue fix --plan --limit 3              # 确认质量后真正提 PR
fissue fix --key github:psf/requests#6712
fissue fix --limit 3 --rounds 8          # 限制 Agent 轮次，省钱
```

修复走 **Agent 循环**（不是一次性吐 patch）。成功路径：Agent 自测通过 → **再跑一次完整 F2P 复核** → fork → 建分支 → 推 → 提 PR → 打 AI 标签。
失败路径（`on_failure: report_manual`）：**绝不硬提 PR**，转生成人工报告 `data/manual/<key>/report.md`。

> 首次上线建议先 `pr_strategy.mode: patch_only`（或命令行 `--dry-run`），观察几轮补丁质量后再放开 `mode: fork`。

---

## 7. 起服务 / 看板

```bash
fissue serve                 # 常驻：增量扫描 + 队列调度 + 阈值通知（也含验证）
fissue serve --with-web      # 同进程带起 Web/API
fissue serve --once          # 只跑一轮就退出（适合交给系统 cron）
fissue web                   # Web 工作台 + REST API（默认 127.0.0.1:8000）
```

---

## 8. 十分钟首次试跑（不写任何东西到平台）

```bash
fissue fetch --repo psf/requests --limit 20
fissue eval --limit 20
fissue status
fissue report --repo psf/requests --out data/exports/first.md
```

这一步只会读平台数据、写本地库，**不会**在平台上打标签或提 PR。

---

## 相关页

[配置参考](Configuration.md) · [CLI 参考](CLI-Reference.md) · [使用场景与配方](Workflows.md) · [故障排查](Troubleshooting.md) · [部署与运维](Deployment.md)
