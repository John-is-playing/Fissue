# Fissue

> 自动抓取 **GitHub / Gitee / AtomGit / GitLab** 的 Issue 与 PR，用 AI 判断
> **真实性 / 重要性 / 可行性 / PR 质量**，生成验证器在 Docker 沙盒里做 F2P 验证，
> 对「低难度 + 高重要性」的 Issue 自动修复并提 PR——**永不自动合并**。

维护仓库最痛的不是写代码，而是面对一堆 Issue/PR 不知道先看哪个：真 Bug 还是误用？
真需求还是重复/刷量？这个 PR 真修好了吗？Fissue 把「分诊 → 验证 → 修复」自动化，
并留下完整评分理由与证据链供人复核。

> 核心主张：**别信模型的自述，要看独立证据。** 本项目绝大多数代码由 AI Agent 驱动编写，请带此前提阅读。

---

## 快速开始

```bash
# 1. 安装（Python 3.10+）
python -m venv .venv && source .venv/Scripts/activate   # Linux/macOS 用 .venv/bin/activate
pip install -e ".[dev]"

# 2. 配置：密钥进 .env，业务规则进 config.yaml
cp .env.example .env            # 填 LLM key 与平台 token（默认 SQLite，可不动）
cp config.example.yaml config.yaml

# 3. 建库 → 抓取 → 评测 → 验证
fissue db init
fissue fetch --repo psf/requests --platform github --limit 50
fissue eval --all
fissue verify --repo psf/requests

# 4. 批量定论 → 自动修复（先 --dry-run 只看补丁）
fissue flush --queue verify
fissue fix --dry-run
fissue fix

# 5. 报告 / 常驻 / Web
fissue report --format markdown --out data/exports/report.md
fissue serve --with-web
```

一次配好全部：`fissue onboard`（交互式向导；无头机器用 `--yes` 加参数）。

---

## 核心流程

```
抓取（四平台，增量）→ 元数据预筛 + AI 分类 BUG / FEATURE
  ├─ Issue·BUG  : 评难度 → 写验证器 → 沙盒 base 验证 → verify 队列 → 批量定论
  │                → tier1/tier2 自动修复提 PR；其余打标签等开发者
  ├─ Issue·FEAT : 评可行性/难度/必要性 → 打标签
  └─ PR·BUG|FEAT: 判提交者 → 找关联 Issue → 写验证器 → 沙盒合并验证 → fix 队列
                  → 批量定论 → 打标签建议合并，由人手动合并
```

两条闸门缺一不可：**F2P**（修复前失败、修复后通过）与**回归门**（仓库既有测试
改前绿、改后仍绿）。详见 [docs/DESIGN.md](docs/DESIGN.md)。

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `fissue onboard` | 初始化向导（模型 / 数据库 / 平台凭据 / 监控仓库，并抓第一轮） |
| `fissue db init` · `db check` | 建表迁移 · 连通性与 DDL 自检 |
| `fissue fetch` | 抓取 Issue/PR（增量） |
| `fissue eval` | AI 评测：四维评分 + 标签 + 反刷子 |
| `fissue verify` | 生成验证器 + 沙盒 F2P 验证 |
| `fissue flush` | 手动 flush 验证 / 修复队列 |
| `fissue fix` | 自动修复并提 PR（`--dry-run` 只产 patch） |
| `fissue requeue` | 把卡住条目放回流水线（`--stale` / `--failed`） |
| `fissue force` | 无视评测结论强制推进修复（`--dry-run` / `--yes` / `--undo`） |
| `fissue report` · `export` | Markdown/JSON 报告 · 全量导出 |
| `fissue status` | 条目统计、队列、模型用量、沙盒可用性 |
| `fissue platforms doctor` | 平台能力体检：只读/克隆/打标签/评论/推分支（写探测需显式开启） |
| `fissue serve` | 常驻：增量扫描 + 调度 + 验证 + 通知（`--with-web` 同进程带 Web） |
| `fissue web` | Web 工作台 + `/api/v1` REST API |
| `fissue sandbox serve` · `check` | 沙盒转发组件（执行唯一通道） · 环境就绪检查 |
| `fissue auth ...` | Web/API 身份：`status` / `set-password` / `reset` / `key list\|create\|revoke` |
| `fissue bench capture\|diff\|list` | 跑分基线冻结与逐项对比 |

---

## 技术栈

