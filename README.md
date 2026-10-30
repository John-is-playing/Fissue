# Fissue

> 自动从 **GitHub / Gitee / AtomGit / GitLab** 检索 Issue / PR，用 AI 大模型判断
> **真实性 / 重要性 / 可行性 / PR 质量**，生成验证器并在 Docker 沙盒中做 F2P 验证，
> 对「低难度 + 高重要性」的 Issue 自动修复并提 PR。
> **注意：**该项目绝大多数代码由AI Agent驱动编写，注意辨识。

---

## 它解决什么问题

维护一个开源仓库，最痛的不是写代码，而是面对一堆 Issue/PR 不知道**先看哪个**：

- 这条 Issue 是真 Bug 还是用户用错了？
- 这是真需求还是重复提交 / 刷量？
- 这个 PR 真的修好了问题吗？合并会不会带崩？
- 哪些可以放心让 AI 先修，哪些必须人来判断？

Fissue 把这一整套「分诊（triage）→ 验证 → 修复」流程自动化，并留下完整的
评分理由与证据链，供人复核。

---

## 核心流程

```
                    ┌────────────────────────────────────────────┐
                    │  抓取 Issue / PR（四平台，增量）            │
                    └───────────────────┬────────────────────────┘
                                        ▼
                    ┌────────────────────────────────────────────┐
                    │  元数据预筛 → AI 分类：BUG / FEATURE         │
                    └───────┬────────────────────────┬───────────┘
                            ▼                        ▼
                   ┌─────────────────┐      ┌─────────────────┐
                   │   Issue · BUG   │      │ Issue · FEATURE │
                   └────────┬────────┘      └────────┬────────┘
                            ▼                        ▼
              ┌──────────────────────────┐   评估可行性 / 难度 / 必要性
              │ AI 评估修复难度 + 写验证器│          打标签
              │      → 验证队列           │           → 等开发者处理
              └────────────┬─────────────┘
                           ▼
                   沙盒执行器跑验证器
                           ▼
        ┌──────────────────────────────────────┐
        │ 攒够 N 条 或 队列空 → 整批交 AI       │
        │ 打 有效性 / 重要性 标签                │
        └──────────────┬───────────────────────┘
                       ▼
        ┌──────────────────────────────────────┐
        │ 难度低 + 重要性高 → AI Agent 修复      │
        │ 难度低 + 重要性低 → AI Agent 修复      │
        │ 其余 → 打标签，等开发者                │
        └──────────────┬───────────────────────┘
                       ▼
              fork 分支 → 提 PR（带 AI 标签）

                    ┌────────────────────────────────────────────┐
                    │                PR · BUG / FEATURE           │
                    └───────────────────┬────────────────────────┘
                                        ▼
                        判定提交者：开发者/社区 or AI
                                        ▼
                     找关联 Issue → AI 编写验证器 → 修复队列
                                        ▼
                沙盒：合并 PR + 跑验证器验证功能 → 结果入队列
                                        ▼
              攒够 N 条 或 队列空 → AI 判定能否合并 → 打标签
                                        ▼
                          等开发者手动合并
```

---

## 技术栈

| 项 | 选型 |
|---|---|
| 语言 | Python 3.10+ |
| 交付形态 | CLI + Web 仪表盘 + REST API + 常驻服务 |
| 抓取平台 | GitHub · Gitee · AtomGit · GitLab（含自建实例） |
| AI | 任意 OpenAI 兼容端点（DeepSeek / OpenAI / 通义 / Kimi / 本地 vLLM…） |
| 存储 | SQLite（默认，零依赖）· PostgreSQL / MySQL（可选，需显式配置；**两个网络后端都做过真机验证**：PostgreSQL 是真实部署，MySQL 在 Docker `mysql:8.0` 上跑通了 `db init → fetch → eval → verify → flush → fix`，见 `BENCHMARK.md` §5）；可导出 JSON / Markdown |
| 沙盒 | Docker：禁网、限 CPU/内存/PID/时长、非 root、只读根、只挂隔离工作区、转发组件通道 |
| 配置 | `.env`（密钥）+ `config.yaml`（业务配置） |

---

## 快速开始

