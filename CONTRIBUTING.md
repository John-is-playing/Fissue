# 贡献指南

感谢你有兴趣参与 Fissue。

在动手之前，请先读这一节——它能让你少走很多弯路。

---

## 0. 先理解 Fissue 的价值观

Fissue 是一个**判断"该不该修、有没有真修好"**的系统。因此本项目对贡献有一条
比其他项目更严格的要求：

> **一切结论都要有可复核的证据。**
> 「我测过了」不算，要给出命令、输出、复现步骤。

这条同时适用于**代码 PR**（要能跑通测试）、**宣称的修复**（要有 F2P 证据）、
以及**提 Issue**（要能复现）。详见 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
里关于"刷量"的说明。

---

## 1. 开始之前

| 步骤 | 命令 |
|---|---|
| 读架构 | [`docs/DESIGN.md`](docs/DESIGN.md)（分层、状态机、安全模型） |
| 装环境 | 见第 2 节 |
| 跑测试 | `.venv/Scripts/python.exe -m pytest -q`（Windows）／`.venv/bin/python -m pytest -q` |
| 找活干 | 带 `good first issue` 标签的 Issue |

**⚠️ 请先开 Issue 对齐再动手。** 尤其是新功能与新平台适配器——
避免你写完一个大 PR 却与项目方向不符（详见第 3 节的接口冻结说明）。

---

## 2. 开发环境

```bash
git clone https://github.com/John-is-playing/Fissue.git
cd Fissue

python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash
# source .venv/bin/activate          # Linux / macOS

pip install -e ".[dev]"
```

### 最小可跑配置（不需要 LLM Key、不需要 Docker）

```bash
cp .env.example .env                 # 测试套件不连网、不需要真实 Key
cp config.example.yaml config.yaml
```

跑测试：

```bash
python -m pytest -q                          # 全量
python -m pytest tests/test_pipeline.py -q   # 单文件
python -m pytest -q -k "duplicate"           # 按关键字
```

**测试套件完全离线**：`tests/conftest.py` 有网络守卫，任何外连会立刻以清晰的
报错暴露，而不是伪装成"跑得慢"。所以不需要配 Token、不需要起 Docker。

代码检查：

```bash
ruff check src tests
ruff format --check src tests
```

---

## 3. 三条推荐的贡献路径

Fissue 的贡献者**就是它的用户**——你在自己仓库里用 Fissue 时最想要的东西，
就是这个项目最需要的贡献。以下三条路径按门槛从低到高排列。

### 路径 ① 加一套测试夹具 ⭐ 最适合第一次贡献

夹具是 Fissue 最重要的资产：**每套夹具都是对一个真实维护场景的对抗性模拟**，
用来验证系统在"假修复、只修一半、重复提交、刷量"面前是否仍然可靠。

现有五套（见 `demo/`）：

| 夹具 | 领域 | 考什么 |
|---|---|---|
| `textkit` | 文本处理 | 单函数语义 |
| `ratekit` | 金额 / 费率 | 数值边界 |
| `pipekit` | 分片流水线 | 跨模块根因定位 |
| `envkit` | 配置 / 缓存 / 校验 | 状态副作用、环境依赖 |
| `lockkit` | 并发原语 / 资源管理 | 资源不变量、超时上限、一次性语义 |

一套新夹具需要：

```
demo/<yourkit>/
├── README.md        # 说明"这套夹具考什么"与期望结果对照表
├── fixtures/        # issue-NN-<slug>.md，头部注释含 title / labels / target
├── repo/            # 待推送的测试仓库（独立 git 仓库，被外层 .gitignore 忽略）
│   ├── <pkg>/       # 库源码（植入若干缺陷）
│   ├── tests/       # 基线测试（必须全绿，且刻意不覆盖那些缺陷）
│   └── pyproject.toml
└── scripts/setup_github.py
```

夹具设计的**三个硬要求**：

1. **基线测试必须全绿**，且**不覆盖**植入的缺陷——逼 Fissue 真去读代码写验证器，
   而不是抄现成测试。
2. **必须有对抗性样本**：至少一个"只修一半"的 PR、一个重复 Issue、一个刷量 Issue、
   一个误报/设计偏好 Issue。
3. **期望结果要写进 `fixtures/*.md` 头部的 `target`**，对照表写进 README。

自检（不连网）：

```bash
cd demo/<yourkit>/repo && python -m pytest -q     # 期望全绿
```

### 路径 ② 加一个平台适配器 ⭐

