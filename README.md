# Fissue

> 自动从 **GitHub / Gitee / AtomGit / GitLab** 检索 Issue / PR，用 AI 大模型判断
> **真实性 / 重要性 / 可行性 / PR 质量**，生成验证器并在 Docker 沙盒中做 F2P 验证，
> 对「低难度 + 高重要性」的 Issue 自动修复并提 PR。

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
| 存储 | PostgreSQL（可导出 JSON / Markdown） |
| 沙盒 | Docker：禁网、限 CPU/内存/PID/时长、非 root、只读根、虚拟盘挂载、转发组件通道 |
| 配置 | `.env`（密钥）+ `config.yaml`（业务配置） |

---

## 快速开始

```bash
# 1. 安装
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. 准备配置
cp .env.example .env            # 填入 LLM key、数据库连接、平台 token
cp config.example.yaml config.yaml

# 3. 初始化数据库
fissue db init

# 4. 抓取仓库（先单仓库）
fissue fetch --repo psf/requests --platform github --limit 50

# 5. AI 评测
fissue eval --all

# 6. 生成验证器并在沙盒中做 F2P 验证
fissue verify --repo psf/requests

# 7. 手动 flush 队列（也可等数量阈值 / 空闲超时自动触发）
fissue flush --queue verify
fissue flush --queue fix

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
| `fissue db init` | 建表 / 迁移 |
| `fissue fetch` | 抓取 Issue/PR（增量） |
| `fissue eval` | AI 评测（四维评分 + 标签 + 反刷子） |
| `fissue verify` | 生成验证器 + 沙盒 F2P 验证 |
| `fissue flush` | 手动 flush 验证/修复队列 |
| `fissue fix` | 自动修复并提 PR（`--dry-run` 只产 patch） |
| `fissue report` | 生成 Markdown/JSON 报告 |
| `fissue export` | 导出全量数据 |
| `fissue serve` | 常驻服务（扫描 + 调度 + 通知） |
| `fissue web` | Web 仪表盘 + REST API |

---

## 目录结构

```
src/fissue/
  config.py       配置加载（.env + config.yaml）
  models.py       领域模型
  store/          PostgreSQL 持久化 + JSON/MD 导出
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
- ❌ `pipekit` 的跨模块根因定位**基本未被有效测量**（15 次 base error）；
- ❌ 真实提 PR 路径**从未端到端验证**；`lockkit` **完全未测量**；
- ⚠️ `envkit` 有 **5 处 tier 期望偏差**，公开保留未美化。

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
且只挂载独立的隔离虚拟盘；宿主与沙盒之间通过受控的转发组件通信，避免不可信 PR
代码污染宿主机。

---

## 许可

MIT