```bash
# 1. 安装
python -m venv .venv
source .venv/bin/activate          # Linux / macOS / Git Bash
# .venv\Scripts\Activate.ps1       # Windows PowerShell
pip install -e ".[dev]"

# 2. 准备配置
cp .env.example .env            # 填入 LLM key 与平台 token（数据库默认 SQLite，可不动）
cp config.example.yaml config.yaml

# 3. 初始化数据库（默认 SQLite，文件落在 ./data/fissue.db，无需另起数据库）
fissue db init

# 4. 抓取仓库（先单仓库）
fissue fetch --repo psf/requests --platform github --limit 50

# 5. AI 评测
fissue eval --all

# 6. 生成验证器并在沙盒中做 F2P 验证
fissue verify --repo psf/requests

# 7. 手动 flush 队列（也可等数量阈值 / 空闲超时自动触发）
fissue flush --queue verify        # Issue-BUG 的验证队列
fissue flush --queue fix_bug       # PR-BUG 的合并验证队列（FEATURE 用 fix_feature）

# 8. 自动修复「低难度 + 高重要性」的 Issue 并提 PR
fissue fix --plan

# 9. 出报告 / 导出
fissue report --format markdown --out data/exports/report.md
fissue export --format json

# 10. 起常驻服务（增量扫描 + 定时调度 + 通知）
fissue serve

# 11. 起 Web / API
fissue web
```

---

## 命令一览

| 命令 | 说明 |
|---|---|
| `fissue onboard` | **初始化向导**：交互式配好模型 / 数据库 / 平台凭据 / 监控仓库，并抓第一轮（无头机器用 `--yes` + 参数走非交互） |
| `fissue db init` | 建表 / 迁移 |
| `fissue fetch` | 抓取 Issue/PR（增量） |
| `fissue eval` | AI 评测（四维评分 + 标签 + 反刷子） |
| `fissue verify` | 生成验证器 + 沙盒 F2P 验证 |
| `fissue flush` | 手动 flush 验证/修复队列 |
| `fissue requeue` | 把卡住的条目放回流水线（`--stale` = 结论过期但修复已开工；`--failed` = 失败重试） |
| `fissue force` | **强制修复**：不管评测结论如何，把指定条目推进修复流程（`-k platform:owner/name#number`，`--dry-run` 只看计划、`--yes` 直接执行、`--undo` 取消强制） |
| `fissue fix` | 自动修复并提 PR（`--dry-run` 只产 patch） |
| `fissue report` | 生成 Markdown/JSON 报告 |
| `fissue export` | 导出全量数据 |
| `fissue status` | 查看当前状态：条目统计、队列情况、模型用量、沙盒可用性 |
| `fissue platforms doctor` | **平台能力体检**：这套令牌 + 这个仓库到底能做什么——只读、克隆、打标签、建评论、推分支。只读与克隆默认就跑，`--write` / `--probe-push` 才做写探测且痕迹自动清理 |
| `fissue serve` | 常驻服务：增量扫描 + 队列调度 + 阈值通知（**也含验证**；`--with-web` 同进程带起 Web/API，`--once` 只跑一轮） |
| `fissue web` | Web 工作台 + REST API：总览 / 条目 / 队列 / 任务 / 监控仓库 / 状态监测 / 初始化向导 / 设置 |
| `fissue sandbox serve` | 起沙盒转发组件（宿主侧常驻，沙盒执行的唯一通道）；`fissue sandbox check` 检查沙盒环境是否就绪 |
| `fissue bench` | 跑分基线：`capture` 冻结 / `diff` 逐项对比（先判可比性）/ `list` 列出 |
| `fissue auth` | Web/API 身份管理：`status` 看鉴权状态、`set-password` 建或改管理员口令、`reset` 重置、`key list/create/revoke/import-legacy` 管 API Key（详见 [docs/AUTH.md](docs/AUTH.md)） |

---

## 目录结构

```
src/fissue/
  config.py       配置加载（.env + config.yaml）
  models.py       领域模型
  store/          SQLAlchemy 持久化（SQLite 默认 / PostgreSQL · MySQL 可选）+ JSON/MD 导出
  platforms/      GitHub / Gitee / AtomGit / GitLab 适配器
  ai/             OpenAI 兼容客户端、预算、提示词、评估器
  verifier/       验证器生成与 F2P 校验
  sandbox/        Docker 沙盒与转发组件
  pipeline/       流水线与队列
  fixer/          自动修复 Agent 循环与提 PR
  service/        常驻调度与通知
  web/            REST API 与仪表盘
  cli/            命令行入口
```

---

## 它靠谱吗

Fissue 的核心主张是**「别信模型的自述，要看独立证据」**。所以它自己也要接受同样的检验。

[**`BENCHMARK.md`**](BENCHMARK.md) 公开了在五套对抗性夹具（60 Issue + 20 PR）上的
**真实测量结果**，包括：

- ✅ 回归门在 3 套夹具上**全部拦下**了「修好目标却弄坏别处」的假修复；
- ✅ 假修复、刷量、误报、设计偏好变更均被拦在自动修复之外；
- ✅ **真实提 PR 路径已端到端跑通**（`envkit#4` → PR #31、`lockkit#2` → PR #28），且两处的
  改动都只有源码文件、**未夹带验证器测试文件**；
