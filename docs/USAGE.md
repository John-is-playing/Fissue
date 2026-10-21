# Fissue 使用手册

> 从零到跑通：安装 → 配置 → 抓取 → 评测 → 沙盒验证 → 自动修复。
> 每条命令都在本机实测通过（Python 3.14 + Docker 26.0.0 + Git Bash / Windows）。

---

## 目录

1. [前置条件](#1-前置条件)
2. [安装](#2-安装)
3. [配置密钥（.env）](#3-配置密钥env)
4. [配置业务规则（config.yaml）](#4-配置业务规则configyaml)
5. [准备沙盒镜像](#5-准备沙盒镜像)
6. [初始化数据库](#6-初始化数据库)
7. [核心流程：五步走](#7-核心流程五步走)
8. [四种交付形态](#8-四种交付形态)
9. [典型场景](#9-典型场景)
10. [成本控制](#10-成本控制)
11. [故障排查](#11-故障排查)

---

## 1. 前置条件

| 项 | 要求 | 检查命令 | 缺失时 |
|---|---|---|---|
| Python | ≥ 3.10 | `python --version` | 装 Python 3.11+ |
| LLM API Key | **必需** | — | 见第 3 节 |
| Docker | 建议（沙盒验证需要隔离） | `docker info` | 可降级本地执行，但**无隔离** |
| PostgreSQL | 可选（生产 / 多机推荐；默认用 SQLite） | `psql --version` | 不装也能跑——默认走 SQLite |

```bash
python --version     # 期望 Python 3.10+
docker info          # 期望打印 Server Version
```

---

## 2. 安装

```bash
cd E:/Fissue

# 1) 建虚拟环境（推荐）
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# source .venv/bin/activate        # Linux / macOS

# 2) 安装本体 + 开发依赖（测试需要 dev 组）
pip install -e ".[dev]"

# 3) 验证安装
fissue --help
```

`fissue --help` 应列出 10 个命令与 3 个命令组：

```
fetch   eval   verify   flush   fix    report
export  status serve    web     db     sandbox   bench
```

---

## 3. 配置密钥（.env）

```bash
cp .env.example .env
```

打开 `.env`，**最少只填一项**即可跑通全流程：

```bash
FISSUE_LLM_API_KEY=sk-你的真实key
```

### 3.1 换模型供应商

Fissue 只说 OpenAI 兼容协议（`/chat/completions`），换供应商只改两个值：

| 供应商 | `FISSUE_LLM_BASE_URL` | `FISSUE_LLM_MODEL` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:14b` |

也可以只写在 `config.yaml` 的 `llm` 段（env 优先级更高，便于临时覆盖）。

### 3.2 数据库

```bash
# 默认就是 SQLite（零依赖，文件落在 ./data/fissue.db），通常什么都不用配
FISSUE_DATABASE_URL=sqlite:///./data/fissue.db

# 生产 / 多机换 PostgreSQL（SQLite 并发写会锁表）
FISSUE_DATABASE_URL=postgresql+psycopg://fissue:fissue@localhost:5432/fissue
```

> 换 PostgreSQL 时需先建库：`createdb -U fissue fissue`

### 3.3 平台 Token

**公开仓库可留空**即可只读抓取；要做自动修复（提 PR）则必须配。

```bash
FISSUE_GITHUB_TOKEN=ghp_xxx        # settings → Developer settings → Tokens
FISSUE_GITEE_TOKEN=xxx             # 设置 → 私人令牌
FISSUE_ATOMGIT_TOKEN=xxx           # 设置 → 访问令牌（classic）
FISSUE_GITLAB_TOKEN=glpat-xxx      # Preferences → Access Tokens
```

Token 权限最小集：`repo`（GitHub / Gitee / AtomGit）/ `api`（GitLab）。
自动修复需要写权限（能建分支、提 PR）。

### 3.4 Web/API

```bash
FISSUE_WEB_HOST=127.0.0.1
FISSUE_WEB_PORT=8000
FISSUE_API_TOKEN=            # 留空=不鉴权（仅本地）；填了则自动开启鉴权
```

> ⚠️ **挂了反向代理就必须填 `FISSUE_API_TOKEN`**。启动检查只能看**绑定地址**，看不见代理——
> 「绑 `127.0.0.1` + 同机 nginx 转出去」同样等于把无鉴权的写控制面
> （`/api/v1/queues/flush`、`/api/v1/actions/*`）对外开放。未开鉴权时，带转发头或来自
> 非回环地址的写请求会被 403，但那只是补刀：**边界始终是令牌**。

---

## 4. 配置业务规则（config.yaml）

```bash
cp config.example.yaml config.yaml
```

配置分四层，通常只需改前两层：

### 4.1 登记仓库（必改）

```yaml
repos:
  - platform: github          # github | gitee | atomgit | gitlab
    owner: psf
    name: requests
    base_branch: main         # 留空则自动探测
    enabled: true
    collect: [issue, pr]      # 抓哪些
    labels_exclude: [wontfix, duplicate]
    since_days: 30            # 只抓最近 30 天；0 = 不限
    test_hint: null           # 留空则由 AI 读仓库自行判断测试方式
```

**多仓库**：继续往下加条目即可。首期建议先只留一条跑通。

**GitLab 自建实例**：额外加 `api_base: https://gitlab.example.com/api/v4`

### 4.2 沙盒（强烈建议确认）

```yaml
sandbox:
  enabled: true
  runtime: docker             # docker | local（local 无隔离，仅本机开发）；拼错会被拒绝启动
  image: fissue/sandbox-base:latest
  network: none               # 禁网，最安全
  read_only_root: true
  git_readonly: true          # 工作区里的 .git 对容器只读（index 除外）
  user: "1000:1000"           # 非 root
  limits:
    cpus: 2.0
    memory_mb: 2048
    timeout_seconds: 600
```

> `git_readonly` 掐掉的是**宿主的执行入口**（`.git/hooks`、`config` 里的 `filter.*.clean`、
> `diff.*.textconv`、`credential.helper`）与**伪造入口**（`.git/objects`、`refs`）。`.git/index`
> 特意留成可写——不少仓库的测试套件会顺手跑 `git status`，整块只读会让它们整片失败。只有
> 「测试套件要在容器里 `git commit`」的仓库才需要关掉它。

### 4.3 自动修复（默认已开，上线前务必确认）

```yaml
fix_policy:                   # 谁能被自动修（Q1 规则）
  tier1: { max_difficulty: 40, min_importance: 70 }   # 难度低 + 重要性高 → 修
  tier2: { max_difficulty: 40, min_importance: 0 }    # 难度低 + 重要性低 → 也修
  only_issues: true           # 只修 Issue，PR 交人工

auto_fix:
  enabled: true
  agent_max_rounds: 12        # Agent 循环上限
  max_changed_files: 20       # 护栏：改动文件数上限
  max_diff_lines: 800         # 护栏：diff 行数上限
  on_failure: report_manual   # 失败→生成需人工报告（绝不硬提 PR）
  pr_strategy:
    mode: fork                # fork | direct | patch_only
    label: ai-generated       # 自动打的 AI 标识标签
```

> **首次使用建议**：先 `mode: patch_only`（或命令行加 `--dry-run`），只产出补丁供人工审阅，
> 观察几轮任务质量后再放开自动提 PR。

### 4.4 阈值与队列（默认合理，可按需调）

```yaml
evaluation:
  thresholds:
    importance_high: 70       # 重要性 ≥ 70 视为高
    difficulty_low: 40        # 难度 ≤ 40 视为低
    alert_importance: 80      # ≥ 80 触发告警推送

queues:
  verify_queue: { flush_size: 10, idle_flush_seconds: 300 }
  fix_queue:    { flush_size: 10, idle_flush_seconds: 300 }
```

### 4.5 既有测试回归门（防止「修好一个、弄坏一片」）

F2P（fail-to-pass）只证明**目标用例**被修好了；证明不了「没弄坏别的」。
典型反例：把 `split(" ")` 改成 `split()`，空串用例确实修对，却改掉了连续
空格的既有语义。回归门用**仓库自己的测试套件**补上这个 pass-to-pass 方向。

```yaml
verifier:
  # off / warn / strict
  regression_gate: strict             # 默认档：base 绿而 fix 红 → 拒绝该修复
  regression_command: null            # 留空 → 仓库 test_hint → 自动探测
  regression_timeout_seconds: 900     # 独立预算，全量套件通常比单测慢很多
```

三档语义：

| 档位 | 行为 | 适用 |
|---|---|---|
| `off` | 完全不跑，零开销 | 不关心回归 / 仓库没测试 |
| `warn` | 跑、记录日志、报告里标注，但**不改变**结论 | 想先观察「本就红」的比例时 |
| `strict`（默认） | base 绿而 fix 不绿 → **拒绝**修复并转人工 | 默认档 |

**推荐姿势**：保持默认的 `strict`——它只在「改之前绿、改之后红」时拦人，而
「base 本来就不绿」的情形已被安全底线放行。想统计有多少仓库本来就红，可临时
切到 `warn` 观察几天。

> ⚠️ 若 base 阶段既有测试就不通过（老仓库普遍如此），回归门一律视为
> **不可信并放行**——绝不据此拒绝，否则会大面积误杀。同理，沙盒禁网导致
> 依赖装不全、或套件超时（`regression_timeout_seconds` 到点即止），也不阻断。

命令来源优先级（**不由 LLM 现编**，否则就不是「既有测试」了）：

```
verifier.regression_command  →  repo.test_hint  →  探测
（pyproject.toml/pytest.ini → pytest；package.json → npm test；go.mod → go test…）
```

探不到就跳过本门并记 `skipped`，不影响流程。

### 4.6 通知（可选）

```yaml
notify:
  enabled: true
  channels:
    - { type: console,  enabled: true }                              # 打印到终端
    - { type: webhook,  enabled: true, url: "https://...", secret: "" }
    - { type: wecom,    enabled: false, url: "企业微信机器人Webhook" }
    - { type: dingtalk, enabled: false, url: "钉钉Webhook", secret: "加签密钥" }
    - { type: feishu,   enabled: false, url: "飞书Webhook" }
  events: [scan_done, eval_done, verify_done, fix_done, pr_created, needs_manual, budget_exceeded]
```

---

## 5. 准备沙盒镜像

沙盒镜像预装了 Python / Node / Go / Java / Rust 的测试框架，
避免每次验证都要联网装依赖（而容器默认是**禁网**的）。

```bash
docker build -t fissue/sandbox-base:latest docker/sandbox
```

首次构建约 3-8 分钟（取决于网速）。构建完检查：

```bash
fissue sandbox check
```

期望输出：

```
Docker：✅ Docker 26.0.0
执行后端：inline（进程内调用）
镜像：fissue/sandbox-base:latest
隔离：网络=none｜只读根=True｜用户=1000:1000｜CPU=2.0｜内存=2048MB｜超时=600s
```

> **不想现在构建？** 也能跑：`sandbox.runtime: local` 会降级为本地子进程执行，
> 但**完全没有容器隔离**，不可信代码有风险。仅限本机开发。

> **生产部署（Linux）**：另起一个转发组件常驻进程，让沙盒执行走受控通道：
> ```bash
> fissue sandbox serve        # 监听 /var/run/fissue-runner.sock
> ```
> Windows 不支持 Unix socket，会自动走 inline 模式。

---

## 6. 初始化数据库

```bash
fissue db init        # 建表（幂等，可重复执行）
fissue db check       # 检查连通性
```

期望：

```
✅ 数据库已就绪：sqlite:///./data/fissue.db
```

重置（危险，清空所有数据）：

```bash
fissue db init --drop
```

---

## 7. 核心流程：五步走

这是 Fissue 的主线。**前四步建议先在单仓库、小批量上验证效果**。

### 步骤 1 — 抓取（fetch）

```bash
# 单仓库（不写 --platform 时默认 github）
fissue fetch --repo psf/requests --limit 50

# 或从 config.yaml 里登记的所有仓库抓
fissue fetch

# 显式指定其它平台
fissue fetch --repo gitee:oschina/git-osc --platform gitee
fissue fetch --repo atomgit:openharmony/arkcompiler --platform atomgit
fissue fetch --repo gitlab:gitlab-org/gitlab --platform gitlab

# 全量重抓（忽略增量游标）
fissue fetch --full
```

期望输出：

```
✅ 抓取完成：拉取 47，新增 32，更新 3，未变 12
```

抓取会自动做三件事：

- **增量**：只拉最近有更新的条目（`since_days` 控制窗口），首次全量
- **去重**：靠内容指纹 `content_hash`，标题/正文/标签/评论数没变就跳过
- **过滤**：按 `config.yaml` 的 `labels_include` / `labels_exclude` 筛掉不关心的

> 不需要 GitHub Token 也能抓公开仓库；未鉴权时限流较紧（每小时 60 次），
> 建议配置 Token 提升到 5000 次/小时。

### 步骤 2 — 评测（eval）

```bash
# 评测新条目（状态为 new 的）
fissue eval

# 评测所有未评测的（含历史）
fissue eval --all --limit 100

# 只评测指定仓库
fissue eval --repo psf/requests --all

# PR 深度评审（额外拉 diff 评代码质量，更贵更准）
fissue eval --all --deep

# 输出 JSON，方便接自己的脚本
fissue eval --all --json > /tmp/evals.json
```

期望输出（表格）：

```
评测结果
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┓
┃ key                     ┃ category┃ authenticity┃ importance┃ difficulty┃ priority┃ action┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━┩
│ github:psf/requests#6712│ bug     │ 92          │ 88       │ 25        │ tier1   │ fix_now│
│ github:psf/requests#6713│ feature │ 85          │ 40       │ 70        │ none    │ backlog│
└─────────────────────────┴─────────┴─────────────┴──────────┴───────────┴─────────┴───────┘
```

这一步做的事：

- **分类**：BUG 还是 FEATURE（关键词预筛 + AI 兜底，决定走哪条流水线）
- **四维评分**：真实性 / 重要性 / 可行性 / PR 质量（各 0-100 + 理由 + 引用证据）
- **难度评估**：0-100，越低越好修
- **反刷子**：识别误报、重复提交、AI 灌水
- **优先级**：按 `fix_policy` 定 tier1 / tier2 / none

> **FEATURE 类到这里就结束了**——只打标签等开发者，不生成验证器、不自动修复。

### 步骤 3 — 验证（verify）

```bash
# 对已评测的 BUG 条目生成验证器，并在沙盒里跑 base 阶段
fissue verify --repo psf/requests --limit 10

# 只验证一条
fissue verify --key github:psf/requests#6712

# JSON 输出
fissue verify --repo psf/requests --json
```

期望输出：

```
沙盒：Docker 26.0.0
验证结果
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━┓
┃ key                     ┃ type ┃ verifier  ┃ reproduced ┃ f2p  ┃ queued ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━┩
│ github:psf/requests#6712│ issue│ executable│ True       │ False│ verify │
└─────────────────────────┴──────┴───────────┴────────────┴──────┴────────┘
```

这里的关键是 **F2P（fail-to-pass）**：

| 阶段 | 代码状态 | 期望结果 | 含义 |
|---|---|---|---|
| base | 未修复 | **必须失败** | 证明问题真实存在、可复现 |
| fix | 修复/合并后 | **必须通过** | 证明修复有效 |

三种失败处置（都**不会**进入自动修复）：

- **base 就通过** → 验证器不可靠 → 自动让 AI 修正（最多 3 轮）→ 仍不行则标 `needs_manual`
- **base 报错/超时** → 结论不可信 → 标 `needs_manual`
- **仓库无测试框架** → 降级为自然语言清单 → LLM 按证据逐条判定
  （文件树 / README / API 契约 + 清单引用的文件内容；明细落验证记录）→
  一般标 `needs_manual` 待人工复核；**强制修复的条目**不会被「无法自动判定」
  挡住：直落待修队列，修复完成后 LLM 在修复后的工作区上逐条重判（全部满足
  → 提 PR；未全部满足 → 带缺项反馈返修，余额用尽转 `needs_manual`）

验证通过的条目进入 **verify 队列**，等批量 flush。

### 步骤 4 — 批量定论（flush）

队列有三种触发方式（自动的，无需你干预）：

1. **数量阈值**：攒够 `flush_size`（默认 10 条）
2. **空闲超时**：队首静默超过 `idle_flush_seconds`（默认 300 秒）
3. **队列排空**：没有待处理条目了

也可以手动立刻触发：

```bash
# 强制 flush 全部队列
fissue flush

# 只 flush 验证队列
fissue flush --queue verify

# 按触发条件判断（不强制），常用于 cron
fissue flush --auto
```

期望输出：

```
[verify] 已达数量阈值（10/10）
✅ [verify] 已 flush 10 条，结论 10 条
```

flush 的产物：

- **打标签**到平台上（`reproduced` / `ai-verified` / `needs-review` / `fissue` 等）
- **派发修复**：结论为 `fix` 且优先级 tier1/tier2 的 Issue → 状态置 `fix_queued`

> **为什么批量而不是逐条？** 同一批的验证输出放同一上下文，AI 能横向比较
> （比如识别出成批的重复提交），而且比逐条调用省钱。

### 步骤 5 — 自动修复（fix）

```bash
# 先试跑！只产出补丁与报告，不建分支、不提 PR
fissue fix --plan --dry-run --limit 5

# 确认补丁质量后，真正提 PR
fissue fix --plan --limit 3

# 只修一条
fissue fix --key github:psf/requests#6712

# 限制 Agent 循环轮次（省钱）
fissue fix --limit 3 --rounds 8
```

期望输出：

```
[psf/requests] 已规划优先级 12 条
修复尝试 3，成功 2，失败 1，产出 PR 2
自动修复结果
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┓
┃ key                     ┃ outcome ┃ rounds ┃ branch          ┃ pr       ┃ error ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━┩
│ github:psf/requests#6712│ success │ 4      │ fissue/fix-6712 │ .../pull/9│       │
│ github:psf/requests#6715│ needs   │ 12     │ fissue/fix-6715 │ -        │ ...   │
└─────────────────────────┴─────────┴────────┴─────────────────┴──────────┴───────┘
```

修复的工作方式（**Agent 循环**，不是一次性吐 patch）：

```
读文件 → 模型决定下一步 → 写文件 / 跑验证器 → 看结果 ─┐
  ▲                                                    │
  └────────── 未通过则继续（最多 N 轮）◄───────────────┘
```

成功路径：Agent 自测通过 → **再跑一次完整 F2P 复核** → fork → 建分支 → 推 → 提 PR → 打 AI 标签

失败路径（`on_failure: report_manual`）：**绝不硬提 PR**，转而生成人工报告：

```
data/manual/github_psf_requests_6712/
  report.md        # 失败原因 + Agent 动作轨迹 + 被拒写入 + 最后一次验证输出
  attempt.patch    # 尝试过的补丁（供人工参考）
  trace.json       # 结构化轨迹 + token 用量
```

**宿主侧强制护栏**（模型说了不算）：

| 护栏 | 说明 |
|---|---|
| 路径白名单 | 只允许仓库内相对路径，`..` 与绝对路径直接拒 |
| 保护路径 | `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` 拒写 |
| 验证器锁定 | 修复期间不可写验证器文件（防「改测试骗过测试」） |
| 规模上限 | 文件数 ≤ 20、diff ≤ 800 行，超出即回滚 |
| F2P 复核 | 提 PR 前重跑，不接受 Agent 自说自话 |
| PR 永不自动合并 | 只打标签给建议，合并由人在平台操作 |

---

## 8. 四种交付形态

### 8.1 CLI（上面的 7 节就是）

补充两个常用命令：

```bash
# 生成 Markdown 报告
fissue report --repo psf/requests --format markdown --out data/exports/report.md
fissue report --repo psf/requests --with-body          # 附带正文
fissue report --format json                            # JSON 格式

# 全量导出（含所有评测结论）
fissue export --format json                            # → data/exports/*.json
fissue export --format markdown --out data/exports/
fissue export --category bug --status evaluated        # 按条件过滤

# 查看当前状态（条目统计 / 队列 / 用量 / 沙盒）
fissue status
fissue status --json

# 跑分基线：冻结一次测量的前提与结果，日后逐项对比
fissue bench capture --label ratekit-v1        # → data/benchmarks/ratekit-v1.json
fissue bench list                             # 列出已冻结的基线
fissue bench diff 旧.json 新.json              # 逐项增量 + 是否可比
```

**为什么需要跑分基线**：`BENCHMARK.md` 里的数字来自「开发期累计运行」，
期间项目自身在改代码、模型输出又不确定，于是那些数字**回答不了**最基本的问题——
*这次改动让结果变好了还是变坏了？* `bench` 把「测量」变成可对比的快照：

| 记录 | 内容 |
|---|---|
| **前提** | git commit + 是否有未提交改动、模型与采样参数（temperature/max_tokens）、关键配置指纹（回归门档位、stall_rounds、tier 阈值…） |
| **结果** | 条目分布、各阶段 outcome 交叉表、F2P 成立数、修复结果（补丁/PR/轮次） |

```bash
# 典型用法：改动前后各冻结一次，再对比
fissue bench capture --repo John-is-playing/ratekit --label ratekit-before
#   … 改代码 / 调配置 …
fissue bench capture --repo John-is-playing/ratekit --label ratekit-after
fissue bench diff data/benchmarks/ratekit-before.json data/benchmarks/ratekit-after.json
```

`diff` 会**先判可比性**再给数字，三种情形显式点名：

```
✅ 可比：commit / 模型 / 工作区状态一致
⚠️  不可比——以下前提不同，结论不能当回归依据：
   - commit 不同（68d5bd49 → c3f1a2b0）
   - 存在未提交改动（新基线 38 个文件）
   - 模型不同（qwen3.6-plus → deepseek-chat）
```

退出码：`0` 可比且无变化｜`1` **不可比**（结论不能当回归依据）｜`2` 可比但有变化——
便于塞进 CI 当回归门。基线文件默认落在 `data/benchmarks/`，**是唯一进版本库的
`data/` 子目录**（其余数据仍在 `.gitignore` 内）。

### 8.2 常驻服务（serve）

```bash
# 常驻：增量扫描 + 队列调度 + 阈值通知
fissue serve

# 同时开 Web/API
fissue serve --with-web

# 只跑一轮就退出（适合交给系统 cron / 任务计划）
fissue serve --once
```

服务里有三个独立循环，互不阻塞：

| 循环 | 默认间隔 | 做什么 |
|---|---|---|
| 扫描 | 每小时 | 增量抓取 → 评测新条目 → **验证**（生成验证器 + F2P）→ 高重要性告警 |
| flush | 60 秒 | 按触发条件 flush 队列 → 打标签 → 派发修复 |
| 修复 | 120 秒 | 修 tier1/tier2 的 Issue，受预算与策略限制 |

定时与「一轮做多少活」都用 `config.yaml` 调：

```yaml
schedule:
  incremental: true
  scan_interval_seconds: 3600
  queue_flush_interval_seconds: 60
  full_rescan_days: 30         # 每 30 天做一次全量兜底
  verify_enabled: true         # false = 常驻服务不做验证，交给人工 `fissue verify`
  verify_batch_limit: 30       # 每轮最多验多少条（积压由后续轮次继续消化）
```

外部 cron 方案（不常驻）：

```bash
# Linux crontab：每小时扫一轮
0 * * * * cd /path/to/Fissue && fissue serve --once >> /var/log/fissue.log 2>&1
```

```powershell
# Windows 任务计划程序：actions 填
powershell -c "cd E:\Fissue; fissue serve --once"
```

### 8.3 Web 仪表盘 + REST API（web）

```bash
fissue web                                  # 默认 127.0.0.1:8000
fissue web --host 0.0.0.0 --port 8080
fissue web --reload                         # 开发模式热重载
```

打开 `http://127.0.0.1:8000` 即可看到仪表盘：列表、筛选（仓库/类型/分类/优先级/状态）、
点击条目看详情（评分理由、证据引用、验证器、执行记录、修复记录）。
页面上还有操作按钮：扫描、评测、验证、Flush、试跑修复。

长任务（评测 / 验证 / 修复 / flush / 强制修复）以「任务」运行：「任务」页有历史与日志，
每个任务另有「详情」页（`/jobs/<id>`），以事件流时间线展示——LLM 的**流式输出**
（JSON 结论按格式化代码块渲染，其余走 Markdown）、修复 Agent 的读文件 / 写文件 /
跑验证器等工具动作（带成败标记）、以及提 PR / 推送等平台动作。运行中每 1.5s 自动
刷新。`serve --with-web` 时，常驻服务的定时轮次也登记为 `round` 类型任务：任务列表
可见、详情页可看实时进度，也可以从页面上停掉这一轮；轮次进行中提交网页任务会直接
得到 409（而不是先受理、抢到写闸门才失败）。

对话式 API 文档：`http://127.0.0.1:8000/docs`

常用接口：

```bash
BASE=http://127.0.0.1:8000/api/v1

curl $BASE/health
curl $BASE/stats                                    # 概览统计
curl "$BASE/items?category=bug&priority=tier1"      # 列表 + 筛选
curl "$BASE/items/github%3Apsf%2Frequests%2342"     # 详情（key 要 URL 编码）
curl $BASE/queues                                   # 队列状态与 flush 判定
curl "$BASE/export?format=markdown"                 # 导出

# 操作类（web.read_only=false 时可用）
curl -X POST $BASE/actions/scan    -H 'Content-Type: application/json' -d '{"repo":"psf/requests"}'
curl -X POST $BASE/actions/evaluate -H 'Content-Type: application/json' -d '{"limit":20}'
curl -X POST $BASE/actions/verify   -H 'Content-Type: application/json' -d '{"limit":10}'
curl -X POST $BASE/queues/flush     -H 'Content-Type: application/json' -d '{"force":true}'
curl -X POST $BASE/actions/fix      -H 'Content-Type: application/json' -d '{"dry_run":true,"limit":5}'

# 人工闸门
curl -X POST "$BASE/actions/approve-fix?key=github%3Apsf%2Frequests%2342" -d '{}' -H 'Content-Type: application/json'
curl -X POST "$BASE/actions/approve-merge?key=github%3Apsf%2Frequests%2399" \
     -H 'Content-Type: application/json' -d '{"note":"已人工确认"}'
```

> `approve-merge` 只是打 `merge-approved` 标签 + 留评论，**不会真的合并**——
> 合并始终由你在平台上手动操作。

鉴权（对外暴露时必须开）：

```bash
# .env
FISSUE_API_TOKEN=your-long-random-token
```
```bash
curl $BASE/stats -H "Authorization: Bearer your-long-random-token"
```

只读模式：

```yaml
web:
  read_only: true        # 隐藏所有操作按钮，API 动作类返回 403
```

### 8.4 四种形态的组合

```bash
# 开发/试用：CLI 单仓库走一遍
fissue fetch -r psf/requests && fissue eval --all && fissue verify -r psf/requests

# 生产：常驻服务 + Web 一起
fissue serve --with-web
```

### 8.5 让平台推事件过来（入站 webhook）

常驻服务默认是**轮询**（`schedule.scan_interval_seconds`，默认 1 小时）——新 Issue/PR 最坏要等
一个间隔才被发现。接上 webhook 就变成**事件驱动**：平台一推，后台立刻跑一轮。

```yaml
# config.yaml
webhooks:
  enabled: true
```
```bash
# .env：每个已启用平台都必须配（密钥与平台 Token 是两回事，别复用）
FISSUE_WEBHOOK_GITHUB_SECRET=<仓库 Webhook 里填的那个 Secret>
```

然后在平台的仓库设置里加 webhook：

| 平台 | Payload URL | 订阅事件 | 密钥 |
|---|---|---|---|
| GitHub | `https://你的域名/api/v1/webhooks/github` | Issues、Pull requests | Secret（`X-Hub-Signature-256`，HMAC-SHA256） |
| GitLab | `…/api/v1/webhooks/gitlab` | Issue events、Merge request events | Secret token（`X-Gitlab-Token`） |
| Gitee / AtomGit | `…/api/v1/webhooks/gitee`（或 `…/atomgit`） | Issue、Pull Request | Webhook 密码（`X-Gitee-Token`） |

**它会怎么反应**：验签通过且仓库**已登记** → 回 `202`，并在**后台**跑一轮（抓取 → 评测 → 验证 →
flush → 修复）；这一轮**同样在写锁内**，已有写进程在跑时直接跳过（下一轮扫描会把它带上），所以
连着推几次不会堆叠。验签不过回 `403`；仓库没登记回 `202` 但 `accepted: false` 并写明原因；不是
条目类事件（如 GitHub 的 ping）同样只记一条日志。

> ⚠️ 三件最容易踩的：
> ① 这个端点**不带 bearer 鉴权**（平台不会带令牌），**密钥就是它的身份证明**——也别把 API token
>    拿来当 webhook 密钥；
> ② 它必须能被**平台服务器**访问到：只绑回环、或反代不放行 `/api/v1/webhooks/*`，推送永远到不了
>    （判断方法很直接：日志里一条 webhook 都没有）；
> ③ 开着 `webhooks.enabled` 却没给某个已启用平台配密钥，**启动就会失败**（fail-closed），
>    这是刻意的——免得端点对外裸奔。

---

## 9. 典型场景

### 9.1 首次试跑（10 分钟，不写任何东西到平台）

```bash
# 1) 只用公开仓库 + 不用 Token，最安全
fissue fetch --repo psf/requests --limit 20
fissue eval --limit 20
fissue status
fissue report --repo psf/requests --out data/exports/first.md
```

这一步只会读平台数据、写本地库，**不会**在平台上打标签或提 PR。

### 9.2 观察自动修复质量（推荐的上线姿势）

```yaml
# config.yaml
auto_fix:
  pr_strategy:
    mode: patch_only      # 只产出补丁，不建分支不提 PR
```

```bash
fissue fetch && fissue eval --all && fissue verify && fissue flush && fissue fix --plan
# 看 data/patches/*.patch 与 data/manual/*/report.md，人工评估 AI 修得怎么样
```

质量满意后再放开：

```yaml
auto_fix:
  pr_strategy:
    mode: fork            # fork → 分支 → 上游 PR
```

### 9.3 多仓库批量托管

```yaml
repos:
  - { platform: github,  owner: psf,      name: requests,  since_days: 14 }
  - { platform: github,  owner: pallets,  name: flask,     since_days: 14 }
  - { platform: gitee,   owner: mindspore, name: mindspore, since_days: 30 }
  - { platform: atomgit, owner: openharmony, name: arkcompiler, since_days: 30 }
  - { platform: gitlab,  owner: gitlab-org, name: gitlab,  since_days: 7, api_base: https://gitlab.com/api/v4 }
```

```bash
fissue fetch                       # 全部仓库增量抓取
fissue eval --all --concurrency 4  # 并发评测
fissue serve --with-web            # 交给常驻服务持续跑
```

### 9.4 只关心高重要性问题

```yaml
evaluation:
  thresholds:
    alert_importance: 80      # ≥80 才推送
notify:
  enabled: true
  channels:
    - { type: wecom, enabled: true, url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." }
  events: [high_importance, needs_manual, pr_created]
```

跑 `fissue serve`，只有真正紧急的才会推到你手机上。

### 9.5 交给 CI 定时跑（不适合常驻的场合）

```bash
# 每小时跑一轮，跑完退出
fissue serve --once

# 或分步更可控
fissue fetch --concurrency 1
fissue eval --all --limit 50
fissue verify --limit 20
fissue flush --auto
fissue fix --limit 3
```

GitHub Actions 示例：

```yaml
name: fissue
on:
  schedule: [{ cron: "0 * * * *" }]
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e .
      - run: fissue serve --once
        env:
          FISSUE_DATABASE_URL: ${{ secrets.FISSUE_DATABASE_URL }}
          FISSUE_LLM_API_KEY: ${{ secrets.FISSUE_LLM_API_KEY }}
          FISSUE_GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### 9.6 只跑某一条 Issue（快速验证效果）

```bash
KEY=github:psf/requests#6712

fissue eval   --key $KEY   2>/dev/null || fissue eval -r psf/requests --all
fissue verify --key $KEY
fissue flush
fissue fix    --key $KEY --dry-run
```

> 说明：`eval` 没有 `--key` 参数（它是批量语义），单条用 `--limit 1`，
> 或直接 `fissue verify --key` / `fissue fix --key`（这两个支持 `--key`）。

---

### 9.7 条目卡住了：`fissue requeue`

两种条目**只能人工放行**（此前没有入口）：

```bash
# 内容改过、结论已过期，但当时它的修复已开工（fix_queued / fixing / pr_created）
fissue requeue --stale --dry-run      # 先看计划，不改状态
fissue requeue --stale --yes          # 放回重评

# 状态为 failed 的条目（评测 / 验证 / 修复中途异常）
fissue requeue --failed --yes
```

`--failed` 的回退目标按**已有结论**决定：有评测 → `evaluated`（重跑验证这一步），
没有 → `new`（重跑评测）。默认先列计划再询问确认。

> 为什么 `--stale` 不自动重评？条目内容实质变化时它的修复可能已经开工，自动回落会给
> 「重复修复 / 重复建 PR」再开一道口子，所以那里只留痕（`RawItem.stale_reasons`，
> 导出与 API 都看得到），由你确认后放回。

### 9.8 模型说「不修」，但你就是想修：`fissue force`

评测会把条目拦在很多闸门后面：判成 FEATURE 直接打标签、疑似刷量不碰、优先级不够不修、
验证后裁决 skip……这些结论是模型给的，**你认不认是你自己的事**。强制修复就是「人拍板」
的入口——无论上述结论如何，把指定条目推进修复流程并**真提 PR**：

```bash
fissue force -k github:owner/repo#42 --dry-run   # 先看计划，不改状态
fissue force -k github:owner/repo#42 --yes       # 强制修复
fissue force -k github:owner/repo#42 --undo      # 取消强制
```

WebUI 里就是条目详情页的「**强制修复**」按钮（Issue 才有；PR 没有自动修复流程），
列表页可勾选多条「强制修复选中」。已强制的条目带 ⚡ 徽标，随时点「取消强制」收回。

强制时实际发生了什么：

1. 条目打上 `force_fix` 标记（人工置位，流水线只读它、不会自行清掉）；
2. 走一遍验证阶段——分类 / 刷量 / 可复现三道闸门对强制条目失效，
   没有验证器的会顺手生成一个（修复器靠它判定修复，**这道闸不旁路**）；
3. 条目直落「待修 tier1」，跳过定论队列与 LLM 裁决，由常驻服务 / `fissue fix` 接力提 PR。

两条边界：

- 已在修复流程中（fixing / pr_created / fix_queued）的条目强制时**保持原状**——
  把已提过 PR 的条目拉回来重验会多开一个 PR；
- 取消强制时，仍在待修队列的条目回退为「需人工」：它留在待修队列就没了依据
  （候选过滤只认状态+优先级），放着会被修复循环反复捡起空转。

---

## 10. 成本控制

### 10.1 各步骤的 token 消耗量级

| 步骤 | 单条消耗 | 说明 |
|---|---|---|
| `eval` | 中 | 一次调用出四维评分 |
| `eval --deep` | 中高 | 额外喂 PR diff 评代码质量 |
| `verify` | 中 | 生成验证器（不可靠时最多重试 3 轮） |
| `flush` | 低（分摊） | 一批一次，条数越多单条越便宜 |
| `fix` | 高 | Agent 循环，每轮都要带上下文 |

### 10.2 设置预算熔断

```yaml
llm:
  concurrency: 4                # 并发上限，别一次打太多
  budget:
    daily_tokens_per_repo: 2000000
    daily_usd_per_repo: 5.0
    hard_stop: true             # true=超限即停；false=仅告警
  pricing:                      # 用于成本估算，按你用的模型单价填
    input_per_1k: 0.00014
    output_per_1k: 0.00028
```

查看当日实际消耗：

```bash
fissue status
# 今日用量：183421 tokens（prompt 150000 / completion 33421），约 $0.0304，调用 47 次
```

### 10.3 省钱的六个开关

```yaml
# 1) 缩小抓取窗口
repos:
  - { platform: github, owner: psf, name: requests, since_days: 7 }

# 2) 关键词预筛（一边倒时不调 LLM）
classification:
  use_keywords_first: true

# 3) 不开 --deep，PR 不走 diff 深度评审
# 4) 验证器重试次数调小
verifier:
  max_rounds: 2

# 5) Agent 循环轮次调小
auto_fix:
  agent_max_rounds: 8

# 6) 只在必要时自动修复（收紧准入）
fix_policy:
  tier2: { max_difficulty: 20, min_importance: 0 }   # tier2 更严格
```

### 10.4 增量抓取天然省钱

内容指纹没变的条目会被跳过，**不会重复评测**。所以常驻服务长期跑的实际消耗
远低于首次全量。

---

## 11. 故障排查

### 11.1 快速自检

```bash
fissue db check          # 数据库通不通
fissue sandbox check     # Docker 与隔离参数
fissue status            # 条目/队列/用量/沙盒 全景
```

### 11.2 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `缺少 LLM API Key` | `.env` 里没填 `FISSUE_LLM_API_KEY` | 填上；或确认 `fissue` 是在项目根目录执行（能找到 `.env`） |
| `无法连接数据库` | 连接串错 / PostgreSQL 没起 | `fissue db check`；PG 用 `pg_isready` 确认 |
| `数据库不存在` | 没建库 | `createdb -U fissue fissue` 后 `fissue db init` |
| `401 / Bad credentials` | Token 过期或权限不足 | 重新生成 Token，检查 scope |
| `触发限流` | 未配 Token 或调用过密 | 配 Token；调小 `--concurrency` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 环境里有 HTTPS 中间人（企业网关 / 抓包工具）。**先看报错里的「当前生效的信任链」**：显示 `certifi` → 装 truststore；显示 `操作系统信任库（truststore）` → 说明根证书连系统信任库也没有，要导出它并设 `FISSUE_CA_BUNDLE`（写进 `.env` 即可） | 详见 [ENV.md §3.7](ENV.md#37-tls-证书fissue_ca_bundle--fissue_insecure_skip_verify) |
| `未找到 docker 命令` | 没装 Docker / 不在 PATH | 装 Docker Desktop；或 `runtime: local` 降级（无隔离） |
| `镜像不可用` | 没构建基础镜像 | `docker build -t fissue/sandbox-base:latest docker/sandbox` |
| `无法连接转发组件` | Linux 上没起 `sandbox serve` | 起它，或确认 socket 路径与配置一致 |
| 验证器一直 `needs_manual` | 问题无法自动化验证 / 验证器不可靠 | 正常现象；看 `data/artifacts/**` 里的执行日志人工复核 |
| 修复总是失败 | 仓库没有测试框架 / 问题太难 | 降级预期；或给 `test_hint` 明确测试命令 |
| `打标签失败` | Token 无写权限 | Token 需要 `repo` 写权限 |
| 仪表盘 404 | 条目 key 含 `#`，URL 未编码 | 用页面链接；API 用 `%23` 代替 `#` |
| `serve` 说"配置里没有启用的仓库" | `config.yaml` 的 `repos` 为空 | 登记至少一个仓库，或改用 CLI 传 `--repo` |

### 11.3 让测试命令更准

AI 有时猜不准项目的测试方式。加一条提示即可大幅提升验证器质量：

```yaml
repos:
  - platform: github
    owner: psf
    name: requests
    test_hint: "python -m pytest tests/ -q"        # 明确告诉它怎么跑测试
```

### 11.4 看日志定位细节

```bash
fissue fetch --verbose          # 任何命令都支持 -v
fissue eval --verbose
```

产物落盘位置（都在 `app.data_dir` 下，默认 `./data`）：

```
data/
  artifacts/<key>/base/       # 验证器 base 阶段输出（stdout/stderr/meta）
  artifacts/<key>/fix/        # 修复阶段输出
  artifacts/<key>/agent/      # Agent 循环每轮输出
  manual/<key>/report.md      # 需人工处理的完整报告
  manual/<key>/attempt.patch  # 尝试过的补丁
  patches/<key>.patch         # dry-run / patch_only 产出的补丁
  exports/                    # report / export 输出
```

用数据库直接查（以 PostgreSQL 为例）：

```bash
# 最近的高优先级待修条目
psql -d fissue -c "select key,priority,status from items where priority in ('tier1','tier2') order by id desc limit 20;"

# 今日 token 消耗
psql -d fissue -c "select purpose,sum(prompt_tokens+completion_tokens) t,round(sum(cost_usd)::numeric,4) usd from llm_usage where created_at > now()-interval '1 day' group by purpose;"

# 队列堆积情况
psql -d fissue -c "select queue,status,count(*) from queue_entries group by 1,2;"
```

### 11.5 彻底重来

```bash
fissue db init --drop            # 清空所有表（危险，需确认）
rm -rf data/                     # 清掉本地产物
fissue db init
```

---

## 附录：命令速查

```bash
fissue --help                    # 总览
fissue <命令> --help             # 每个命令的完整参数

fissue db init [--drop]          # 建表 / 重置
fissue db check                  # 连通性
fissue sandbox check             # 沙盒环境
fissue sandbox serve             # 转发组件常驻（Linux 生产）

fissue fetch [-r owner/name] [-p 平台] [--full] [--limit N] [--concurrency N]
fissue eval  [-r owner/name] [--all] [--deep] [--limit N] [--json]
fissue verify [-r owner/name] [--key KEY] [--limit N] [--json]
fissue flush [--queue verify|fix_bug|fix_feature] [--force/--auto] [--limit N]
fissue fix   [-r owner/name] [--key KEY] [--limit N] [--plan] [--dry-run] [--rounds N]
fissue force [-k KEY ...] [--dry-run] [--yes] [--undo]   # 强制修复 / 取消（见 9.8）

fissue report [-r owner/name] [-f markdown|json] [-o 路径] [--with-body]
fissue export [-f json|markdown] [-o 路径] [--category X] [--status Y]
fissue status [--json]

fissue bench capture [-r owner/name] [--label 名字] [-o 路径] [--json]
fissue bench list [--json]
fissue bench diff 旧.json 新.json [--json]     # 退出码 0=无变化 1=不可比 2=有变化

fissue serve [--once] [--with-web]
fissue web [--host H] [--port P] [--reload]
```

全局通用参数（几乎所有命令都支持）：

```
-c, --config PATH   指定 config.yaml
-e, --env PATH      指定 .env
-v, --verbose       输出调试日志
--json              JSON 输出（便于脚本消费）
```

---

