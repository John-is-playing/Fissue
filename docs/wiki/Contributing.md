# 贡献指南

> 完整的贡献指南见 [CONTRIBUTING.md](../../CONTRIBUTING.md)；开发环境、测试与发布约定见[开发与发布](Development.md)。

---

## 0. 先理解 Fissue 的价值观

Fissue 是一个**判断「该不该修、有没有真修好」**的系统。因此它对贡献有一条比其他项目更严格的要求：

> **一切结论都要有可复核的证据。**
> 「我测过了」不算，要给出命令、输出、复现步骤。

这条同时适用于**代码 PR**（要能跑通测试）、**宣称的修复**（要有 F2P 证据）、以及**提 Issue**（要能复现）。
详见 [CODE_OF_CONDUCT.md](../../CODE_OF_CONDUCT.md) 里关于「刷量」的说明。

---

## 1. 开始之前

| 步骤 | 命令 |
|---|---|
| 读架构 | [架构设计](Architecture.md)（分层、状态机、安全模型） |
| 装环境 | 见[开发与发布 §开发环境](Development.md#1-开发环境) |
| 跑测试 | `python -m pytest -q`（离线，不需要 Key / Docker） |
| 找活干 | 带 `good first issue` 标签的 Issue |

> **请先开 Issue 对齐再动手。** 尤其是新功能与新平台适配器——避免你写完一个大 PR 却与项目方向不符。

---

## 2. 三条推荐的贡献路径

Fissue 的贡献者**就是它的用户**——你在自己仓库里用 Fissue 时最想要的东西，就是这个项目最需要的贡献。
以下按门槛从低到高排列。

### 路径 ① 加一套测试夹具（最适合第一次贡献）

夹具是 Fissue **最重要的资产**：**每套夹具都是对一个真实维护场景的对抗性模拟**，
用来验证系统在「假修复、只修一半、重复提交、刷量」面前是否仍然可靠。

一套新夹具的目录形态与**三个硬要求**见[扩展指南 §加一套测试夹具](Extending.md#5-加一套测试夹具门槛最低价值最高)：

1. 基线测试**必须全绿**，且**不覆盖**植入的缺陷。
2. **必须有对抗性样本**：至少一个「只修一半」的 PR、一个重复 Issue、一个刷量 Issue、一个误报/设计偏好 Issue。
3. **期望结果写进 `fixtures/*.md` 头部的 `target`**，对照表写进 README。

自检（不连网）：`cd demo/<yourkit>/repo && python -m pytest -q`（期望全绿）。

### 路径 ② 加一个平台适配器

继承 `PlatformAdapter`（v5 风格平台可直接继承 `V5Adapter`），实现 `_headers / fetch_items / fetch_diff / item_url`，
注册进 `platforms/registry.py:ADAPTERS`。详见[扩展指南 §加一个平台适配器](Extending.md#1-加一个平台适配器)。

### 路径 ③ 加一个验证器模式 / 缺陷模式（最有价值）

把一类反复出现的缺陷沉淀成可复用的验证器知识。比如 `lockkit` 里「资源不变量被破坏」这种——
**正常路径完全看不出来**，必须多还一次许可才能抓住。
如果你在真实仓库里遇到某一类缺陷反复出现，欢迎开 Issue 描述模式，并附上你的验证器写法。

### 其它

- **修 Bug / 补测试**：欢迎，请附带能复现的测试。
- **文档**：错别字、表达不清、示例跑不通，都值得提 PR。
- **不要**为了「贡献量」提交无实质内容的改动（见价值观那一节）。

---

## 3. 提交规范（摘要）

使用 Conventional Commits，说明文字用简体中文：

```
<type>: <中文说明>
```

`feat / fix / test / docs / refactor / chore`。完整说明与示例见[开发与发布 §提交规范](Development.md#4-提交规范)。

---

## 4. 提 PR（摘要）

- 从 `main` 拉分支，PR 目标为 `main`；填完 PR 模板（会引导你给出**验证证据**）。
- **必须**：`ruff check` 通过、`pytest` 全绿。
- **必须**：新增行为附带测试；修 Bug 附带回归测试。
- 一个 PR 只做一件事。

### 动了这些地方要格外小心

| 区域 | 为什么 |
|---|---|
| `sandbox/` | 安全边界。任何削弱隔离的改动都要在 PR 里说明威胁模型影响 |
| `pipeline/stages.py` 的重复检测 | 阈值 0.22 是实测标定的，改阈值必须附上标定数据 |
| `verifier/` | F2P 是产品的核心主张，改动会影响所有下游结论 |
| `fixer/` 的护栏 | 宿主侧护栏是最后一道防线 |
| `config.py` 的默认值 | 默认值会决定所有用户的初始行为 |

---

## 5. 报告 Bug

请用仓库的 [Bug 报告模板](https://github.com/John-is-playing/Fissue/issues/new?template=bug_report.yml)，并**尽量包含**：

- Fissue 版本（commit hash）与运行平台；
- 完整命令与实际输出（`--verbose` 的输出最有用）；
- 最小复现步骤；期望行为 vs 实际行为。

> 若问题与 LLM 输出有关，请注意：**模型输出是非确定性的**。单测只锁定「规则写对了」，
> 不锁定「模型每次照做」。报这类问题时，请附上原始响应（`data/artifacts/**` 里有）。

**安全漏洞请不要走公开 Issue**，见[安全模型 §报告漏洞](Security.md#7-报告漏洞)。

---

## 6. 常见坑

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前先 `git status` 确认干净 |
| Windows 上换行符 | 夹具仓库有 `.gitattributes` 强制 LF。混入 CRLF 会导致 `git apply` 失败 |
| 写文件用文本模式 | Windows 上 `write_text` 会把 `\n` 转 `\r\n`，破坏补丁/换行敏感产物 |
| 子进程编码 | 子进程调用**必须**显式 `encoding="utf-8"`（中文环境按 GBK 解码会乱码） |
| 全局 Python 缺 truststore | 中间人环境下必须用 `.venv` 的 Python |
| 缓存目录误命中 | `grep` 类工具可能命中 `.ruff_cache`、`__pycache__`，注意排除 |

---

## 7. 许可

提交贡献即表示你同意以本项目的 [MIT 许可](../../LICENSE) 发布你的贡献。

---

## 相关页

[扩展指南](Extending.md) · [开发与发布](Development.md) · [安全模型](Security.md) · [架构设计](Architecture.md) · [CONTRIBUTING.md](../../CONTRIBUTING.md)
