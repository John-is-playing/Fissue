# 验证器与 F2P

> 验证器与 F2P 是 Fissue 的**核心判据**：它决定了「这条 Issue 是否真实存在」「这个修复是否真的有效」。
> 完整规范见 [docs/DESIGN.md §4](../DESIGN.md#4-验证器与-f2pq7) 与 [docs/REGRESSION-GATE.md](../REGRESSION-GATE.md)。

---

## 1. F2P（fail-to-pass）

```
base 阶段：在**未修复**代码上跑 → 必须失败（exit≠0）→ 证明问题真实存在
fix  阶段：在**修复/合并后**代码上跑 → 必须通过（exit=0）→ 证明修复有效
```

只有两者同时成立才算 `f2p_satisfied`。

### 防御性设计

| 情形 | 处置 |
|---|---|
| **base 就通过** | 判定「验证器不可靠」→ 触发 LLM 修正（最多 `verifier.max_rounds` 轮）；仍不可靠则标 `needs_manual`，**绝不据此自动修码** |
| **base 直接 ERROR / TIMEOUT** | 结论不可信，**不进入**自动修复 |
| **无测试框架** | 降级为 `checklist` 型，由 LLM 逐条判读执行输出；也可直接用 `fissue verify` 人工复核 |

### 验证器模式

```yaml
verifier:
  mode: hybrid            # executable | checklist | hybrid
  require_f2p: true
  max_rounds: 2           # 生成验证器后自校正的最大轮次
  allow_checklist_fallback: true
```

### 验证器命令的来源

验证器**命令固定化，不许 LLM 现编**。回归门的命令优先级为
`verifier.regression_command` → `repo.test_hint` → 探测（`pyproject.toml`/`pytest.ini` → pytest；
`package.json` → npm test；`go.mod` → go test…）→ 探不到就跳过并记 `skipped`。

> 别忘了给仓库写 `test_hint`：AI 猜不准测试方式时，一条提示就能显著提升验证器质量。

---

## 2. 回归门（Regression Gate）

### 2.1 为什么需要它

F2P 只表达**一个**方向：目标用例修复前失败、修复后通过。它**表达不了**「别弄坏别的」——
即 pass-to-pass（回归）要求。后果是实测过的（`textkit` 夹具 issue #1）：

| | `word_count("")` | `word_count("hello  world")` |
|---|---|---|
| 修复前（`split(" ")`） | 1（目标问题） | **3**（既有语义） |
| 修复后（`split()`） | 0（目标达成） | **2**（语义被改） |

正文写明「其余用例不受影响」，但验证器只把列出的目标用例翻成断言，而两种修法在目标用例上**表现完全相同**（都返回 0），差异全在未列举的连续空格上。
于是「改对了目标、却弄坏了别处」的修复照样通过 F2P。

**补上这个方向的唯一可靠判据是仓库自己的既有测试套件。**

### 2.2 判定矩阵（最容易做错的地方）

| base 既有测试 | fix 既有测试 | 结论 |
|---|---|---|
| 通过 | 通过 | 可继续 |
| 通过 | **失败** | **拒绝**：修复破坏了既有行为（本门要拦的正是它） |
| **失败** | — | 「仓库本身不绿 → 本门不可信」：记 `warn` 并**放行**，绝不据此拒绝 |
| ERROR / TIMEOUT | — | 同「不绿」：环境/依赖问题，不可归咎于修复 |

> **第 3 行是安全底线**：老仓库普遍存在本来就失败/跳过的测试，或沙盒内依赖没装全。
> 若把「base 不绿」当成修复的错，会把**所有**条目误杀，闸门反而变成故障源。

### 2.3 三档模式

```yaml
verifier:
  regression_gate: strict             # off | warn | strict（默认）
  regression_command: null            # 留空 → repo.test_hint → 探测
  regression_timeout_seconds: 900      # 独立预算，通常远大于单测验证器（后者默认 600）
```

| 档位 | 行为 | 适用 |
|---|---|---|
| `off` | 完全不跑，零开销 | 不关心回归 / 仓库没测试 |
| `warn` | 跑、记录日志、报告里标注，但**不改变**结论 | 想先观察「本就红」的比例时 |
| `strict`（默认） | base 绿而 fix 不绿 → **拒绝**修复并转人工 | 默认档 |

**推荐姿势**：保持默认的 `strict`——它只在「改之前绿、改之后红」时拦人，而「base 本来就不绿」的情形已被安全底线放行。
想统计有多少仓库本来就红，可临时切到 `warn` 观察几天。

### 2.4 实现要点（改代码前必读）

- **不新建工作区**：回归门只是在同一工作区上**多跑一条命令**。成本随**运行时间**增长，与 Issue 数、LLM token **无关**。
- **必须排除验证器自己写入的文件**：验证器会把测试文件写进工作区（`spec.files`，如 `tests/test_reproduce.py`），
  跑全量套件时会被 pytest 一起收集。实现上建议**基于 git 解析**（只跑 `git ls-files` 里的测试文件），
  并在跑之前把 `spec.files` 路径**临时移出**（`try/finally` 保证恢复），因为 pytest 默认会扫到未跟踪文件。
- **必须走沙盒通道**（`SandboxManager` → `ExecRequest`），**不得**直接 `subprocess` 跑在宿主上。
- **独立 stage 标识**（如 `regression:base` / `regression:fix`）落库，便于与 `base`/`fix` 区分。
- **超时用 `regression_timeout_seconds`**，**不要**沿用 `spec.timeout_seconds`（后者是单测验证器的预算，通常远小于全量套件）。
- **`judge_f2p` 的语义不要改**：F2P 与回归门是两个独立判据，混在一起会破坏现有测试对
  `f2p_satisfied / reproducible / fixed` 的契约。

### 2.5 PR 流水线的顺序契约

`PRVerifyStage` 中，**base 相关的一切都在 `_merge_pr` 之前**：base 验证器 → base 回归门（期望通过）→ 合并 PR → fix 验证器 → fix 回归门。
回归门需要判定「合并前」的状态，顺序颠倒会拿到错误的 base。

---

## 3. 判定与落地路径

| 结论 | 落地 |
|---|---|
| F2P 成立 + 回归门通过 | 可继续：Issue 侧 → tier1/tier2 进自动修复；PR 侧 → 打标签建议合并 |
| F2P 不成立 | 假修复识破 → 不合并 / 不修 |
| 回归门 `strict` 拒绝 | 走既有失败路径：`needs_manual` + `data/manual/<key>/report.md`；结论**必须点名哪一条测试挂了** |
| `needs_manual` / `failed` | 用 `fissue requeue --failed` 人工放回 |

> 修复侧还有一次补刀：`auto_fix.regression_retries`（默认 1）会在回归门失败后，
> 带着「哪几条用例被改坏」**重跑修复**——实测里这通常一两轮就能改对。

---

## 相关页

[架构设计](Architecture.md) · [AI 层](AI-Layer.md) · [基准与可信度](Benchmark.md) · [安全模型](Security.md) · [docs/REGRESSION-GATE.md](../REGRESSION-GATE.md)
