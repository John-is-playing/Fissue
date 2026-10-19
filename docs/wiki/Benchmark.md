# 基准与可信度

> 公开 Fissue 在**五套对抗性夹具**上的**真实测量结果**，包括**已知失败**。
> 全文与逐条出处见 [BENCHMARK.md](../../BENCHMARK.md)。

Fissue 对自己的态度与对 Issue 的态度一致：**只报有证据的结论**。因此这里没有「全部通过」这类说法，
只有可复核的数字、失败项、以及尚未测量的部分。三条规则：**只写实测**、**已知失败照写**、**没跑过就写没跑**。

---

## 1. 方法学

夹具是**刻意构造的对抗性样本仓库**：植入若干真实缺陷，附带一套**全绿但不覆盖缺陷**的基线测试，
再混入假修复、只修一半、重复提交、刷量、误报等干扰项。

| 夹具 | 领域 | 规模 | 考什么 |
|---|---|---|---|
| `textkit` | 文本处理 | 5 Issue + 2 PR | 单函数语义 |
| `ratekit` | 金额 / 费率 | 10 Issue + 3 PR | 数值边界 |
| `pipekit` | 分片流水线 / 区间 | 15 Issue + 5 PR | **跨模块根因定位** |
| `envkit` | 配置 / 缓存 / 校验 | 15 Issue + 5 PR | 状态副作用、环境依赖、输入校验 |
| `lockkit` | 并发原语 / 资源管理 | 15 Issue + 5 PR | 资源不变量、超时上限、一次性语义 |

合计 **60 Issue + 20 PR**。链路：`fetch → eval → verify（F2P）→ 回归门 → flush → fix --dry-run`。

**判定口径**：

| 指标 | 通过条件 |
|---|---|
| F2P 成立 | `base` exit≠0 **且** `fix` exit=0 |
| 回归门通过 | `base` 既有测试绿 **且** `fix` 既有测试绿 |
| 重复检测命中 | `is_duplicate=true` 且 `duplicate_of` 指向正确编号 |
| 刷量/误报拦截 | 低真实性条目 `priority=none`，且**未**生成修复补丁 |
| 假修复识破 | F2P 不成立 **或** 回归门拦下 |

> **重要前提**：主表数据来自**开发期累计运行**（2026-10-03 ~ 10-05），期间 Fissue 自身在持续修 bug。
> 因此这**不是**「冻结版本的官方跑分」，而是历史测量记录。LLM 输出非确定性，**同一配置重跑数值会漂移**。

---

## 2. 结果摘要

| 夹具 | 条目 | 验证器 F2P 成立 | 回归门 fix 通过 | 回归门 fix 失败 | 批量结论 |
|---|---|---|---|---|---|
| `textkit` | 5 I + 2 PR | 5 | 0（注 1） | 0（注 1） | 3 fix / 1 merge / 3 needs_review / 1 reject |
| `ratekit` | 10 I + 3 PR | 9 | 9 | **1** | 6 fix / 2 merge / 1 reject |
| `pipekit` | 15 I + 5 PR | 3 | 3 | **1** | 11 fix / 2 merge / 1 needs_review / 1 skip |
| `envkit` | 15 I + 5 PR | 12 | 19 | **1** | 11 fix / 2 merge / 2 needs_review |
| `lockkit` | 15 I + 12 PR | 7 | 7 | **1** | 8 fix / 6 merge |

注 1：`textkit` 是最早跑的一套，当时回归门功能尚未加入，故无 `regression:*` 阶段记录。
注 2：`lockkit` 是 2026-10-13 首次测量，抓取到 **27 条**（比夹具自述的 20 条多，多出来的是仓库里既有的其它 PR）；
**修复侧只跑了 1 条**（`lockkit#2`），其余仍停在 `fix_queued`——这一行反映的是「首次接入 + 单条修复的端到端通路」，**不是全量修复跑分**。

### 沙盒阶段明细

| 夹具 | base fail（可复现） | base pass（不可靠） | base error | fix pass | fix fail |
|---|---|---|---|---|---|
| `textkit` | 8 | 6 | 0 | 10 | 1 |
| `ratekit` | 10 | 1 | 0 | 9 | 1 |
| `pipekit` | 16 | 0 | 15 | 3 | 1 |
| `envkit` | 24 | 0 | 0 | 19 | 1 |
| `lockkit` | 22 | 0 | 0 | 8 | 0 |

**怎么读**：`base pass` 表示验证器不可靠（在未修复代码上就通过）；`base error` 表示沙盒本身出错（结论不可信）；
`regression:fix fail` 就是**成功拦下的「修好目标却弄坏别处」**。

> `pipekit` 的 **15 次 `base error` 属同一个瞬态环境故障**（当晚基础镜像未构建，运行期拉同名镜像又超时失败），
> 且**每条随后都重跑成功**（重跑时 `error` 为 **0 次**）。根因：`ensure_image()` 在镜像缺失时尝试拉取、
> 失败即返回「镜像不可用」。**不作为能力证据**。

