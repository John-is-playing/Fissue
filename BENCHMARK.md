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
| `lockkit` | 15 I + 12 PR | **7** | 7 | **1** | 8 fix / 6 merge |

¹ textkit 是最早跑的一套，当时回归门功能尚未加入（commit `7bddf08` 之后才有），
故无 `regression:*` 阶段记录。

² **lockkit 是 2026-10-13 首次测量的**，口径与前四套略有不同，读这张表时要留个心：
抓取到 **27 条**（15 Issue + 12 PR），比夹具自述的 20 条多——多出来的是仓库里既有的
其它 PR，不是夹具的一部分。**修复侧只跑了 1 条**（`lockkit#2`，先 dry-run 再真实提 PR
各一次），其余 7 条仍停在 `fix_queued`。所以这一行反映的是「首次接入 + 单条修复的
端到端通路」，不是全量修复跑分；tier 期望偏差见 [§4.7](#47-lockkit-的-4-处-tier-期望偏差首次运行后补记)。

### 2.2 沙盒阶段明细

| 夹具 | base fail（可复现） | base pass（不可靠） | base error | fix pass | fix fail |
|---|---|---|---|---|---|
| `textkit` | 8 | **6** | 0 | 10 | 1 |
| `ratekit` | 10 | **1** | 0 | 9 | 1 |
| `pipekit` | 16 | 0 | **15** | 3 | 1 |
| `envkit` | 24 | 0 | 0 | 19 | 1 |
| `lockkit` | 22 | 0 | 0 | 8 | 0 |

**怎么读这张表**：

- `base fail` 是**理想情况**——未修复代码上验证器失败，证明问题真实存在。
- `base pass` 表示验证器不可靠（在**未修复**代码上就通过了），Fissue 会触发修正回路，
  修正不了则转人工。textkit 早期有 6 次，ratekit 1 次。
- `base error` 表示沙盒执行本身出错（结论不可信，不进入自动修复）。
  pipekit 的 15 次 error **属同一个瞬态环境故障（基础镜像当时不在），
  且每条随后都重跑成功**，故不作为能力证据，见 [§4.1](#41-pipekit-的-base-error)。
- `regression:fix fail` 就是**成功拦下的"修好目标却弄坏别处"**。
- `lockkit` 只跑了 1 条修复（见 §2.1 脚注 ²），它的 `fix pass` 计数来自那两次尝试里
  的多次「写入→验证」，**不能**与前四套的「全量修复」计数直接横比。

### 2.3 冻结基线

| 基线 | 冻结时间 | commit | 工作区 | 模型 | 条目 | 验证器运行 | 修复尝试 |
|---|---|---|---|---|---|---|---|
| `7351432-baseline` | 2026-10-10 | `7351432a` | 干净 | qwen3.6-plus | 103 | 467 | 71 |
| `6f1f03b-baseline` | 2026-10-13 | `6f1f03b0` | 干净 | deepseek-v4.1-flash | 56 | 125 | 9 |

两份都用 `fissue bench capture` 冻结（**只读数据库**，不触发抓取/评测），都满足
「工作区干净」这一可复现前提，`fissue bench list` 可复核。新基线对应的正是
2026-10-13 那次真跑：envkit 全套 + lockkit 首次接入——它的 `pr_created: 2` 就是
PR #31 与 #28。

**两份基线不可比**，`fissue bench diff` 的判定（退出码 `1`）：

```text
⚠️  不可比——以下前提不同，结论不能当回归依据：
   - commit 不同（7351432a → 6f1f03b0）
   - 模型不同（qwen3.6-plus → deepseek-v4.1-flash）
```

> ⚠️ **还有第三条差异，`diff` 原先没 flag：条目集不同（103 → 56）。** 旧库里含
> textkit / ratekit / pipekit，而当前 `fissue.db` 只剩 envkit + lockkit。所以 diff
> 里那一串「↓ -47 / -55 / -36」**绝大部分是范围变化，不是回归**——读这些数字之前
> 必须先确认条目集一致。
>
> **该缺口已修**：`capture` 现在记录 `items.by_repo`，`diff` 在「仓库集不同」或
> 「一侧未记录仓库集」时都会判为不可比。但注意**本节这两份基线都冻结于修复之前**，
> 都没有 `by_repo`，所以它们的范围仍然无从判断。没有回头补记：基线一旦冻结，内容就
> 不该再改——改了，`label` 与 commit 的对应也随之断裂，那正是这套机制赖以成立的东西。

另一个**只能当线索、不能当结论**的观察：`avg_scores.importance` 从 73.8 掉到 56.0
（-17.8）。这与 [§4.7](#47-lockkit-的-4-处-tier-期望偏差首次运行后补记) 里「模型给
lockkit 九条的重要性全部落在 55–78」互相印证。但两份基线的**模型与条目集同时不同**，
无法把这点单独归因给模型——只能说「换模型后重要性刻度似乎下移了约 18 分」，值得在
**同一套条目**上单独测一次再下结论。

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
| `lockkit` | PR4 `should_retry` 合同被破坏 | 既有测试 `1 failed, 86 passed`（构造期实测；见 [§4.6](#46-lockkit-pr4-的回归门样本一次失效与修复构造期发现已修)） |

四套夹具的"回归门对抗样本"**全部有效**。
其中 `lockkit` 的样本曾是失效的，修复过程（含一次被否决的错误修法）记在 §4.6。

实测失败输出示例
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

### 4.1 pipekit 的 base error（已定位：瞬态环境故障，非能力问题）

> **本节曾被写成"本基准最大的未解问题"。复核数据后推翻了那个结论，原文保留在文末「修订记录」。**

**现象**：pipekit 有 **15 次 `base` 阶段 `error`**，且只有 3 个验证器达到 `f2p_satisfied=1`
（相对 20 个条目）。

**复核**：15 次 error 的 `error` 字段**去重后只有一条文案**：

```text
镜像不可用：fissue/sandbox-base:latest
```

且这 15 条的 `exit_code` 全为 `NULL`、`duration_seconds` 仅 **0.42~0.72 秒**、`stderr` 为 0 字节——
沙盒根本没启动，是执行前的准备阶段就返回了失败。按 `created_at` 切分，时间线干净得反常：

| 时段 | `base` 阶段结果 |
|---|---|
| 11:21–12:01 | **15 × `error`**（镜像不可用） |
| 13:02–13:49 | **16 × `fail`**（正常复现） |

**每一条带 `error` 的条目，随后都拿到了一次 `fail`**：

```text
#1  error 11:21 → fail 13:02   |   #10 error 11:45 → fail 13:31
#2  error 11:59 → fail 13:49   |   #14 error 11:35 → fail 13:17   （16 条同形态）
```

**根因**：当晚基础镜像未构建（`docker build -t fissue/sandbox-base:latest docker/sandbox`
是 `USAGE.md` 里的准备步骤），而运行期 `ensure_image()` 尝试去 Docker Hub 拉同名镜像又超时失败。
该报错路径已在本地复现：

```text
INFO  镜像 fissue/…:latest 不存在，尝试拉取…
ERROR 拉取镜像失败：… context deadline exceeded
→  ExecResult(ok=False, error='镜像不可用：…')
```

再者，本文档自己的口径是「取**最新一次**结论，阶段统计是**累计**的」（§7.3）。
15 次 error 与 16 次 fail 按时段互斥、且逐条一一对应——**累计统计把同一个瞬态故障算成了"大面积失败"**。

**修正后的结论**：

- 那 15 次 error **不构成"根因定位未被测量"的证据**，应从「站不住」中撤下。
- pipekit 得到的是 **16 条 `base fail` + 16 条 `regression:base` 全 `pass`**（12 Issue + 4 PR）——
  验证器生成能在未修复代码上稳定复现这些跨模块缺陷。
- `f2p_satisfied=1` 只有 3 条，指的是 **PR 侧**：5 个 PR 里 PR1/PR4/PR5 的三段式
  （`base` fail → `fix` pass → 回归门 pass）成立。`fix` 阶段的 **4 次运行全部来自 PR**
  （#16/#17/#19/#20；#18 是纯文档 PR，按设计不验证），**与 Issue 无关**。
- pipekit 真正未被测量的是 **Issue 侧自动修复**：10 条 tier1/tier2 全部停在 `fix_queued`，
  `fix_attempts` 记录为 **0**、`data/patches/` 无 pipekit 补丁（与 §4.5 一致），见 [§5](#5-尚未测量)。

**状态**：**已定位**（非夹具缺陷、非能力缺陷，是环境准备缺失 + 统计口径），并已加固工具链：
`fissue verify` 现在会在跑整批之前做一次**基础镜像前置检查**——只查存在性、**不触发拉取**
（拉取失败前要空等约 20 秒超时，放进逐条检查会比原问题更糟），缺镜像时毫秒级拦下整批并给出
`docker build` 命令，取代原来「每条先克隆 + LLM 生成验证器、最后才在沙盒里报错」的形态。

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

### 4.6 lockkit PR4 的回归门样本：一次失效与修复（构造期发现，已修）

**这一条不是 Fissue 的缺陷，是夹具自身的规格 bug**，但同样公开——
包括我们**第一次修错**的过程。

#### 4.6.1 原设计失效

`lockkit` 最初把 PR4（`fix/retry-cap-and-subclass`）设计为「顺手把
`Latch.open` 由 `== 0` 改成 `<= 0`」，声称这会让
`tests/test_gate.py` 的既有测试从绿变红。构造时实测**并非如此**：

```text
把 Latch.open 由 `self._remaining == 0` 改成 `self._remaining <= 0`
 →  87 passed，没有任何一条测试变红
```

三条原因叠加：

1. 基线只断言 `open is False`，此时 `remaining == 2` —— `== 0` 与 `<= 0` 都满足；
2. 能区分两者的唯一路径是「一次 `count_down(n)` 跨过阈值」，
   而 `count_down()` 的返回值**就是** `open`，基线从未构造这个场景；
3. **最关键**：`<= 0` 恰恰是 Issue #7 的**正确修复**，不是回归——
   `count_down` 返回 `self.open`，若恢复 `== 0`，`count_down(5)` 会把闩锁
   留在「未打开」状态。

#### 4.6.2 一个看似显然、实则有害的「修法」

顺着上面的分析，最自然的补救是「给 `tests/test_gate.py` 补一条覆盖
『一次跨过阈值』的基线断言」。**这个想法是错的，已否决**：

那条断言在**未修复的 `main`** 上就会失败（`count_down(5)` 后 `open` 为 `False`
而 `remaining` 为 0）。把它加进基线，等于让基线**不再是全绿**——
而全绿基线正是本夹具赖以成立的前提（也是回归门「base 绿则可信」判定的前提）。
换句话说，用「补断言」去造回归样本，会让 `main` 变成「本就红」的仓库，
反而使**整条回归门对所有条目失效**。

**这提醒了一件事**：造回归样本必须选一个「既有行为确实正确、却被改坏」的点，
不能选「本来就错、被改对」的点。

#### 4.6.3 已采用的修法

改用 `should_retry` 作为被破坏点。PR4 在修好 `delay_for`（目标用例通过）的同时，
「顺手」剥掉 `should_retry` 的 `attempt >= self.attempts` 次数判断，
只留异常类型判定，并声称「尝试次数耗尽交由调用方判断」。

`should_retry` 是对外契约、有独立语义，因此真有既有测试变红：

```text
FAILED tests/test_retry.py::test_should_retry_exhausted_attempts
E   assert True is False
E    +  where True = should_retry(ValueError('x'), attempt=2)   # attempts=2，已耗尽
1 failed, 86 passed
```

而 `retry_call` 自己就用 `attempt >= active.attempts` 判断、并不走 `should_retry`，
所以功能上「看着没事」——**只看 F2P 会误判为「可合并」，只有跑既有测试回归门
才拦得住**。这正是该样本要演示的东西。

**状态**：**已修复**。`lockkit` 现在与另外三套一样，能提供有效的回归门对抗样本。

### 4.7 lockkit 的 4 处 tier 期望偏差（首次运行后补记）

**现象**：lockkit README 期望 #1/#3/#5/#8 为 `tier1`（原话「难度低 + 重要性高」），
实测**全部为 `tier2`**；另外 5 条期望 tier2 的**全部命中**。

| # | README 期望 | 实测 | 重要性 | 难度 |
|---|---|---|---|---|
| 1 | tier1 | **tier2** | 68 | 12 |
| 3 | tier1 | **tier2** | 70 | 12 |
| 5 | tier1 | **tier2** | 60 | 8 |
| 8 | tier1 | **tier2** | 55 | 15 |
| 2 / 4 / 6 / 7 / 9 | tier2 | tier2 ✅ | 78 / 72 / 68 / 68 / 65 | 8 ~ 15 |

**根因**：`fix_policy.tier1` 要求 `重要性 ≥ 80`，而模型给这 9 条的评分**全部落在
55–78**，没有一条够得到门槛。难度侧没有问题（全部 ≤ 15）。

**功能后果：零。** tier1 与 tier2 **都会进入自动修复队列**（`fix_candidates` 同时取
两档），所以这 9 条「应自动修复」的期望**照常达成**——首次运行里 `lockkit#2` 被自动
修好并提出了 PR #28。这条偏差只影响**标签写成 tier1 还是 tier2**，不影响任何行为。

与 §4.2 对照可以看出一个更有意思的事实：模型的重要性口径与**两套夹具作者的预期都
不完全对齐**——envkit 那 5 条期望 tier2 的实测 ≥ 80（判 tier1），lockkit 这 4 条期望
tier1 的实测 < 80（判 tier2）。**方向相反，说明这是阈值标定问题，而非「模型一律偏
松 / 偏严」。** 与 §4.2 一样，**不改夹具期望表**：期望表是规格，不该为了让实测好看
而修改。

### 4.8 首次真跑暴露的 6 个缺陷（2026-10-13，均已修复）

把 `fetch → eval → verify → flush → fix（真提 PR）` 在**真实仓库**上跑通的这一轮里，
连续暴露出 6 个缺陷。它们的共同点是：**单测一个都没拦住**——因为单测打桩掉的，
恰恰就是出问题的那一层。

| # | 缺陷 | 为什么只有真跑才暴露 |
|---|---|---|
| 1 | `fix` 取不到候选时静默空跑 | 漏了 `flush` 一步，而日志里没有任何线索指向它 |
| 2 | Agent 读不到验证器的测试文件 → 12 轮空转 | 验证器文件只在**跑验证器时**注入沙盒，工作区里从来没有 |
| 3 | 只读不写的空转烧满 12 轮，且报错写成「修了很多次没修好」 | 原有停滞判据依赖「验证器跑过」，从不写入就永不触发 |
| 4 | `git add -A` 会把验证器测试文件一并提交进 PR | 补丁走 `git diff`（**不含未跟踪文件**），只有提交路径才带上 |
| 5 | `-c` 加在 `git push` 之后 → 推送全线失败 | 当时只验了凭据助手脚本，**从未真跑一次 `push`** |
| 6 | 已有开放 PR 被报成「推送失败 / 需人工」 | 分支名含内容哈希，重跑必然撞上同名分支 |

> **可复用的结论**：参数拼装 / 文件读写 / 状态机这类缺陷，单测的替代品是
> **本地裸仓库 + 真跑一次**（缺陷 5 现在就有这种集成测试）；而「最后一步」的缺陷
> 只能靠**真提一次 PR** 发现——dry-run 会**系统性地**漏掉提交与推送这两段路径。

---

## 5. 尚未测量

诚实列出**没跑过**的部分（划掉的是本次已补上测量的）：

| 项 | 状态 |
|---|---|
| **`pipekit` 的 Issue 侧自动修复（`fissue fix`）** | **仍未运行**（`fix_attempts` = 0、无 patch 产物），10 条 tier1/tier2 全部停在 `fix_queued`。`fix` 链路本身已在 envkit / lockkit 上跑通（见下两行），缺的只是这套夹具自己的测量。PR 侧 5 个 PR 中 4 个**已完整测量**（#18 为纯文档 PR，按设计不验证），详见 [§4.1](#41-pipekit-的-base-error已定位瞬态环境故障非能力问题) |
| ~~`lockkit`（15 Issue + 5 PR）~~ → **已测量（2026-10-13）** | 完整链路跑通：抓取 **27 条**（15 Issue + 12 PR，比夹具自述的 20 条多）→ 评测 → 验证（27 条全部有结论，48 次沙盒执行）→ flush（verify 队列 8 条全部打标、8 条 tier2 进入待修队列）→ 自动修复 `lockkit#2` 成功（3 轮）并提出 **PR #28**。另有 **4 处 tier 期望偏差**，见 [§4.7](#47-lockkit-的-4-处-tier-期望偏差首次运行后补记) |
| ~~真实提 PR（`mode: fork`）~~ → **已测量（2026-10-13）** | 已跑通并逐项核对：`envkit#4` → **PR #31**（验证后已关闭）、`lockkit#2` → **PR #28（open）**。两处的改动都只有源码文件、**未夹带验证器测试文件**，标签与配置一致（`fissue` / `ai-generated`）。历史：2026-10-09 那次也建过 #21–#29（当前仍 open） |
| Gitee / AtomGit / GitLab 平台 | 仅单测覆盖，**无端到端实测** |
| PostgreSQL 后端 | 开发期使用 SQLite |
| 冻结版本的可复现跑分 | **已冻结两份基线**（2026-10-10 与 2026-10-13），均在「工作区干净」前提下 capture，`bench list` 可复核；两者的可比性判定与范围提醒见 [§2.3](#23-冻结基线)。`bench capture` 记录 git commit + 是否有未提交改动、模型与采样参数、关键配置指纹，以及各阶段结果；`bench diff` 先判**可比性**再给逐项增量——见 [§6](#6-如何复现)。 |

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

### 6.1 冻结一份可复现的基线

上面「出报告」只产出**可读**的 Markdown，无法机械比对。要做正式跑分，
用 `fissue bench` 冻结**机器可读**的基线——它同时记录**前提**与**结果**：

```bash
# 1) 前提必须先干净：commit 已提交、工作区无未提交改动、模型参数固定
git status --short          # 应无输出
git rev-parse HEAD          # 记下这个 commit

# 2) 冻结（只读数据库，不触发抓取/评测）
fissue bench capture --repo John-is-playing/ratekit --label ratekit-<commit 短哈希>

# 3) 之后任何改动（代码或配置）再冻结一次，然后逐项对比
fissue bench capture --repo John-is-playing/ratekit --label ratekit-<新哈希>
fissue bench diff data/benchmarks/ratekit-<旧哈希>.json data/benchmarks/ratekit-<新哈希>.json
```

`bench diff` **先判可比性再给数字**；若 commit、模型或工作区状态不一致，
它会直接标 `不可比` 并说明原因，不会给出一个会被误当成回归结论的增量。
退出码：`0` 可比且无变化｜`1` **不可比**｜`2` 可比但有变化。

> ⚠️ **工作区脏的基线会被标记 `reproducible: false`**。这是刻意的：
> 本文档 §7.2「非冻结」这一局限，正是靠这个标记被机械地表达出来——
> 不允许拿「代码随时会变」的两次测量当回归依据。所以**正式跑分前先提交**。

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
   反面证据：pipekit 的 Issue 侧自动修复**从未运行**，说明**不是所有维度都好看**。

---

## 8. 结论

**站得住的**：

- 回归门在 3 套夹具上**全部拦下**了"修好目标却弄坏别处"的假修复——这是 F2P 单独做不到的。
- 假修复（F2P 不成立）被识破；刷量、误报、设计偏好变更均被拦在自动修复之外。
- 重复检测能召回中文同义重复，且不误标"同源但非重复"。

**站不住的**：

- pipekit 的 **Issue 侧自动修复从未运行**（10 条 tier1/tier2 停在 `fix_queued`），
  故「跨模块根因定位 → 自动改对文件」这一环**未被测量**
  ——注意：这**不是**早先归因的「15 次 base error」，那 15 次是瞬态环境故障，见 [§4.1](#41-pipekit-的-base-error已定位瞬态环境故障非能力问题)。
  （PR 侧的合并验证已覆盖 4/5 个 PR。）
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

---

## 附二：修订记录

### §4.1 的原文（2026-10-08 复核前）

本节曾是本文档「最重要的未解问题」，现按 [§4.1](#41-pipekit-的-base-error已定位瞬态环境故障非能力问题) 的复核结论改写。
原文照抄如下，供读者核对这次修正是否站得住：

```text
### 4.1 pipekit 的 base error

**现象**：pipekit 有 15 次 base 阶段 error，且只有 3 个验证器达到 f2p_satisfied=1
（相对 20 个条目）。

**含义**：沙盒执行本身失败，结论不可信，这些条目未被自动修复（保守行为正确），
但也意味着 pipekit 的根因定位能力大部分没能被验证。

**影响**：这是目前最大的未解问题。pipekit 是专门设计来考"跨模块根因定位"的夹具，
它的 base 阶段大面积失败，说明该维度的能力尚未得到有效测量。

**状态**：未修复。需要单独排查（沙盒环境？依赖装不全？超时？），
建议后续用 fissue verify --repo John-is-playing/pipekit --key <单条> 逐条定位。
```

**改了什么**：

| 位置 | 原文 | 现文 |
|---|---|---|
| §2.2 说明 | 「pipekit 的 15 次全部是 error，这是本基准最大的一个未解问题」 | 同一瞬态故障、且每条随后都重跑成功 |
| §4.1 标题 | 「pipekit 的 base error」 | 追加「已定位：瞬态环境故障，非能力问题」 |
| §5 | 无 pipekit 行 | 新增「Issue 侧自动修复从未运行」为真正未测量项 |
| §7.5 | 反面证据=「大量 base error」 | 反面证据=Issue 侧自动修复从未运行 |
| §8 站不住 | 「根因定位基本未被有效测量（15 次 base error）」 | 「Issue 侧自动修复从未运行」 |
| `README.md` | 同 §8 | 同 §8 |

**依据**：`fissue.db` 的 `verifier_runs` 表。15 次 error 的 `error` 字段去重后仅
`镜像不可用：fissue/sandbox-base:latest` 一条，`exit_code` 全 `NULL`、
`duration_seconds` 0.42~0.72s、`stderr` 0 字节；按时段切分与后续 16 次 `fail`
互斥且逐条对应。复现与加固过程见 §4.1「状态」。