- ✅ `lockkit` 已测量（27 条全部有结论，`lockkit#2` 3 轮修好并提 PR；注意那一次只跑了
  「单条修复」的通路，全量修复跑分见 `BENCHMARK.md` §2.1 注 ²）；
- ✅ `pipekit` 的 Issue 侧自动修复 **7/7**——7 条待修条目**全部在 2~3 轮内产出补丁**；
- ✅ 旧结论「跨模块根因定位拿不到正例」（`pipekit` 0/2、一次都没落笔）**已被自己推翻**：
  只改提示词后就是上面那个 7/7。这条自我更正的过程完整留在 `BENCHMARK.md` §4.9 / §4.11；
- ✅ **PostgreSQL 后端已完成真实部署验证**（`1.1.0-b4`：`db init` / `db check` / 全量单测 /
  服务启动与页面实测均在该后端上跑过），但上面各夹具的跑分数字仍全部来自 SQLite——两者口径不同，不要混读；
- ⚠️ **Gitee 的条目级写：只剩「建/改 Issue」两个动作做不到**（2026-10-30 复核）：
  `POST /issues` 与 `PATCH /issues/{n}` 一律 `404 project or enterprise`（换新令牌、换私有仓库
  都复现，原因仍未定位）。但**打标签与建评论是通的**——先前记的「打标签也被拒」是**我们发错了
  请求体形状**（Gitee 只认裸 JSON 数组，发 `{"labels":[…]}` 会得到 `400 Problems parsing JSON`），
  适配器已修，`flush` 在 Gitee 上的打标签因此从「坏的」变成「可用的」。
  仓库级写（建分支、提 PR、仓库标签）一直是通的。判据与实测矩阵见
  [`BENCHMARK.md` §4.16](BENCHMARK.md#416-写操作atomgit-全部跑通gitee-卡在平台限制2026-10-15)；
  四个平台的**只读**链路已全部跑通。
- ⚠️ tier 标定存在**边界抖动**（不是一份稳定清单）：2026-10-30 用当前代码/配置复测，
  `envkit` 上次那 5 处偏差**全部转命中**、却反向多出 3 处；`lockkit` 由 4 处变 5 处
  （含 1 处反向）。根因是模型的重要性评分密集在 62~82、而门槛 `min_importance=80` 正压在
  这段分布右端，单次抖动即可跨线；**功能后果为零**（tier1/tier2 都进修复队列，只差标签）。
  逐条对照与复跑命令见 [`BENCHMARK.md` §4.7.1](BENCHMARK.md#471-复测2026-10-30偏差不是一份稳定清单而是阈值边界抖动)。

> 我们宁可把上面这段写出来，也不愿给一份「全部通过」的漂亮表格——
> 那正是 Fissue 存在的意义所反对的事。

---

## 参与贡献

| 文档 | 内容 |
|---|---|
| [**CONTRIBUTING.md**](CONTRIBUTING.md) | 三条贡献路径（**加一套夹具** / 加平台适配器 / 沉淀验证器模式）、开发环境、提交规范 |
| [**BENCHMARK.md**](BENCHMARK.md) | 公开基准：方法学、实测结果、已知失败、尚未测量项 |
| [**SECURITY.md**](SECURITY.md) | 威胁模型、沙盒隔离参数、漏洞上报渠道 |
| [**CODE_OF_CONDUCT.md**](CODE_OF_CONDUCT.md) | 行为准则（含本项目定制的「拒绝刷量」条款） |
| [**CHANGELOG.md**](CHANGELOG.md) | 变更记录 |
| [docs/DESIGN.md](docs/DESIGN.md) | 架构设计：分层、状态机、F2P 与回归门、安全模型 |
| [docs/USAGE.md](docs/USAGE.md) | 使用手册与故障排查 |

**最适合第一次贡献的是「加一套夹具」**——它门槛低、能立刻在你自己关心的场景里验证
Fissue 是否可靠，而且夹具是这个项目最重要的资产。

> 提交贡献即表示同意以 [MIT 许可](LICENSE) 发布。

---

## 安全说明

沙盒默认 **禁网**、**非 root**、**只读根文件系统**、**限制 CPU/内存/PID/时长**，
且只挂载独立的隔离工作区（默认是普通临时目录，可选 loopback 虚拟盘）；起转发组件
（`fissue sandbox serve`）后，宿主与沙盒之间的执行走受控通道，避免不可信 PR
代码污染宿主机。

---

## 许可

MIT
