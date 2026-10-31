# 贡献指南

> 一条硬规矩：**一切结论都要有可复核的证据**。「我测过了」不算，要给出命令与输出。
> 这条同时约束代码 PR、宣称的修复与 Issue 报告。

**动手前先开 Issue 对齐**——尤其新功能与新平台适配器，避免写完大 PR 方向不符。

---

## 开发环境

```bash
git clone https://github.com/John-is-playing/Fissue.git && cd Fissue
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash；Linux/macOS 用 .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env                 # 测试套件离线，不需要真实 Key
cp config.example.yaml config.yaml
```

```bash
python -m pytest -q                         # 全量
python -m pytest tests/test_pipeline.py -q  # 单文件
python -m pytest -q -k duplicate            # 按关键字
ruff check src tests && ruff format --check src tests
```

测试完全离线：`tests/conftest.py` 有网络守卫，任何外连会立即报错，不需要 Token、不需要 Docker。

---

## 三条贡献路径

### ① 加一套测试夹具 ⭐ 门槛最低

夹具是本项目最重要的资产：每套都是对真实维护场景的对抗性模拟，用来检验系统在假修复、
只修一半、重复提交、刷量面前是否仍然可靠。

| 夹具 | 领域 | 考什么 |
|---|---|---|
| `textkit` | 文本处理 | 单函数语义 |
| `ratekit` | 金额 / 费率 | 数值边界 |
| `pipekit` | 分片流水线 | 跨模块根因定位 |
| `envkit` | 配置 / 缓存 / 校验 | 状态副作用、环境依赖 |
| `lockkit` | 并发原语 / 资源管理 | 资源不变量、超时上限、一次性语义 |

一套新夹具的形态：

```
demo/<yourkit>/
├── README.md        # 这套夹具考什么 + 期望结果对照表
├── fixtures/        # issue-NN-<slug>.md，头部注释含 title / labels / target
├── repo/            # 待推送仓库（独立 git 仓库，被外层 .gitignore 忽略）
│   ├── <pkg>/       # 库源码（植入缺陷）
│   ├── tests/       # 基线测试（全绿，且刻意不覆盖缺陷）
│   └── pyproject.toml
└── scripts/setup_github.py
```

三条硬要求：

1. **基线测试全绿且不覆盖植入缺陷**——逼 Fissue 真读代码写验证器，而不是抄现成测试。
2. **必须有对抗样本**：至少一个只修一半的 PR、一个重复 Issue、一个刷量 Issue、一个误报或设计偏好 Issue。
3. **期望结果写进 `fixtures/*.md` 头部 `target`**，对照表写进 README。

自检：`cd demo/<yourkit>/repo && python -m pytest -q`（期望全绿）。

### ② 加一个平台适配器 ⭐

```
继承 PlatformAdapter → 实现 _headers / fetch_items / fetch_diff / item_url
                    → 注册进 platforms/registry.py:ADAPTERS
```

v5 风格（`/repos/:owner/:repo/...`）平台直接继承 `V5Adapter`。

### ③ 沉淀验证器 / 缺陷模式 ⭐⭐

最有价值的贡献：把反复出现的一类缺陷变成可复用的验证器知识。例如 `lockkit` 里
「资源不变量被破坏」——正常路径完全看不出来，必须多还一次许可才能抓住。欢迎开 Issue
描述模式并附验证器写法。

其它：修 Bug（附复现测试）、补文档（示例跑不通、表达不清）都欢迎。
**不要**为贡献量提交无实质内容的改动。

---

## 提交规范

[Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，说明用简体中文：

```
<type>: <中文说明>
```

`feat` 新功能 · `fix` 修 Bug · `test` 测试与夹具 · `docs` 文档 · `refactor` 重构 · `chore` 构建/依赖/CI

提交信息解释**为什么**，不只是做了什么。反直觉的改动（如重复检测阈值 0.22、回归门默认
`strict`）请写明理由——本项目代码里大量这类「为什么」注释，是刻意保持的风格。

---

## 提 PR

- 从 `main` 拉分支，PR 目标为 `main`；一个 PR 只做一件事
- 填 PR 模板并给出**验证证据**（命令 + 实际输出）
- **必须**：`ruff check` 通过、`pytest` 全绿
- **必须**：新增行为带测试，修 Bug 带回归测试

动了这些区域请格外小心：

| 区域 | 为什么 |
|---|---|
| `sandbox/` | 安全边界，削弱隔离须说明威胁模型影响 |
| `pipeline/stages.py` 重复检测 | 阈值 0.22 是实测标定，改必附标定数据 |
| `verifier/` | F2P 是核心主张，改动影响所有下游结论 |
| `fixer/` 护栏 | 宿主侧护栏是最后一道防线 |
| `config.py` 默认值 | 决定所有用户的初始行为 |

---

## 报告 Bug

用 [Bug 模板](https://github.com/John-is-playing/Fissue/issues/new?template=bug_report.yml)，尽量包含：
版本（commit hash）与平台、完整命令与输出（`--verbose` 最有用）、最小复现步骤、期望 vs 实际。

与 LLM 输出有关的问题：模型输出是非确定性的，单测只锁「规则写对了」，不锁「模型每次照做」；
请附原始响应（见 `data/artifacts/**`）。

**安全漏洞不要走公开 Issue**，见 [SECURITY.md](SECURITY.md)。

---

## 常见坑

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前先 `git status` |
| Windows 换行符 | 夹具仓库 `.gitattributes` 强制 LF；CRLF 会让验证 PR 时的 `git apply` 失败 |
| 全局 Python 缺 truststore | HTTPS 中间人环境下必须用虚拟环境的 Python，否则 TLS 失败 |
| 缓存目录干扰 | `ast_grep` / `grep` 可能命中 `.ruff_cache`、`__pycache__`，注意排除 |
| 缺 LLM Key | 单测不需要；`eval/verify/fix` 需要，报错信息明确 |

---

## 相关文档

[values → CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [架构 docs/DESIGN.md](docs/DESIGN.md) ·
[使用手册 docs/USAGE.md](docs/USAGE.md) · [夹具索引 demo/README.md](demo/README.md) ·
[分诊 TRIAGE.md](TRIAGE.md) · [基准 BENCHMARK.md](BENCHMARK.md)

提交贡献即表示同意以 [MIT 许可](LICENSE) 发布。