| 项 | 选型 |
|---|---|
| 语言 / 交付 | Python 3.10+；CLI（typer）+ Web（FastAPI + Jinja2）+ REST API + 常驻服务 |
| 平台 | GitHub · Gitee · AtomGit · GitLab（含自建实例） |
| AI | 任意 OpenAI 兼容端点（DeepSeek / OpenAI / 通义 / Kimi / 本地 vLLM…） |
| 存储 | SQLite（默认，零依赖）· PostgreSQL / MySQL（需显式配置，均已真机验证部署路径）；可导出 JSON / Markdown |
| 沙盒 | Docker：禁网、非 root、只读根、限 CPU/内存/PID/时长，仅挂隔离工作区，可走受控转发通道 |
| 配置 | `.env`（密钥）+ `config.yaml`（业务规则）+ `config.local.yaml`（覆盖层） |

---

## 目录结构

```
src/fissue/
  config.py      .env + config.yaml 加载与校验
  models.py      领域模型
  store/         SQLAlchemy 持久化 + JSON/MD 导出
  platforms/     GitHub / Gitee / AtomGit / GitLab 适配器
  ai/            客户端、预算、提示词、评估器
  verifier/      验证器生成与 F2P / 回归门执行
  sandbox/       Docker 运行时与转发组件
  pipeline/      抓取、阶段、队列、flush、上下文
  fixer/         受控修复循环与提 PR
  service/       常驻调度与通知
  web/           REST API 与仪表盘
  cli/           命令行入口
demo/            五套对抗性夹具（textkit / ratekit / pipekit / envkit / lockkit）
docs/            设计、使用、环境变量、鉴权、回归门 + wiki/
```

---

## 它靠谱吗

[**BENCHMARK.md**](BENCHMARK.md) 公开了在五套对抗性夹具（60 Issue + 20 PR）上的真实测量：

- ✅ 回归门在 3 套夹具上全部拦下「修好目标却弄坏别处」的假修复；
- ✅ 假修复、刷量、误报、设计偏好变更均被拦在自动修复之外；
- ✅ 真实提 PR 已端到端跑通（`envkit#4` → PR #31、`lockkit#2` → PR #28），改动不含验证器测试文件；
- ✅ `pipekit` Issue 侧自动修复 7/7（2~3 轮内产出补丁）；早先「跨模块根因定位拿不到正例」的结论已被自我推翻；
- ⚠️ Gitee 条目级写只剩「建/改 Issue」不可用（404，原因未定位），打标签、评论、仓库级写均通；
- ⚠️ tier 标定是阈值边界抖动而非稳定清单；功能后果为零（tier1/tier2 都进修复队列，只差标签）。

我们宁可把这些写出来，也不给一份「全部通过」的漂亮表格。

---

## 文档地图

| 读者 | 入口 |
|---|---|
| 第一次接触 | [docs/wiki/Overview.md](docs/wiki/Overview.md) → [docs/wiki/Getting-Started.md](docs/wiki/Getting-Started.md) |
| 配环境 / 查变量 | [docs/USAGE.md](docs/USAGE.md) · [docs/ENV.md](docs/ENV.md) · [docs/AUTH.md](docs/AUTH.md) |
| 查命令 / 接口 | [docs/wiki/CLI-Reference.md](docs/wiki/CLI-Reference.md) · [docs/wiki/Web-and-API.md](docs/wiki/Web-and-API.md) |
| 改代码 / 加扩展 | [docs/DESIGN.md](docs/DESIGN.md) · [docs/wiki/Architecture.md](docs/wiki/Architecture.md) · [docs/wiki/Extending.md](docs/wiki/Extending.md) |
| 看可信度 | [BENCHMARK.md](BENCHMARK.md) · [docs/REGRESSION-GATE.md](docs/REGRESSION-GATE.md) |
| 部署 / 安全 | [docs/wiki/Deployment.md](docs/wiki/Deployment.md) · [SECURITY.md](SECURITY.md) |
| 参与贡献 | [CONTRIBUTING.md](CONTRIBUTING.md) · [TRIAGE.md](TRIAGE.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |

完整导航见 [docs/wiki/Home.md](docs/wiki/Home.md)。

---

## 安全与许可

沙盒默认禁网、非 root、只读根、限资源，只挂隔离工作区；Docker 不可用时**默认拒绝执行**，
不静默降级。威胁模型与护栏见 [SECURITY.md](SECURITY.md)。

[MIT](LICENSE) · 提交贡献即同意以 MIT 发布。