# 验证器与 F2P

> 核心判据：它决定「这条 Issue 是否真实存在」「这个修复是否真的有效」。
> 完整规范见 [DESIGN.md §4](../DESIGN.md) 与 [REGRESSION-GATE.md](../REGRESSION-GATE.md)。

## 1. F2P（fail-to-pass）

```
base：未修复代码上跑 → 必须失败（exit≠0）→ 问题真实存在
fix ：修复/合并后跑   → 必须通过（exit=0）→ 修复有效
```

两者同时成立才算 `f2p_satisfied`。

| 情形 | 处置 |
|---|---|
| **base 就通过** | 判「验证器不可靠」→ LLM 修正（最多 `verifier.max_rounds` 轮）；仍不行标 `needs_manual`，**绝不据此自动修码** |
| **base ERROR / TIMEOUT** | 结论不可信，不进入自动修复 |
| **无测试框架** | 降级 `checklist`，LLM 按证据逐条判读；也可 `fissue verify` 人工复核 |

```yaml
verifier:
  mode: hybrid            # executable | checklist | hybrid
  require_f2p: true
  max_rounds: 2
  allow_checklist_fallback: true
```

**验证器命令固定化，不许 LLM 现编**。回归门命令优先级：`verifier.regression_command` →
`repo.test_hint` → 探测（`pyproject.toml`/`pytest.ini` → pytest；`package.json` → npm test；
`go.mod` → go test）→ 探不到跳过并记 `skipped`。

> 给仓库写 `test_hint` 能显著提升验证器质量——AI 猜不准测试方式时，一条提示就够。

## 2. 回归门

### 2.1 为什么需要

F2P 只表达一个方向，表达不了「别弄坏别的」——实测反例（`textkit` issue #1）：

| | `word_count("")` | `word_count("hello  world")` |
|---|---|---|
| 修复前 `split(" ")` | 1（目标问题） | **3**（既有语义） |
| 修复后 `split()` | 0（目标达成） | **2**（语义被改） |

两种修法在目标用例上表现完全相同，差异全在未列举的连续空格上。正文虽写「其余用例不受影响」，
验证器只把列出的目标用例翻成断言。补上这个方向的唯一可靠判据是**仓库自己的既有测试套件**。

### 2.2 判定矩阵

| base 既有测试 | fix 既有测试 | 结论 |
|---|---|---|
| 通过 | 通过 | 可继续 |
| 通过 | **失败** | **拒绝**（本门要拦的正是它） |
| **失败** | — | 仓库本身不绿 → 本门不可信，记 `warn` 并**放行** |
| ERROR / TIMEOUT | — | 同「不绿」：环境/依赖问题，不可归咎于修复 |

> 第 3 行是**安全底线**：老仓库普遍有本来就失败的测试，或沙盒内依赖没装全。把「base 不绿」
> 当成修复的错会把所有条目误杀，闸门反而变成故障源。

### 2.3 三档

```yaml
verifier:
  regression_gate: strict             # off | warn | strict（默认）
  regression_command: null
  regression_timeout_seconds: 900     # 独立预算，通常远大于单测验证器（默认 600）
```

| 档位 | 行为 |
|---|---|
| `off` | 不跑，零开销 |
| `warn` | 跑、记录、报告标注，不改变结论（先观察「本就红」比例） |
| `strict`（默认） | base 绿而 fix 不绿 → 拒绝并转人工 |

推荐保持 `strict`：它只在「改之前绿、改之后红」时拦人，误杀风险已被安全底线兜住。

### 2.4 实现要点

- **不新建工作区**：只是在同一工作区多跑一条命令，成本随运行时间增长，与 Issue 数、token 无关。
- **排除验证器自己写入的文件**：`spec.files`（如 `tests/test_reproduce.py`）会被 pytest 收集。
  按 `git ls-files` 限定，并在跑前把 `spec.files` 临时移出（`try/finally` 恢复）。
- **必须走沙盒通道**（`SandboxManager` → `ExecRequest`），不得直接 `subprocess` 跑宿主。
- **独立 stage 标识**（`regression:base` / `regression:fix`）落库；超时用 `regression_timeout_seconds`。
- **`judge_f2p` 语义不要改**：两个判据混在一起会破坏 `f2p_satisfied / reproducible / fixed` 契约。

### 2.5 PR 顺序契约

`PRVerifyStage` 中 **base 相关的一切都在 `_merge_pr` 之前**：base 验证器 → base 回归门 →
合并 PR → fix 验证器 → fix 回归门。顺序颠倒会拿到错误的 base。

## 3. 判定落地

| 结论 | 落地 |
|---|---|
| F2P 成立 + 回归门通过 | Issue 侧 → tier1/tier2 自动修复；PR 侧 → 打标签建议合并 |
| F2P 不成立 | 假修复识破 → 不合并 / 不修 |
| 回归门 strict 拒绝 | `needs_manual` + `data/manual/<key>/report.md`，结论文案**必须点名哪条测试挂了** |
| `needs_manual` / `failed` | `fissue requeue --failed` 人工放回 |

> 修复侧还有一次补刀：`auto_fix.regression_retries`（默认 1）在回归门失败后带着「哪几条用例
> 被改坏」重跑修复，实测通常一两轮就能改对。

---

相关：[架构设计](Architecture.md) · [AI 层](AI-Layer.md) · [基准与可信度](Benchmark.md) ·
[安全模型](Security.md) · [REGRESSION-GATE.md](../REGRESSION-GATE.md)