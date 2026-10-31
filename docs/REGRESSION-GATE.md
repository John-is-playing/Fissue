# 既有测试回归门（Regression Gate）

> **已实现**：`verifier/regression.py`（纯函数 `judge_regression`）、`pipeline/stages.py`（接入
> base / fix 两条流水线）、`fixer/autofix.py`（拦下后返修）、测试 `tests/test_regression_gate.py`。
> 建议先读 [DESIGN.md §4](DESIGN.md) 了解 F2P 与验证器体系。

## 1. 为什么需要它

F2P 只表达一个方向：目标用例修复前失败、修复后通过。它**表达不了「别弄坏别的」**。实测例子：

| | `word_count("")` | `word_count("hello  world")` |
|---|---|---|
| 修复前 `split(" ")` | 1 ❌ 目标问题 | **3**（既有语义） |
| 修复后 `split()` | 0 ✅ 目标达成 | **2** ⚠️ 语义被改 |

两种修法在目标用例上表现完全相同，差异全在未列举的连续空格上。而 issue 正文写着「其余用例
不受影响」——但验证器只把列出的目标用例翻成断言。

这是与验证器提示词**互补而非替代**：提示词能覆盖 issue 作者显式写出的约束；真实仓库里海量的
历史行为，唯一可靠判据是**仓库自己的测试套件**。

## 2. 与非目标

**目标**：加一道「既有测试必须仍然全绿」的闸门；成本随**运行时间**增长而与 Issue 数、token
无关；base / fix 两个状态都跑；不误伤本来就不绿的仓库。

**非目标**：不做跨 Issue 的语义裁决（成本不可控）；不替代 F2P；不覆盖「仓库没测到的行为」。

## 3. 判定矩阵（最容易做错的地方）

```
F2P     ：base 失败 + fix 通过      → f2p_satisfied
回归门  ：base 既有测试绿 + fix 仍绿 → regression_ok
```

| base 既有测试 | fix 既有测试 | F2P | 结论 |
|---|---|---|---|
| 通过 | 通过 | 成立 | ✅ 都成立，可继续 |
| 通过 | **失败** | 成立 | ❌ **拒绝**：修复破坏了既有行为 |
| **失败** | — | — | ⚠️ 仓库本身不绿 → 本门不可信，记 warn 并放行 |
| ERROR/TIMEOUT | — | — | ⚠️ 同「不绿」：环境/依赖问题，不可归咎于修复 |
| 通过 | 通过 | 不成立 | 回归门不是 F2P 的替代，仍按 F2P 处理 |

**第 3 行是安全底线**：老仓库普遍有本来就失败的测试，沙盒还常缺第三方依赖。把「base 不绿」
当成修复的错，会把所有条目误杀，闸门反而变成故障源。

`run_broken_reason` 统一判 `ERROR`：pytest `exit=4`、目标文件不存在、conftest 加载失败都算
「测试压根没跑」——例如仓库 conftest `import yaml` 失败，沙盒镜像里没有该依赖。

## 4. 配置

```yaml
verifier:
  regression_gate: strict     # off | warn | strict（默认 strict）
  regression_command: null    # 留空 → 仓库级 test_hint → 探测
  regression_timeout_seconds: 900
auto_fix:
  regression_retries: 1       # 被拦下时把「哪几条用例被改坏」喂回 Agent 重试
```

- **默认 `strict`**：默认只提醒等于本门宣称的作用不生效。而误杀风险已由上面的安全底线兜住，
  所以 strict 只在「改之前是绿的、改完之后红了」时拦人——那正是它该拦的。
- `warn`：跑、记录、报告标注，但不改变 F2P 结论（先统计一批「不绿率」时用）。
- `off`：完全不跑，零开销。

## 5. 实现要点

**命令来源**（优先级）：`regression_command` → `repo.test_hint` → 探测（`pyproject.toml`/`pytest.ini`
用 pytest，`package.json` 用 `npm test`…）→ 探不到则 `SKIPPED` 跳过。
**不要让 LLM 现编命令**——本门的价值就在于跑仓库自己的既有测试。

**必须排除验证器自己写入的文件**（`spec.files`，如 `tests/test_reproduce.py`）：pytest 默认会收集
未跟踪文件，它们会让 base 阶段失败，与「既有测试」语义混淆。做法是按 `git ls-files` 限定，
并把 `spec.files` 路径临时移出、`try/finally` 恢复。

**执行与记录**：复用 `VerifierRunner` 的沙盒通道（不得直接 `subprocess` 跑在宿主上）；独立
`stage` 标识（`regression:base` / `regression:fix`）落库，避免与 F2P 混读；超时用
`regression_timeout_seconds`（不要沿用验证器自己的预算）；产物沿用 `_save_artifacts`。

**呈现**：`strict` 拒绝时必须点名**哪一条测试挂了**，否则人工无从复核；失败走既有
`needs_manual` + `data/manual/<key>/report.md`。

**顺序契约**：PR 流水线的 base 回归门必须在 `_merge_pr` **之前**（见
`test_pr_verify_runs_base_before_merge`）。`judge_f2p` 语义**不要改**——两个独立判据混在一起会
破坏 `f2p_satisfied` / `reproducible` / `fixed` 的契约。

## 6. 成本与风险

额外成本 = N 次全量套件运行（每 Issue 2 次），与 Issue 数、token 无关；套件太慢则到点超时，
按「不可信」放行，不无限等待。

| 风险 | 处置 |
|---|---|
| 老仓库普遍不绿 | 安全底线：base 不绿/跑不通/超时一律放行；仍想观察可改 `warn` |
| 套件很慢 | 独立超时预算，到点放弃 |
| 依赖装不全（沙盒禁网） | base 红即判不可信，不阻断 |
| 测试发现机制扫到验证器文件 | 基于 `git ls-files` 限定 + 临时移出 |
| 与 F2P 语义混淆 | 独立纯函数 + 独立 stage 标识 |

## 7. 验收清单

- [ ] `off` 时零开销（断言未发起沙盒执行）
- [ ] `warn` 下失败不改变 F2P 结论，但报告可见；且不新增 LLM 调用
- [ ] `strict` 下「base 绿、fix 红」→ 拒绝、落 `needs_manual`、点名失败用例
- [ ] `strict` 下「base 红」→ **不阻断**（单独验证，写错会大面积误杀）
- [ ] base 回归门失败不污染 `reproducible / fixed / f2p_satisfied`
- [ ] PR 流水线 base 回归门在合并之前
- [ ] 验证器写入的测试文件被排除（造「该文件会失败」仍 `PASS`）
- [ ] 命令探不到返回 `SKIPPED`；超时返回 `TIMEOUT`

工程细节：写盘**不得**用文本模式（Windows 会把 `\n` 转 `\r\n`，破坏补丁），子进程调用**必须**
显式 `encoding="utf-8"`（中文环境按 GBK 解码会乱码）。参考实现见 `_write_patch`、
`tests/test_sandbox_verifier.py::_sandbox_outcome`。

---

相关：[DESIGN.md §4](DESIGN.md) · [USAGE.md](USAGE.md) · [wiki/Verifier-and-F2P.md](wiki/Verifier-and-F2P.md) ·
[BENCHMARK.md](../BENCHMARK.md)