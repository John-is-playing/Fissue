# 基准测试（Benchmark）

> 这份文档公开 Fissue 在五套对抗性夹具上的**真实测量结果**，包括**已知失败**。
>
> 我们对"跑分"的态度和 Fissue 对待 Issue 的态度一致：**只报有证据的结论**。
> 因此这里没有"全部通过"这类说法，只有可复核的数字、失败项、以及尚未测量的部分。

---

## 0. 为什么公开一份会自曝短板的基准

大多数项目只展示成功案例。但 Fissue 的核心主张是
**「别信模型的自述，要看独立证据」**——如果连它自己的评测都只挑好看的报，
那这个主张就没有说服力。

所以本文档遵守三条规则：

1. **只写实测**。每条结论都能在仓库内找到出处（数据库、产物、报告文件）。
2. **已知失败照写**。见 [§4](#4-已知失败与偏差)。
3. **没跑过就写"没跑"**。见 [§5](#5-尚未测量)。

---

## 1. 测量方法

### 1.1 五套夹具是什么

夹具是**刻意构造的对抗性样本仓库**：植入若干真实缺陷，附带一套**全绿但不覆盖缺陷**的
基线测试，再混入假修复、只修一半、重复提交、刷量、误报等干扰项。

| 夹具 | 领域 | 规模 | 考什么 |
|---|---|---|---|
| `textkit` | 文本处理 | 5 Issue + 2 PR | 单函数语义 |
| `ratekit` | 金额 / 费率 | 10 Issue + 3 PR | 数值边界 |
| `pipekit` | 分片流水线 / 区间 | 15 Issue + 5 PR | **跨模块根因定位** |
| `envkit` | 配置 / 缓存 / 校验 | 15 Issue + 5 PR | 状态副作用、环境依赖、输入校验 |
| `lockkit` | 并发原语 / 资源管理 | 15 Issue + 5 PR | 资源不变量、超时上限、一次性语义 |

合计 **60 Issue + 20 PR**。每套的「期望结果对照表」在其自己的 `README.md` 里。

### 1.2 链路

```
fetch → eval（四维评分 + 分类 + 反刷子）
      → verify（生成验证器 → 沙盒 F2P：base 必失败、fix 必通过）
      → 回归门（仓库既有测试 pass-to-pass）
      → flush（批量定论 + 打标签）
      → fix --dry-run（自动修复，只产补丁）
```

### 1.3 判定口径

| 指标 | 通过条件 |
|---|---|
| **F2P 成立** | `base` 阶段 exit≠0 **且** `fix` 阶段 exit=0 |
| **回归门通过** | `base` 既有测试绿 **且** `fix` 既有测试绿 |
| **重复检测命中** | 相似 Issue 被判 `is_duplicate=true` 且 `duplicate_of` 指向正确编号 |
| **刷量/误报拦截** | 低真实性条目 `priority=none`，且**未**生成修复补丁 |
| **假修复识破** | F2P 不成立 **或** 回归门拦下 → 结论为不合并 |

> ⚠️ **重要前提**：本表数据来自**开发期累计运行**（2026-10-03 ~ 10-05），
> 期间 Fissue 自身在持续修 bug（见 `30ae40b-BUG.md`）。
> 因此这不是"冻结版本的官方跑分"，而是历史测量记录。
> LLM 输出具有非确定性，**同一配置重跑数值会漂移**。

---

## 2. 各夹具测量结果

数据来源：`fissue.db`（verifier_runs / verifiers / batch_conclusions 表）。

### 2.1 汇总

| 夹具 | 条目 | 验证器 F2P 成立 | 回归门 fix 通过 | 回归门 fix 失败 | 批量结论 |
|---|---|---|---|---|---|
| `textkit` | 5 I + 2 PR | 5 | 0 ¹ | 0 ¹ | 3 fix / 1 merge / 3 needs_review / 1 reject |
| `ratekit` | 10 I + 3 PR | **9** | 9 | **1** | 6 fix / 2 merge / 1 reject |
| `pipekit` | 15 I + 5 PR | **3** | 3 | **1** | 11 fix / 2 merge / 1 needs_review / 1 skip |
| `envkit` | 15 I + 5 PR | **12** | 19 | **1** | 11 fix / 2 merge / 2 needs_review |

¹ textkit 是最早跑的一套，当时回归门功能尚未加入（commit `7bddf08` 之后才有），
故无 `regression:*` 阶段记录。

### 2.2 沙盒阶段明细

| 夹具 | base fail（可复现） | base pass（不可靠） | base error | fix pass | fix fail |
|---|---|---|---|---|---|
| `textkit` | 8 | **6** | 0 | 10 | 1 |
| `ratekit` | 10 | **1** | 0 | 9 | 1 |
| `pipekit` | 16 | 0 | **15** | 3 | 1 |
| `envkit` | 24 | 0 | 0 | 19 | 1 |

**怎么读这张表**：

- `base fail` 是**理想情况**——未修复代码上验证器失败，证明问题真实存在。
- `base pass` 表示验证器不可靠（在**未修复**代码上就通过了），Fissue 会触发修正回路，
  修正不了则转人工。textkit 早期有 6 次，ratekit 1 次。
- `base error` 表示沙盒执行本身出错（结论不可信，不进入自动修复）。
  **pipekit 的 15 次全部是 error**，这是本基准最大的一个未解问题，见 [§4.1](#41-pipekit-的-base-error)。
- `regression:fix fail` 就是**成功拦下的"修好目标却弄坏别处"**。

---

## 3. 关键对抗样本的实际结果

这些是夹具里最能说明问题的设计，逐项列出实测结论。

### 3.1 假修复（F2P 不成立 → 应拒绝）

| 夹具 | 样本 | 实测结论 | 判定 |
|---|---|---|---|
| `ratekit` | PR3 `percent_of`「修复」（abs → ×-1） | 批量结论 `reject` | ✅ 识破 |
| `pipekit` | PR2 `drop_ranges`「修复」（根因未动） | fix 阶段 `fail` | ✅ 识破 |
| `textkit` | PR2 `slugify` 半吊子修复 | fix 阶段 `fail` ×1 | ✅ 识破 |

> textkit PR2 是最经典的一项：它用 `\s+` 折叠了连续空格，
> `Hello   World` 确实修对了，但 `Hello - World`（空格+连字符混排）
> 仍产出 `hello---world`。**只看目标用例的验证器会判它可合并。**

### 3.2 回归门拦截（F2P 成立但弄坏别处）

| 夹具 | 样本 | 实测结论 |
|---|---|---|
| `ratekit` | PR2 `is_weekend` 修复 | `regression:fix` = **fail**（exit=1）→ 转人工 |
| `pipekit` | PR4 `merge_adjacent` 修复 | `regression:fix` = **fail** |
| `envkit` | PR4 `sanitize` 修复 | `regression:fix` = **fail** |

三套夹具的"回归门对抗样本"**全部被拦下**。实测失败输出示例
（`data/artifacts/github_John-is-playing_ratekit#12/regression_fix/stdout.log`）：

```text
..FFF...............................                                     [100%]
test_add_working_days_over_weekend: 2024-01-09 != 2024-01-08
test_add_working_days_span:         2024-01-16 != 2024-01-15
test_add_working_days_zero:         ...
```

**这是本基准最重要的正面结论**：F2P 只表达"目标修好了"，表达不了"别弄坏别的"；
补上回归门后，这三类假修复才被真正拦住。

### 3.3 重复检测

| 夹具 | 样本 | 期望 | 实测 |
|---|---|---|---|
| `ratekit` | #8 与 #2 均为「含税反推不对」 | 判重复 | ✅ `is_duplicate=true`, `duplicate_of=[2]` |
| `pipekit` | #12 与 #3 同源（标题词面只 0.10） | 相关但**非**重复 | ✅ 未误标 |
| `envkit` | #12 与 #2 同源 | 相关但**非**重复 | ✅ 未误标 |

`ratekit` #8 曾经**漏检**——那时中文标题按单字切分，Jaccard 只有 0.208，低于阈值 0.25，
连候选都进不了重复判定。修复后改为 **bigram + 包含度 + 阈值 0.22**，得 0.286 正确召回。
阈值是用真实重复对（0.286）与设计偏好对（0.154）夹出来的，详见 `30ae40b-BUG.md` §5.1。

### 3.4 刷量与误报拦截

| 夹具 | 样本 | 期望 | 实测 |
|---|---|---|---|
| `ratekit` | #10 性能+推广 | 低真实性 | `authenticity=10`, `is_spam=true`, `action=close` |
| `ratekit` | #9 非法输入应返回 0 | 低真实性（设计偏好） | `authenticity=30`（原为 90，见 §4.2） |
| `envkit` | #13 `get_bool` 应抛错 | 低真实性（设计偏好） | 分类 `FEATURE`，`priority=none`，未生成补丁 |
| `envkit` | #14 性能+推广 | 判刷量 | `priority=none` |
| `envkit` | #15 说不清哪里错 | 判无效 | `action=需要更多信息` |

---

## 4. 已知失败与偏差

**这一节是本文档最重要的部分。**

### 4.1 pipekit 的 base error

**现象**：pipekit 有 **15 次 `base` 阶段 `error`**，且只有 3 个验证器达到 `f2p_satisfied=1`
（相对 20 个条目）。

**含义**：沙盒执行本身失败，结论不可信，这些条目**未被自动修复**（保守行为正确），
但也意味着 **pipekit 的根因定位能力大部分没能被验证**。

**影响**：这是目前最大的未解问题。pipekit 是专门设计来考"跨模块根因定位"的夹具，
它的 base 阶段大面积失败，说明该维度的能力**尚未得到有效测量**。

**状态**：未修复。需要单独排查（沙盒环境？依赖装不全？超时？），
建议后续用 `fissue verify --repo John-is-playing/pipekit --key <单条>` 逐条定位。

### 4.2 envkit 的 5 处 tier 期望偏差

**现象**：envkit README 期望 #2/#4/#6/#8/#9 为 `tier2`，实测均为 `tier1`。

| # | 难度/重要性 | README 期望 | 实测 |
|---|---|---|---|
| 2 | 15 / 85 | tier2 | **tier1** |
| 4 | — / — | tier2 | **tier1** |
| 6 | — / — | tier2 | **tier1** |
| 8 | — / — | tier2 | **tier1** |
| 9 | — / — | tier2 | **tier1** |

**两种读法，都写出来**：

1. **README 期望值过期**：`fix_policy.tier1.min_importance` 在开发期由 **70 调到 80**
   （为修正 ratekit #4/#5 偏高）。envkit 的 README 期望表写在改阈值之前。
   按 `min_importance=80`，这 5 条重要性均 ≥ 80，判 tier1 是**符合当前配置**的。
2. **若以 README 为规格**：则这是 5 处未命中，属于阈值标定问题（`30ae40b-BUG.md` 提升项 E）。

**我们倾向读法 1，但没有把 README 改掉**——因为"期望表"是夹具的规格，
不该为了让实测好看而修改。这个不一致目前**公开保留**。

### 4.3 textkit 缺少回归门数据

textkit 跑在回归门功能（`7bddf08`）合入之前，因此没有 `regression:*` 记录。
它的 5 个 F2P 成立、PR2 被 fix 阶段识破，但**没有经过回归门检验**。

### 4.4 textkit 的 18 次 Agent error

`textkit` 的 `agent` 阶段有 18 次 `error`（另有 24 次 pass）。
Agent 循环的失败尝试属于正常现象（自纠错过程的中间态），
但数量偏高，说明**早期 Agent 稳定性不足**。

### 4.5 修复尝试的结果分布

| 夹具 | 结果 |
|---|---|
| `textkit` | `failed` ×2，`needs_manual` ×7 |
| `ratekit` | `needs_manual` ×6 |
| `pipekit` | 无记录 |
| `envkit` | `needs_manual` ×16 |

**全部为 dry-run**（`patch_only` 模式，只产出补丁、不建分支、不提 PR）。
`needs_manual` 在这里是**预期语义**——dry-run 下补丁已产出但未提 PR。
`data/patches/` 下有 17 个补丁产物。

> ⚠️ 注意：以上**没有任何一次真实提 PR**。Fissue 的自动提 PR 路径
> **尚未在本基准中被端到端验证过**。

---

## 5. 尚未测量

诚实列出**没跑过**的部分：

| 项 | 状态 |
|---|---|
| `lockkit`（15 Issue + 5 PR） | **从未运行**。夹具已完成、基线自测 87 passed 全绿，但未进入 fetch→eval→verify→fix 链路 |
| 真实提 PR（`mode: fork` + `auto_submit: true`） | **从未运行**。所有修复均为 dry-run |
| Gitee / AtomGit / GitLab 平台 | 仅单测覆盖，**无端到端实测** |
| PostgreSQL 后端 | 开发期使用 SQLite |
| 冻结版本的可复现跑分 | **不存在**。需要固定 commit + 固定模型温度重跑 |

---

## 6. 如何复现

```bash
# 1) 准备环境（见 docs/USAGE.md 第 1~6 节）
python -m venv .venv && source .venv/Scripts/activate
pip install -e ".[dev]"
cp .env.example .env      # 填 FISSUE_LLM_API_KEY
cp config.example.yaml config.yaml
fissue db init

# 2) 夹具仓库需先推送到 GitHub（见各夹具 README 第一节）
#    注意：这一步会产生平台写操作，请自行执行

# 3) 跑完整链路
fissue fetch  --repo John-is-playing/ratekit --limit 50
fissue eval   --repo John-is-playing/ratekit --all
fissue verify --repo John-is-playing/ratekit
fissue flush  --queue verify
fissue fix    --repo John-is-playing/ratekit --dry-run --limit 6

# 4) 出报告，与 README 的期望对照表逐项核对
fissue report --repo John-is-playing/ratekit --format markdown --out data/exports/ratekit.md
```

**注意**：`fissue eval/verify/fix` 需要 LLM Key；单测（`python -m pytest -q`）
完全离线，不需要 Key 也不需要 Docker。

---

## 7. 已知的方法学局限

公开列出，供读者判断这些数字的可信度：

1. **非确定性**：LLM 输出每次都不同。本文档的数字是**单次历史测量**，
   不是稳定保证。重跑会漂移。
2. **非冻结**：测量跨越 2026-10-03 ~ 10-05，期间 Fissue 自身在修 bug，
   不同夹具实际上跑在**不同版本**的 Fissue 上。
3. **累计数据**：一个条目可能有多次评测记录（修 bug 后重跑），
   本文档取**最新一次**结论，但阶段统计是**累计**的。
4. **模型差异**：换一个 LLM 供应商，全部数字都会变。
5. **夹具偏差**：五套夹具由本项目设计，可能存在"照着 Fissue 的长处出题"的偏差。
   反面证据：pipekit 的大量 `base error` 说明**不是所有维度都好看**。

---

## 8. 结论

**站得住的**：

- 回归门在 3 套夹具上**全部拦下**了"修好目标却弄坏别处"的假修复——这是 F2P 单独做不到的。
- 假修复（F2P 不成立）被识破；刷量、误报、设计偏好变更均被拦在自动修复之外。
- 重复检测能召回中文同义重复，且不误标"同源但非重复"。

**站不住的**：

- pipekit 的跨模块根因定位能力**基本未被有效测量**（15 次 base error）。
- 真实提 PR 路径**从未端到端验证**。
- lockkit（并发/资源不变量维度）**完全未测量**。
- tier 标定存在**公开保留**的期望不一致（envkit 5 处）。

> 我们宁可把上面这段写出来，也不愿给一份"全部通过"的漂亮表格——
> 那正是 Fissue 存在的意义所反对的事。

---

## 附：数据出处

| 数据 | 位置 |
|---|---|
| 评测 / 验证 / 队列 / 修复记录 | `fissue.db`（SQLite；表 `evaluations` / `verifier_runs` / `verifiers` / `fix_attempts` / `batch_conclusions`） |
| 沙盒执行产物 | `data/artifacts/<platform>_<owner>_<repo>#<n>/`（`base/` `fix/` `agent/` `regression_base/` `regression_fix/`） |
| dry-run 补丁 | `data/patches/*.patch` |
| 需人工处理报告 | `data/manual/<key>/report.md` |
| 评测报告（逐条结论） | `data/exports/*.md` |
| 缺陷清单与修复过程 | `30ae40b-BUG.md` |
| 各夹具期望对照表 | `demo/<kit>/README.md` |
