<!-- 提交前请读 CONTRIBUTING.md：一切结论都要有可复核的证据。「我测过了」不算，请给命令与输出。
     一个 PR 只做一件事。 -->
## 这个 PR 做了什么

<!-- 一句话说清，关联 Issue 用 Closes #N -->

Closes #

## 为什么要这么做

<!-- 解释「为什么」而不只是「做了什么」；反直觉的改动（阈值、默认值）请写明理由 -->

## 类型

- [ ] `fix` 修 Bug
- [ ] `feat` 新功能
- [ ] `test` 测试 / 夹具
- [ ] `docs` 文档
- [ ] `refactor` 重构（不改行为）
- [ ] `chore` 构建 / 依赖 / CI

## 验证证据（必填）

**1. 跑了什么命令**

```
python -m pytest -q
ruff check src tests
```

**2. 实际输出**（贴关键部分）

```
# 粘贴输出
```

**3. Bug 修复：怎么证明真修好了**（修复前失败 / 修复后通过，F2P 精神）

```
# 修复前的失败输出
# 修复后的通过输出
```

**4. 有没有可能弄坏别的**（会影响哪些现有行为、为什么不会回归）

## 改动范围

- 涉及文件：
- 新增/修改配置项：是 / 否
- 影响存储结构（`store/tables.py`）：是 / 否
- 影响 CLI 或 REST API：是 / 否

## 自查清单

- [ ] `ruff check src tests` 通过
- [ ] `pytest -q` 全绿（离线即可，无需 LLM Key / Docker）
- [ ] 新增行为带测试；修 Bug 带回归测试
- [ ] 提交信息用 Conventional Commits，说明为简体中文
- [ ] 未混入无关改动（如顺手 format 整个文件）

### 改动了敏感区域请额外确认

- [ ] 不适用
- [ ] `sandbox/`：已说明对威胁模型的影响
- [ ] `pipeline/stages.py` 重复检测阈值：已附标定数据（0.22 是实测值）
- [ ] `verifier/`：已说明对下游验证结论的影响
- [ ] `fixer/` 护栏：已说明护栏为何仍成立
- [ ] `config.py` 默认值：已说明对存量用户的影响

### 新增夹具（`demo/<name>/`）

- [ ] `cd demo/<name>/repo && python -m pytest -q` 全绿
- [ ] 基线测试刻意不覆盖植入缺陷
- [ ] 含四类对抗样本：只修一半的 PR / 重复 Issue / 刷量 Issue / 误报或设计偏好 Issue
- [ ] 期望结果写进 `fixtures/*.md` 头部 `target`
- [ ] README 写明「考什么」与期望结果对照表
- [ ] `repo/.gitattributes` 强制 LF（否则验证 PR 时 `git apply` 失败）

## 备注

<!-- reviewer 需要知道的：已知问题、后续 TODO、不确定之处 -->