想用 Fissue 管你公司的 Gitea / 自建 GitLab？自己加最直接。

```
继承 PlatformAdapter → 实现 _headers / fetch_items / fetch_diff / item_url
                    → 注册进 platforms/registry.py:ADAPTERS
```

若目标平台是 v5 风格（`/repos/:owner/:repo/...`），直接继承 `V5Adapter` 即可。

### 路径 ③ 加一个验证器模式 / 缺陷模式 ⭐⭐

这是**最有价值**的贡献：把一类反复出现的缺陷沉淀成可复用的验证器知识。
比如 lockkit 里"资源不变量被破坏"这种——正常路径完全看不出来，
必须多还一次许可才能抓住。

如果你在真实仓库里遇到某一类缺陷反复出现，欢迎开 Issue 描述模式，
并附上你的验证器写法。

### 其它

- **修 Bug / 补测试**：欢迎，请附带能复现的测试。
- **文档**：错别字、表达不清、示例跑不通，都值得提 PR。
- **不要**为了"贡献量"提交无实质内容的改动（见价值观那一节）。

---

## 4. 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，
说明文字用**简体中文**：

```
<type>: <中文说明>
```

常用 `type`：

| type | 用于 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 Bug |
| `test` | 测试与夹具 |
| `docs` | 文档 |
| `refactor` | 重构（不改行为） |
| `chore` | 构建 / 依赖 / CI |

示例：

```
fix: 中文标题的重复预筛改用 bigram 与包含度
test: 新增 lockkit 纯净测试夹具（15 Issue + 5 PR）
docs: 补记 C/D/E/F 的修复过程并收敛遗留清单
```

**好的提交信息解释"为什么"，不只是"做了什么"。** 如果某个改动看起来反直觉
（例如"为什么回归门默认 warn 而不是 strict"），请在提交信息或代码注释里写明
理由——这个项目的代码里有大量这类"为什么"注释，是本项目的风格。

---

## 5. 提 PR

- 从 `main` 拉分支，PR 目标为 `main`
- 填完 PR 模板（会引导你给出验证证据）
- **必须**：`ruff check` 通过、`pytest` 全绿
- **必须**：新增行为附带测试；修 Bug 附带回归测试
- 一个 PR 只做一件事，不要混入无关改动

### 如果动了这些地方，请格外小心

| 区域 | 为什么 |
|---|---|
| `sandbox/` | 安全边界。任何削弱隔离的改动都要在 PR 里说明威胁模型影响 |
| `pipeline/stages.py` 的重复检测 | 阈值 0.22 是实测标定的，改阈值必须附上标定数据 |
| `verifier/` | F2P 是产品的核心主张，改动会影响所有下游结论 |
| `fixer/` 的护栏 | 宿主侧护栏是最后一道防线 |
| `config.py` 的默认值 | 默认值会决定所有用户的初始行为 |

---

## 6. 报告 Bug

请用仓库的 [Bug 报告模板](https://github.com/John-is-playing/Fissue/issues/new?template=bug_report.yml)，
并**尽量包含**：

- Fissue 版本（commit hash）与运行平台
- 完整命令与实际输出（`--verbose` 的输出最有用）
- 最小复现步骤
- 期望行为 vs 实际行为

> 如果问题与 LLM 输出有关，请注意：**模型输出是非确定性的**。
> 单测只锁定"规则写对了"，不锁定"模型每次照做"。
> 报这类问题时，请附上原始响应（`data/artifacts/**` 里有）。

**安全漏洞请不要走公开 Issue**，见 [`SECURITY.md`](SECURITY.md)。

---

## 7. 常见坑

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前先 `git status` 确认干净 |
| Windows 上换行符 | 夹具仓库有 `.gitattributes` 强制 LF。混入 CRLF 会导致 `git apply` 失败（Fissue 验证 PR 时依赖它） |
| 全局 Python 缺 truststore | 本机若处于 HTTPS 中间人环境，**必须**用 `.venv` 里的 Python，否则 TLS 失败 |
| pathlib 缓存 | `ast_grep` / `grep` 类工具可能命中 `.ruff_cache`、`__pycache__`，注意排除 |
| LLM Key 缺失 | 单测不需要；但 `fissue eval/verify/fix` 需要，报错信息很明确 |

---

## 8. 许可

提交贡献即表示你同意以本项目的 [MIT 许可](LICENSE) 发布你的贡献。

---

再一次：**先开 Issue 对齐，再动手。** 这会让我们双方都省很多时间。
