# 使用手册

> 从零到跑通：安装 → 配置 → 抓取 → 评测 → 沙盒验证 → 自动修复。
> 变量逐个说明见 [ENV.md](ENV.md)，架构见 [DESIGN.md](DESIGN.md)。

## 目录

[1 前置条件](#1-前置条件) · [2 安装](#2-安装) · [3 密钥](#3-密钥-env) · [4 业务配置](#4-业务规则-configyaml) ·
[5 沙盒镜像](#5-沙盒镜像) · [6 建库](#6-初始化数据库) · [7 五步流程](#7-核心流程-五步) ·
[8 交付形态](#8-四种交付形态) · [9 典型场景](#9-典型场景) · [10 成本](#10-成本控制) · [11 排错](#11-故障排查)

---

## 1. 前置条件

| 项 | 要求 | 检查 | 缺失时 |
|---|---|---|---|
| Python | ≥ 3.10 | `python --version` | 装 3.11+ |
| LLM API Key | **必需** | — | 见 §3 |
| Docker | 建议（沙盒隔离） | `docker info` | 可降级本地执行，但**无隔离** |
| PostgreSQL / MySQL | 可选（生产 / 多机） | `psql --version` | 默认 SQLite，够用 |

## 2. 安装

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash；Linux/macOS 用 .venv/bin/activate
pip install -e ".[dev]"
fissue --help
```

## 3. 密钥 .env

```bash
cp .env.example .env
```

**最少只填一项**即可跑通全流程：

```bash
FISSUE_LLM_API_KEY=sk-你的真实key
```

### 3.1 换模型供应商

只认 OpenAI 兼容协议（`/chat/completions`），换供应商改两个值：

| 供应商 | `FISSUE_LLM_BASE_URL` | `FISSUE_LLM_MODEL` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:14b` |

也可只写 `config.yaml` 的 `llm` 段（env 优先级更高，便于临时覆盖）。

### 3.2 数据库

```bash
FISSUE_DATABASE_URL=sqlite:///./data/fissue.db                                    # 默认，零依赖
FISSUE_DATABASE_URL=postgresql+psycopg://fissue:fissue@localhost:5432/fissue      # 生产 / 多机
FISSUE_DATABASE_URL=mysql+pymysql://fissue:fissue@localhost:3306/fissue?charset=utf8mb4
```

换 PostgreSQL 前先 `createdb -U fissue fissue`；MySQL 的字符集细节见 [ENV.md](ENV.md)。

### 3.3 平台 Token

公开仓库可留空（只读抓取）；自动修复必须配。

```bash
FISSUE_GITHUB_TOKEN=ghp_xxx
FISSUE_GITEE_TOKEN=xxx
FISSUE_ATOMGIT_TOKEN=xxx
FISSUE_GITLAB_TOKEN=glpat-xxx
```

最小权限：`repo`（GitHub / Gitee / AtomGit）、`api`（GitLab）；自动修复需写权限（建分支、提 PR）。

### 3.4 Web / API

```bash
FISSUE_WEB_HOST=127.0.0.1
FISSUE_WEB_PORT=8000
```

鉴权**不在 .env 里**：口令与 API Key 存数据库，见 [AUTH.md](AUTH.md)。首次部署打开 `/login`，
用启动时打印的引导口令建管理员；脚本/CI 用 `fissue auth key create`。

> ⚠️ 挂了反向代理就必须声明反代或可信网段。启动检查只能看**绑定地址**，看不见代理——
> 「绑 `127.0.0.1` + 同机 nginx 转出去」同样等于把写控制面对外开放。

---

## 4. 业务规则 config.yaml

```bash
cp config.example.yaml config.yaml
```

通常只需改前两段。

### 4.1 登记仓库（必改）

```yaml
repos:
  - platform: github          # github | gitee | atomgit | gitlab
    owner: psf
    name: requests
    base_branch: main         # 留空自动探测
    enabled: true
    collect: [issue, pr]
    labels_exclude: [wontfix, duplicate]
    since_days: 30            # 0 = 不限
    test_hint: null           # 留空由 AI 自行判断测试方式
    # api_base: https://gitlab.example.com/api/v4   # 自建 GitLab 才需要
```

多仓库继续加条目；首次建议只留一条跑通。

### 4.2 沙盒

```yaml
sandbox:
  enabled: true
  runtime: docker             # docker | local（local 无隔离，仅本机开发）；拼错会拒绝启动
  image: fissue/sandbox-base:latest
  network: none
  read_only_root: true
  git_readonly: true          # 工作区里的 .git 只读（index 除外）
  user: "1000:1000"
  limits: { cpus: 2.0, memory_mb: 2048, timeout_seconds: 600 }
```

`git_readonly` 掐掉的是宿主的**执行入口**（`.git/hooks`、`config` 里的 `filter.*.clean` /
`diff.*.textconv` / `credential.helper`）与**伪造入口**（`objects`/`refs`）。`.git/index` 特意
留可写——不少仓库的测试会顺手跑 `git status`。

### 4.3 自动修复

```yaml
fix_policy:
  tier1: { max_difficulty: 40, min_importance: 70 }
  tier2: { max_difficulty: 40, min_importance: 0 }
  only_issues: true           # 只修 Issue，PR 交人工

auto_fix:
  enabled: true
  agent_max_rounds: 12
  max_changed_files: 20
  max_diff_lines: 800
  on_failure: report_manual   # 失败生成人工报告，绝不硬提 PR
  pr_strategy:
    mode: fork                # fork | direct | patch_only
    label: ai-generated
```

**首次使用建议** `mode: patch_only`（或命令行 `--dry-run`），只产补丁供人工审阅。

### 4.4 阈值与队列

```yaml
evaluation:
  thresholds:
    importance_high: 70
    difficulty_low: 40
    alert_importance: 80
queues:
  verify_queue: { flush_size: 10, idle_flush_seconds: 300 }
  fix_queue:    { flush_size: 10, idle_flush_seconds: 300 }
```

### 4.5 既有测试回归门

F2P 只证明目标用例被修好，证明不了「没弄坏别的」（例：`split(" ")` → `split()` 修对了空串，
却改掉连续空格语义）。回归门用仓库自己的测试套件补 pass-to-pass 方向。

```yaml
verifier:
  regression_gate: strict     # off | warn | strict
  regression_command: null    # 留空 → repo.test_hint → 自动探测
  regression_timeout_seconds: 900
auto_fix:
  regression_retries: 1       # 被拦下时把「哪几条用例坏了」喂回 Agent 重试
```

| 档位 | 行为 |
|---|---|
| `off` | 不跑，零开销 |
| `warn` | 跑、记录、报告标注，不改变结论（先统计「本就红」的比例） |
| `strict`（默认） | base 绿而 fix 不绿 → 拒绝并转人工 |

⚠️ base 阶段就不绿、沙盒缺依赖、套件超时，一律视为**不可信并放行**——否则会大面积误杀。
命令来源 `regression_command → test_hint → 探测`，**不由 LLM 现编**。详见
[REGRESSION-GATE.md](REGRESSION-GATE.md)。

### 4.6 通知（可选）

```yaml
notify:
  enabled: true
  channels:
    - { type: console, enabled: true }
    - { type: webhook, enabled: true, url: "https://..." }
    - { type: wecom,   enabled: false, url: "企业微信机器人 Webhook" }
    - { type: dingtalk, enabled: false, url: "钉钉 Webhook", secret: "加签密钥" }
    - { type: feishu,  enabled: false, url: "飞书 Webhook" }
  events: [scan_done, eval_done, verify_done, fix_done, pr_created, needs_manual, budget_exceeded]
```

---

### 4.7 EdgeJev 旁路打分器（可选）

项目可以额外接一个**本地**的 Jev / System One 服务
（[yzfly/edgejev](https://github.com/yzfly/edgejev)），让它独立给
`feasibility` / `difficulty` / `pr_quality` 打分，再按 `relation` 与主模型融合。

它不是主模型的替代品：协议只有 `POST /v1/systemone`（强类型决策：选项 / 分档 /
概率 / 置信度），没有 `/chat/completions`，也不支持流式。

```yaml
edgejev:
  enabled: true                     # 本地服务起好后再开
  base_url: http://127.0.0.1:8009
  model: jev-latest                 # 与本地加载的模型名一致
  dimensions: [feasibility, difficulty, pr_quality]   # Issue 上会自动剔除 pr_quality
  relation: second_opinion          # 见下表
  weight: 0.4                       # 仅 weighted 用
  min_confidence: 0.15              # 低于此值该维回退主模型
  divergence_threshold: 25          # 仅 second_opinion 用
```

| `relation` | 行为 |
|---|---|
| `second_opinion` | 保留主模型分数；两者相差 ≥ `divergence_threshold` 时写告警 |
| `weighted` | `主模型 * (1 - weight) + EdgeJev * weight` |
| `replace` | 该维度直接采用 EdgeJev 换算分 |
| `shadow` | 只记录 EdgeJev 读数，完全不影响结论（先观察再切换的推荐姿势） |

融合发生在算优先级**之前**，所以 `weighted` / `replace` 改的可行性、难度会真正影响
tier1/tier2 分档。任何模式下，连接失败、返回结构异常或置信度不足都**回退主模型结论**，
原因写进该条评测的 `alerts`（`fissue export` 的 JSON 里可见）。

建议先用 `shadow` 跑一轮，比对 `edgejev.answers` 与主模型分数是否合理，再切
`second_opinion` 或 `weighted`。

---

## 5. 沙盒镜像

镜像预装 Python / Node / Go / Java / Rust 测试框架，避免每次验证联网装依赖（容器默认禁网）。

```bash
docker build -t fissue/sandbox-base:latest docker/sandbox   # 首次 3~8 分钟
fissue sandbox check
```

```
Docker：✅ 26.0.0
执行后端：inline（进程内调用）
镜像：fissue/sandbox-base:latest
隔离：网络=none｜只读根=True｜用户=1000:1000｜CPU=2.0｜内存=2048MB｜超时=600s
```

不想构建也能跑（`runtime: local`），但**完全没有容器隔离**，仅限本机开发。
生产（Linux）另起转发组件，让执行走受控通道：`fissue sandbox serve`
（Windows 无 Unix socket，自动走 inline）。

## 6. 初始化数据库

```bash
fissue db init        # 建表，幂等
fissue db check       # → ✅ 数据库已就绪：sqlite:///./data/fissue.db
fissue db init --drop # 清空所有表（危险）
```

---

## 7. 核心流程 五步

前四步建议先在单仓库、小批量上验证效果。

### 步骤 1 — 抓取

```bash
fissue fetch --repo psf/requests --limit 50      # 单仓库，默认 github
fissue fetch                                     # config.yaml 里所有仓库
fissue fetch --repo oschina/git-osc --platform gitee
fissue fetch --full                              # 忽略增量游标
```

抓取自动做三件事：**增量**（`since_days` 窗口，首次全量）、**去重**（内容指纹 `content_hash`
没变就跳过）、**过滤**（`labels_include` / `labels_exclude`）。

> 公开仓库不配 Token 也能抓，但限流紧（60 次/小时）；配 Token 后 5000 次/小时。

### 步骤 2 — 评测

```bash
fissue eval                            # 评测状态为 new 的条目
fissue eval --all --limit 100          # 所有未评测的（含历史）
fissue eval --repo psf/requests --all
fissue eval --all --deep               # PR 额外拉 diff 评代码质量，更贵更准
fissue eval --all --json > evals.json
```

输出表格含 `category / authenticity / importance / difficulty / priority / action`。这一步做：
分类（BUG 还是 FEATURE，决定走哪条流水线）、四维评分（各 0-100 + 理由 + 证据引用）、难度评估、
反刷子（误报 / 重复 / AI 灌水）、按 `fix_policy` 定 tier1 / tier2 / none。

> **FEATURE 到这里就结束了**——只打标签，不生成验证器、不自动修复。

### 步骤 3 — 验证

```bash
fissue verify --repo psf/requests --limit 10
fissue verify --key github:psf/requests#6712
fissue verify --repo psf/requests --json
```

关键判据 **F2P**：

| 阶段 | 代码状态 | 期望 | 含义 |
|---|---|---|---|
| base | 未修复 | **必须失败** | 问题真实存在、可复现 |
| fix | 修复/合并后 | **必须通过** | 修复有效 |

三种失败处置（都**不会**进入自动修复）：

- **base 就通过** → 验证器不可靠 → 让 AI 修正（最多 `verifier.max_rounds` 轮）→ 仍不行标 `needs_manual`；
- **base 报错/超时** → 结论不可信 → `needs_manual`；
- **仓库无测试框架** → 降级为自然语言清单，LLM 按证据逐条判定；配置了 `test_hint` 的仓库
  **不允许**降级（强制可执行验证器，外部依赖用 mock）。强制修复的条目修完后由 LLM 在修复
  工作区上重判，全部满足才提 PR。

验证通过的条目进入 **verify 队列**。

### 步骤 4 — 批量定论（flush）

自动触发条件（取或）：攒够 `flush_size`（默认 10）· 队首静默超 `idle_flush_seconds`（默认 300）·
队列排空（哪怕只有 1 条）。也可手动：

```bash
fissue flush                  # 强制 flush 全部队列
fissue flush --queue verify
fissue flush --auto           # 按触发条件判断（cron 常用）
```

产物：**打标签**（`reproduced` / `ai-verified` / `needs-review` / `fissue` 等）；**派发修复**——
结论为 `fix` 且 tier1/tier2 的 Issue → `fix_queued`。

> 为什么批量：同一批验证输出放同一上下文，AI 能横向比较（识别成批重复提交），也更省钱。

### 步骤 5 — 自动修复

```bash
fissue fix --plan --dry-run --limit 5   # 只产补丁与报告，不建分支不提 PR
fissue fix --plan --limit 3             # 真提 PR
fissue fix --key github:psf/requests#6712
fissue fix --limit 3 --rounds 8
```

Agent 是**循环**而非一次性吐 patch：读文件 → 模型决定下一步 → 写文件 / 跑验证器 → 看结果，
未通过则继续（最多 N 轮）。成功路径：Agent 自测通过 → **再跑一次完整 F2P 复核** → fork →
建分支 → 推送 → 提 PR → 打 AI 标签。

失败路径（`on_failure: report_manual`）：**绝不硬提 PR**，改生成人工报告：

```
data/manual/<key>/
  report.md        # 失败原因 + 动作轨迹 + 被拒写入 + 最后一次验证输出
  attempt.patch    # 尝试过的补丁
  trace.json       # 结构化轨迹 + token 用量
```

**宿主侧护栏**（模型说了不算）：路径白名单 · 保护路径（`.github/**`、`LICENSE`、`**/*.lock` 等）·
验证器锁定 · 规模上限 · 提 PR 前重跑 F2P · PR **永不自动合并**。详见 [DESIGN.md §5](DESIGN.md)。

---

## 8. 四种交付形态

### 8.1 CLI

```bash
fissue report --repo psf/requests --format markdown --out data/exports/report.md
fissue report --with-body
fissue export --format json
fissue export --category bug --status evaluated
fissue status [--json]
```

**跑分基线**（把「测量」变成可对比快照）：

```bash
fissue bench capture --repo John-is-playing/ratekit --label ratekit-before
fissue bench capture --repo John-is-playing/ratekit --label ratekit-after
fissue bench diff data/benchmarks/ratekit-before.json data/benchmarks/ratekit-after.json
fissue bench list
```

记录**前提**（commit、是否有未提交改动、模型与采样参数、关键配置指纹）与**结果**（条目分布、
各阶段 outcome 交叉表、F2P 成立数、修复与 PR 结果）。`diff` **先判可比性**再给数字；退出码
`0` 可比无变化｜`1` 不可比｜`2` 可比有变化——可直接塞进 CI。基线落在 `data/benchmarks/`，
是唯一进版本库的 `data/` 子目录。

### 8.2 常驻服务

```bash
fissue serve              # 增量扫描 + 队列调度 + 验证 + 通知
fissue serve --with-web    # 同进程带起 Web/API
fissue serve --once        # 只跑一轮，适合 cron / 任务计划
```

| 循环 | 默认间隔 | 做什么 |
|---|---|---|
| 扫描 | 每小时 | 增量抓取 → 评测新条目 → 验证（生成验证器 + F2P）→ 高重要性告警 |
| flush | 60 秒 | 按条件 flush → 打标签 → 派发修复 |
| 修复 | 120 秒 | 修 tier1/tier2 的 Issue，受预算与策略限制 |

```yaml
schedule:
  incremental: true
  scan_interval_seconds: 3600
  queue_flush_interval_seconds: 60
  full_rescan_days: 30
  verify_enabled: true        # false = 常驻不做验证，交给人工 fissue verify
  verify_batch_limit: 30
```

```bash
# Linux crontab：每小时扫一轮
0 * * * * cd /path/to/Fissue && fissue serve --once >> /var/log/fissue.log 2>&1
```

### 8.3 Web 工作台 + REST API

```bash
fissue web                        # 默认 127.0.0.1:8000
fissue web --host 0.0.0.0 --port 8080
fissue web --reload               # 开发热重载
```

仪表盘含列表（按仓库/类型/分类/优先级/状态筛选）、条目详情（评分理由、证据、验证器、执行与
修复记录）与操作按钮（扫描 / 评测 / 验证 / Flush / 试跑修复）。长任务以「任务」运行，`/jobs/<id>`
以事件流展示 LLM 流式输出、Agent 工具动作与提 PR 等平台动作（运行中每 1.5s 刷新）。
`serve --with-web` 时定时轮次也登记为 `round` 任务，可从页面停掉；轮次进行中提交网页任务直接 409。

交互式 API 文档：`http://127.0.0.1:8000/docs`。

```bash
BASE=http://127.0.0.1:8000/api/v1

curl $BASE/health
curl $BASE/stats
curl "$BASE/items?category=bug&priority=tier1"
curl "$BASE/items/github%3Apsf%2Frequests%2342"     # key 要 URL 编码
curl $BASE/queues
curl "$BASE/export?format=markdown"

# 操作类（web.read_only=false 时可用）
curl -X POST $BASE/actions/scan     -H 'Content-Type: application/json' -d '{"repo":"psf/requests"}'
curl -X POST $BASE/actions/evaluate -H 'Content-Type: application/json' -d '{"limit":20}'
curl -X POST $BASE/actions/verify   -H 'Content-Type: application/json' -d '{"limit":10}'
curl -X POST $BASE/queues/flush     -H 'Content-Type: application/json' -d '{"force":true}'
curl -X POST $BASE/actions/fix      -H 'Content-Type: application/json' -d '{"dry_run":true,"limit":5}'

# 人工闸门
curl -X POST "$BASE/actions/approve-fix?key=github%3Apsf%2Frequests%2342" -H 'Content-Type: application/json' -d '{}'
curl -X POST "$BASE/actions/approve-merge?key=github%3Apsf%2Frequests%2399" -H 'Content-Type: application/json' -d '{"note":"已人工确认"}'
```

> `approve-merge` 只打 `merge-approved` 标签 + 留评论，**不会真的合并**。

```bash
fissue auth key create --name my-machine        # 明文只显示这一次
curl $BASE/stats -H "Authorization: Bearer fissue_<prefix>_<secret>"
```

只读模式：`web.read_only: true`（隐藏操作按钮，API 动作类返回 403）。

### 8.4 组合方式

```bash
# 开发 / 试用
fissue fetch -r psf/requests && fissue eval --all && fissue verify -r psf/requests
# 生产
fissue serve --with-web
```

### 8.5 入站 webhook（事件驱动）

默认是**轮询**（默认 1 小时），webhook 让平台一推就跑一轮：

```yaml
webhooks:
  enabled: true
```
```bash
FISSUE_WEBHOOK_GITHUB_SECRET=<仓库 Webhook 里填的 Secret>
```

| 平台 | Payload URL | 订阅 | 密钥 |
|---|---|---|---|
| GitHub | `https://你的域名/api/v1/webhooks/github` | Issues、Pull requests | Secret（`X-Hub-Signature-256`，HMAC-SHA256） |
| GitLab | `…/api/v1/webhooks/gitlab` | Issue / MR events | `X-Gitlab-Token` |
| Gitee / AtomGit | `…/api/v1/webhooks/gitee`（或 `…/atomgit`） | Issue、PR | `X-Gitee-Token` |

验签通过且仓库已登记 → 回 `202` 并**后台**跑一轮（抓取→评测→验证→flush→修复），该轮同样在
写锁内，已有写进程则跳过（下一轮扫描会带上）。验签不过回 403；仓库未登记回 202 但
`accepted: false` 并写明原因；非条目类事件只记日志。

三个坑：① 端点不带 bearer 鉴权，**密钥就是它的身份**，别拿 API token 当密钥；② 必须能被
**平台服务器**访问到（只绑回环或反代不放行 `/api/v1/webhooks/*` 时永远收不到，日志里一条都没有）；
③ 开着 `webhooks.enabled` 却没给某个已启用平台配密钥会**启动失败**（刻意 fail-closed）。

---

## 9. 典型场景

### 9.1 首次试跑（10 分钟，不写平台）

```bash
fissue fetch --repo psf/requests --limit 20
fissue eval --limit 20
fissue status
fissue report --repo psf/requests --out data/exports/first.md
```

只读平台数据、写本地库，**不会**打标签或提 PR。

### 9.2 观察自动修复质量（推荐上线姿势）

```yaml
auto_fix:
  pr_strategy: { mode: patch_only }
```

```bash
fissue fetch && fissue eval --all && fissue verify && fissue flush && fissue fix --plan
# 看 data/patches/*.patch 与 data/manual/*/report.md
```

满意后改 `mode: fork`（fork → 分支 → 上游 PR）。

### 9.3 多仓库托管

```yaml
repos:
  - { platform: github,  owner: psf,       name: requests,  since_days: 14 }
  - { platform: github,  owner: pallets,   name: flask,     since_days: 14 }
  - { platform: gitee,   owner: mindspore, name: mindspore, since_days: 30 }
  - { platform: atomgit, owner: openharmony, name: arkcompiler, since_days: 30 }
  - { platform: gitlab,  owner: gitlab-org, name: gitlab,   since_days: 7, api_base: https://gitlab.com/api/v4 }
```

```bash
fissue fetch && fissue eval --all --concurrency 4 && fissue serve --with-web
```

### 9.4 只关心高重要性

```yaml
evaluation: { thresholds: { alert_importance: 80 } }
notify:
  enabled: true
  channels: [{ type: wecom, enabled: true, url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." }]
  events: [high_importance, needs_manual, pr_created]
```

### 9.5 交给 CI 定时跑

```bash
fissue serve --once
# 或分步更可控
fissue fetch --concurrency 1 && fissue eval --all --limit 50 && fissue verify --limit 20 \
  && fissue flush --auto && fissue fix --limit 3
```

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

### 9.6 只跑某一条

```bash
KEY=github:psf/requests#6712
fissue verify --key $KEY
fissue flush
fissue fix --key $KEY --dry-run
```

> `eval` 没有 `--key`（批量语义），单条用 `--limit 1`；`verify` / `fix` 支持 `--key`。

### 9.7 条目卡住了：`requeue`

```bash
fissue requeue --stale --dry-run   # 内容改过、结论过期，但修复已开工
fissue requeue --stale --yes
fissue requeue --failed --yes      # 状态 failed 的条目
```

`--failed` 的回退目标按已有结论决定：有评测 → `evaluated`，没有 → `new`。
`--stale` 不自动重评：条目内容实质变化时修复可能已开工，自动回落会给「重复修复 / 重复建 PR」
再开一道口子，所以只留痕（`stale_reasons`，导出与 API 可见），由人确认后放回。

### 9.8 模型说不修，但你要修：`force`

```bash
fissue force -k github:owner/repo#42 --dry-run
fissue force -k github:owner/repo#42 --yes
fissue force -k github:owner/repo#42 --undo
```

WebUI 是条目详情页的「强制修复」按钮（仅 Issue），列表页可批量。强制时：打 `force_fix` 标记 →
走一遍验证（分类 / 刷量 / 可复现三道闸门失效，**没有验证器会顺手生成一个，这道闸不旁路**）→
直落待修 tier1，跳过定论队列与 LLM 裁决。

两条边界：已在修复流程中（`fixing` / `pr_created` / `fix_queued`）的条目强制时**保持原状**
（拉回来重验会多开一个 PR）；取消强制时仍在待修队列的条目回退「需人工」。

---

## 10. 成本控制

| 步骤 | 单条消耗 | 说明 |
|---|---|---|
| `eval` | 中 | 一次调用出四维评分 |
| `eval --deep` | 中高 | 额外喂 PR diff 评代码质量 |
| `verify` | 中 | 生成验证器（不可靠时最多重试 `max_rounds` 轮） |
| `flush` | 低（分摊） | 一批一次，条数越多单条越便宜 |
| `fix` | 高 | Agent 循环，每轮带上下文 |

```yaml
llm:
  concurrency: 4
  budget:
    daily_tokens_per_repo: 2000000
    daily_usd_per_repo: 5.0
    hard_stop: true             # true=超限即停；false=仅告警
  pricing: { input_per_1k: 0.00014, output_per_1k: 0.00028 }
```

`fissue status` 查看当日用量。六个省钱开关：缩小 `since_days` · `use_keywords_first: true` ·
不开 `--deep` · `verifier.max_rounds: 2` · `auto_fix.agent_max_rounds: 8` · 收紧 `fix_policy.tier2`。
增量抓取天然省钱：指纹没变就不重复评测。

---

## 11. 故障排查

```bash
fissue db check          # 数据库通不通
fissue sandbox check     # Docker 与隔离参数
fissue status            # 条目/队列/用量/沙盒 全景
```

| 现象 | 原因 | 解决 |
|---|---|---|
| `缺少 LLM API Key` | `.env` 没填 | 填上；确认在项目根目录执行（能找到 `.env`） |
| `无法连接数据库` | 连接串错 / PG 没起 | `fissue db check`；`pg_isready` |
| `数据库不存在` | 没建库 | `createdb -U fissue fissue` 后 `fissue db init` |
| `401 / Bad credentials` | Token 过期或权限不足 | 重新生成，检查 scope |
| 触发限流 | 未配 Token 或调用过密 | 配 Token；调小 `--concurrency` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | HTTPS 中间人 | 先看报错里的「当前生效的信任链」：`certifi` → 装 truststore；已是系统信任库 → 导出根证书设 `FISSUE_CA_BUNDLE`。见 [ENV.md](ENV.md) |
| `未找到 docker 命令` | 没装 / 不在 PATH | 装 Docker；或 `runtime: local`（无隔离） |
| `镜像不可用` | 没构建镜像 | `docker build -t fissue/sandbox-base:latest docker/sandbox` |
| `无法连接转发组件` | Linux 上没起 `sandbox serve` | 起它，或核对 socket 路径 |
| 验证器一直 `needs_manual` | 无法自动验证 / 验证器不可靠 | 正常；看 `data/artifacts/**` 人工复核 |
| 修复总是失败 | 没测试框架 / 问题太难 | 给 `test_hint` 明确测试命令 |
| 打标签失败 | Token 无写权限 | 需要 `repo` 写权限 |
| 仪表盘 404 | key 含 `#` 未编码 | 用页面链接；API 用 `%23` |
| `serve` 说没有启用仓库 | `repos` 为空 | 登记至少一个仓库，或用 CLI 传 `--repo` |

**让测试命令更准**：`test_hint: "python -m pytest tests/ -q"` 能大幅提升验证器质量。

**看日志**：任何命令支持 `-v`。产物都在 `app.data_dir`（默认 `./data`）：

```
data/
  artifacts/<key>/base/       # 验证器 base 阶段输出
  artifacts/<key>/fix/        # 修复阶段输出
  artifacts/<key>/agent/      # Agent 每轮输出
  manual/<key>/report.md      # 人工处理报告
  manual/<key>/attempt.patch
  patches/<key>.patch         # dry-run / patch_only 补丁
  exports/                    # report / export
```

```sql
-- 最近的高优先级待修条目
select key,priority,status from items where priority in ('tier1','tier2') order by id desc limit 20;
-- 今日 token 消耗
select purpose,sum(prompt_tokens+completion_tokens) t,round(sum(cost_usd)::numeric,4) usd
  from llm_usage where created_at > now()-interval '1 day' group by purpose;
-- 队列堆积
select queue,status,count(*) from queue_entries group by 1,2;
```

**彻底重来**：`fissue db init --drop` + `rm -rf data/` + `fissue db init`。

---

## 附录：命令速查

```bash
fissue --help                     # 总览；fissue <命令> --help 看完整参数

fissue db init [--drop] · db check
fissue sandbox check · sandbox serve

fissue fetch  [-r owner/name] [-p 平台] [--full] [--limit N] [--concurrency N]
fissue eval   [-r owner/name] [--all] [--deep] [--limit N] [--json]
fissue verify [-r owner/name] [--key KEY] [--limit N] [--json]
fissue flush  [--queue verify|fix_bug|fix_feature] [--force|--auto] [--limit N]
fissue fix    [-r owner/name] [--key KEY] [--limit N] [--plan] [--dry-run] [--rounds N]
fissue force  [-k KEY ...] [--dry-run] [--yes] [--undo]
fissue requeue [--stale|--failed] [--dry-run] [--yes]

fissue report [-r owner/name] [-f markdown|json] [-o 路径] [--with-body]
fissue export [-f json|markdown] [-o 路径] [--category X] [--status Y]
fissue status [--json]

fissue bench capture [-r owner/name] [--label 名字] [-o 路径] [--json]
fissue bench list [--json]
fissue bench diff 旧.json 新.json [--json]   # 0=无变化 1=不可比 2=有变化

fissue serve [--once] [--with-web] · web [--host H] [--port P] [--reload]
fissue auth {status|set-password|reset|key list|create|revoke}
fissue platforms doctor [--write] [--probe-push]
fissue onboard [--yes ...]
```

通用参数：`-c/--config PATH` · `-e/--env PATH` · `-v/--verbose` · `--json`。

---

相关：[ENV.md](ENV.md) · [AUTH.md](AUTH.md) · [DESIGN.md](DESIGN.md) ·
[REGRESSION-GATE.md](REGRESSION-GATE.md) · [wiki/CLI-Reference.md](wiki/CLI-Reference.md) ·
[wiki/Troubleshooting.md](wiki/Troubleshooting.md)