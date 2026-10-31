# 贡献指南

> 完整版见 [CONTRIBUTING.md](../../CONTRIBUTING.md)；开发环境、CI 与发布约定见[开发与发布](Development.md)。

## 价值观

Fissue 判断「该不该修、有没有真修好」，因此要求比其他项目更严格：

> **一切结论都要有可复核的证据。**「我测过了」不算，要给出命令、输出、复现步骤。

适用于代码 PR、宣称的修复（要 F2P 证据）与 Issue 报告（要能复现）。
**先开 Issue 对齐再动手**，尤其新功能与新平台适配器。

## 三条路径

| 路径 | 门槛 | 内容 |
|---|---|---|
| ① 加一套夹具 | 最低 | 对抗性样本仓库，见[扩展指南](Extending.md) |
| ② 加平台适配器 | 中 | 继承 `PlatformAdapter`（v5 风格继承 `V5Adapter`），实现 `_headers / fetch_items / fetch_diff / item_url`，注册进 `platforms/registry.py:ADAPTERS` |
| ③ 沉淀验证器模式 | 高，最有价值 | 把反复出现的缺陷变成可复用的验证器知识（如 `lockkit` 的「资源不变量被破坏」，正常路径看不出来） |

其它：修 Bug（附复现测试）、补文档都欢迎。**不要**为「贡献量」提交无实质内容的改动。

**夹具三个硬要求**：基线测试全绿且**不覆盖**植入缺陷 · 必须有对抗样本（只修一半的 PR、重复
Issue、刷量 Issue、误报/设计偏好 Issue）· 期望结果写进 `fixtures/*.md` 的 `target` 并在 README
写对照表。自检：`cd demo/<yourkit>/repo && python -m pytest -q`。

## 提交与 PR

Conventional Commits，说明用简体中文：`<type>: <中文说明>`，`feat / fix / test / docs / refactor / chore`。
提交信息解释**为什么**，反直觉的改动要写理由。

- 从 `main` 拉分支，目标 `main`；一个 PR 只做一件事
- 填 PR 模板并给出**验证证据**（命令 + 输出，修复前后 F2P 证据，回归考虑）
- 必须：`ruff check` 通过、`pytest` 全绿；新增行为带测试，修 Bug 带回归测试

| 敏感区域 | 为什么 |
|---|---|
| `sandbox/` | 安全边界，削弱隔离须说明威胁模型影响 |
| `pipeline/stages.py` 重复检测 | 阈值 0.22 是实测标定，改必附数据 |
| `verifier/` | F2P 是核心主张，影响所有下游结论 |
| `fixer/` 护栏 | 宿主侧最后一道防线 |
| `config.py` 默认值 | 决定所有用户初始行为 |

## 报 Bug 与常见坑

报 Bug 请给版本 commit、平台、完整命令与输出（`--verbose` 最有用）、最小复现、期望 vs 实际。
与 LLM 输出有关的问题：模型输出非确定，单测只锁「规则写对了」，请附原始响应（`data/artifacts/**`）。
**安全漏洞不要走公开 Issue**，见[安全模型](Security.md)。

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前 `git status` |
| Windows 换行符 | 夹具 `.gitattributes` 强制 LF，CRLF 会让 `git apply` 失败 |
| 文本模式写文件 | `write_text` 会把 `\n` 转 `\r\n`，破坏补丁——参照 `_write_patch` 用二进制 |
| 子进程编码 | 必须显式 `encoding="utf-8"`（中文环境按 GBK 解码会乱码） |
| 全局 Python 缺 truststore | 中间人环境必须用虚拟环境的 Python |
| 缓存目录误命中 | `grep` 可能命中 `.ruff_cache`、`__pycache__` |

提交贡献即表示同意以 [MIT 许可](../../LICENSE) 发布。

---

相关：[扩展指南](Extending.md) · [开发与发布](Development.md) · [安全模型](Security.md) ·
[架构设计](Architecture.md) · [CONTRIBUTING.md](../../CONTRIBUTING.md)