### 提示词重写前后的对照（同一批待修条目）

| | 尝试 | 产出补丁 | 轮次分布 |
|---|---|---|---|
| 重写前 | 13 | **5（38%）** | 0~12 轮，含一次 12 轮的**纯空转**（一个文件都没写） |
| 重写后 | 19 | **19（100%）** | **18 条恰好 2 轮**，1 条 3 轮 |

逐仓库（重写后）：`envkit` 5/5、`lockkit` 7/7、`pipekit` **7/7**。
花费：19 条共 **41 次调用、246,888 tokens**。

> 这不是「能力突然变强」，而是**把纪律写进提示词**之后模型不再空转：判据从「读够了没」换成「**够了就动手**」。

---

## 3. 关键对抗样本的实际结果

### 假修复（F2P 不成立 → 应拒绝）

| 夹具 | 样本 | 实测结论 |
|---|---|---|
| `ratekit` | PR3 `percent_of`「修复」（abs → ×-1） | 批量结论 `reject`，识破 |
| `pipekit` | PR2 `drop_ranges`「修复」（根因未动） | fix 阶段 `fail`，识破 |
| `textkit` | PR2 `slugify` 半吊子修复 | fix 阶段 `fail`，识破 |

> `textkit` PR2 最经典：它用 `\s+` 折叠连续空格，`Hello   World` 确实修对了，
> 但 `Hello - World`（空格+连字符混排）仍产出 `hello---world`；**只看目标用例的验证器会判它可合并**。

### 回归门拦截（F2P 成立但弄坏别处）

| 夹具 | 样本 | 实测结论 |
|---|---|---|
| `ratekit` | PR2 `is_weekend` 修复 | `regression:fix` = **fail**（exit=1）→ 转人工 |
| `pipekit` | PR4 `merge_adjacent` 修复 | `regression:fix` = **fail** |
| `envkit` | PR4 `sanitize` 修复 | `regression:fix` = **fail** |
| `lockkit` | PR4 `should_retry` 合同被破坏 | 既有测试 `1 failed, 86 passed`（构造期实测） |

四套夹具的「回归门对抗样本」**全部有效**。

---

## 4. 已知失败与偏差（公开保留，不美化）

| 项 | 说明 |
|---|---|
| **`envkit` 5 处 tier 期望偏差** | 夹具 README 期望 #2/#4/#6/#8/#9 为 `tier2`，实测均为 `tier1`。两种读法都写出：① README 期望值过期（`tier1.min_importance` 开发期由 70 调到 80，而这 5 条重要性均 ≥ 80，判 tier1 **符合当前配置**）；② 若以 README 为规格，则是阈值标定问题。**不改 README**——期望表是夹具的规格，不该为了让实测好看而修改 |
| **`lockkit` 4 处 tier 期望偏差** | README 期望 #1/#3/#5/#8 为 `tier1`，实测**全部 `tier2`**（重要性 55~78，够不到 `min_importance`）。**功能后果为零**：tier1 与 tier2 **都会进入自动修复队列**，只影响标签写成 tier1 还是 tier2 |
| 两者**方向相反** | `envkit` 期望 tier2 的实测判 tier1；`lockkit` 期望 tier1 的实测判 tier2。**说明这是阈值标定问题，而非「模型一律偏松 / 偏严」** |
| `textkit` 缺少回归门数据 | 跑在回归门功能合入之前，**没有经过回归门检验** |
| `textkit` 的 18 次 Agent error | Agent 循环的失败尝试属自纠错中间态，但数量偏高，说明**早期 Agent 稳定性不足** |
| 修复尝试的结果分布 | 全部为 dry-run（`patch_only`）；`needs_manual` 在这里是**预期语义**（补丁已产出但未提 PR） |

---

## 5. 尚未测量

| 项 | 状态 |
|---|---|
| `pipekit` Issue 侧自动修复 | **已跑通**（2026-10-14）：7 条待修全部在 2~3 轮内产出补丁（7/7）。此前曾是 0/2、**一次都没落笔** |
| `lockkit`（15 Issue + 5 PR） | **已测量**（2026-10-13）：27 条全部有结论；`lockkit#2` 3 轮修好并提出 **PR #28** |
| 真实提 PR（`mode: fork`） | **已测量**（2026-10-13）：`envkit#4` → PR #31、`lockkit#2` → PR #28（open）；两处改动都只有源码文件、**未夹带验证器测试文件** |
| 四平台链路 | **只读全部跑通**（GitHub / AtomGit / Gitee / 极狐 GitLab）；**写操作** GitHub / 极狐 GitLab / AtomGit 已验证打标签、建分支、提 PR；**Gitee 写操作未验证**（建 Issue 被平台限制挡住） |
| **PostgreSQL 后端** | **未在真跑里用过**——开发期使用 SQLite |
| 冻结版本的可复现跑分 | **已冻结四份基线**（10-10 `7351432`、10-13 `6f1f03b`、10-14 `8a6e760`、10-15 `6d8ab43`），均在「工作区干净」前提下 capture |

### 冻结基线

| 基线 | 冻结时间 | commit | 工作区 | 模型 | 条目 | 验证器运行 | 修复尝试 |
|---|---|---|---|---|---|---|---|
| `7351432-baseline` | 2026-10-10 | `7351432a` | 干净 | qwen3.6-plus | 103 | 467 | 71 |
| `6f1f03b-baseline` | 2026-10-13 | `6f1f03b0` | 干净 | deepseek-v4.1-flash | 56 | 125 | 9 |
| `8a6e760-baseline` | 2026-10-14 | `8a6e7609` | 干净 | deepseek-v4.1-flash | 78 | 230 | 32 |
| `6d8ab43-baseline` | 2026-10-15 | `6d8ab43c` | 干净 | deepseek-v4.1-flash | 118 | 257 | 38 |

四份都用 `fissue bench capture` 冻结（**只读数据库**，不触发抓取/评测），`fissue bench list` 可复核。
**逐对都不可比**（commit / 模型 / 仓库集不同），`bench diff` 一律标「不可比——结论不能当回归依据」。

> 最新一份（10-15）是在上一份之后又改掉 **6 处代码**后冻的；它的 `items.by_repo` 已含 **9 个仓库**——
> **读它的增量前务必先看仓库集**（那条判定就是这么提示的）。

---

## 6. 已知的方法学局限

1. **非确定性**：LLM 输出每次都不同。数字是**单次历史测量**，不是稳定保证，重跑会漂移。
2. **非冻结**：主表测量跨越 2026-10-03 ~ 10-05，期间 Fissue 自身在修 bug，不同夹具跑在**不同版本**上。
3. **累计数据**：一个条目可能有多次评测记录，取**最新一次**结论，但阶段统计是**累计**的。
4. **模型差异**：换一个 LLM 供应商，全部数字都会变。
5. **夹具偏差**：五套夹具由本项目设计，可能存在「照着 Fissue 的长处出题」的偏差。
   反面证据：`pipekit` 的 Issue 侧自动修复曾**整整一天跑不通**，是靠**改提示词**（而不是换模型）才转正的。

---

## 7. 结论

**站得住的**：

- 回归门在 3 套夹具上**全部拦下**了「修好目标却弄坏别处」的假修复——这是 F2P 单独做不到的。
- 假修复（F2P 不成立）被识破；刷量、误报、设计偏好变更均被拦在自动修复之外。
- 重复检测能召回中文同义重复，且不误标「同源但非重复」。
- **真实提 PR 路径已端到端跑通**（`envkit#4` → PR #31、`lockkit#2` → PR #28）。
- **`lockkit` 首次测量完成**：27 条全部有结论。

**站不住的 / 公开保留**：

- 「跨模块根因定位」这一环**曾**拿不到正例（`pipekit` 0/2）——该结论**同日被自己推翻**：只改提示词后 `pipekit` **7/7**。
  保留记录是为了说明**先怀疑判据与提示词、再谈换模型**这条顺序的价值。
- tier 标定存在**公开保留**的期望不一致（`envkit` 5 处、`lockkit` 4 处，方向相反）。

> 我们宁可把上面这段写出来，也不愿给一份「全部通过」的漂亮表格——那正是 Fissue 存在的意义所反对的事。

---

## 8. 如何自己复现

```bash
# 1) 准备环境（见 docs/USAGE.md 第 1~6 节）
python -m venv .venv && source .venv/Scripts/activate
pip install -e ".[dev]"
cp .env.example .env && cp config.example.yaml config.yaml
fissue db init

# 2) 夹具仓库需先自行推送到平台（这一步会产生平台写操作）

# 3) 跑完整链路
fissue fetch  --repo John-is-playing/ratekit --limit 50
fissue eval   --repo John-is-playing/ratekit --all
fissue verify --repo John-is-playing/ratekit
fissue flush  --queue verify
fissue fix    --repo John-is-playing/ratekit --dry-run --limit 6

# 4) 出报告，与夹具 README 的期望对照表逐项核对
fissue report --repo John-is-playing/ratekit --format markdown --out data/exports/ratekit.md
```

做**正式跑分**请用 `fissue bench`（见[开发与发布 §跑分基线](Development.md#7-跑分基线fissue-bench)）——
它同时记录**前提**与**结果**，并**先判可比性**再给数字。

---

## 相关页

[项目概览](Overview.md) · [验证器与 F2P](Verifier-and-F2P.md) · [贡献指南](Contributing.md) · [开发与发布](Development.md) · [BENCHMARK.md](../../BENCHMARK.md)
