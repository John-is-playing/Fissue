# 30ae40b 缺陷与提升清单（ratekit 夹具实测）

> 基线 commit：`30ae40b test: 新增 ratekit 纯净测试夹具（10 Issue + 3 PR）`
> 来源：用 `demo/ratekit` 夹具完整跑通 `fetch → eval → verify → flush → fix --dry-run` 后，
> 逐项比对「期望结果」与「实际落库」时发现的差异。
> 说明：本文件只记录**已定位根因**的问题，每条都给出代码位置与证据。

---

## 0. 前置结论：项目自身是健康的

跑缺陷排查前先确认基线：

```text
.venv/Scripts/python.exe -m pytest -q
→ 394 passed, 1 warning in 173.13s
```

**394 项单测全绿**。因此下面这些不是「改坏了」，而是 ratekit 换了一个
**领域不同、语言偏中文、包含重复/刷量/误报**的真实场景后，才被照出来的问题。

ratekit 整体表现（详见期望对照表）：

- 6 个可复现 Issue：F2P 验证 + 回归门 + 自动修复补丁，全部通过；
- PR#12「修好目标却弄坏别处」：回归门 `regression:fix` = **fail**，成功拦下；
- PR#13「假修复」：F2P 不成立 → `reject`；
- #10 刷量：`authenticity=20` + `is_spam=true` + `action=close`，判定正确。

问题集中在**重复检测**、**优先级落库一致性**、**报告口径**三处。

---

## 1. 期望 vs 实际对照表

| # | 条目 | 期望分类/优先级 | 实际 | 判定 |
|---|---|---|---|---|
| 1 | format_amount int 丢小数位 | bug / tier1 | bug / tier2 | ⚠️ tier 差一档 |
| 2 | remove_tax 公式错 | bug / tier1 | bug / tier1 | ✅ |
| 3 | parse_amount 丢负号 | bug / tier2 | bug / tier1 | ⚠️ 偏高 |
| 4 | percent_of 负数取绝对值 | bug / tier2 | bug / tier2 | ✅ |
| 5 | is_weekend 漏周六 | bug / tier2 | bug / tier2 | ✅ |
| 6 | 精度口径不统一 | 不自动修 | base 即 pass → f2p=0 → 未入队 | ✅ 高难度被拦住 |
| 7 | 按小时计费 | feature / 只打标 | feature / labeled | ✅ |
| 8 | 含税反推不对（重复 #2） | 疑似重复 #2 | **未判重复**，独立验证并入 fix 队列 | ❌ 见缺陷 A |
| 9 | 非法输入应返回 0（误报） | 低真实性 | **authenticity=90**，未误修 | ⚠️ 见提升项 F |
| 10 | 性能+推广（刷量） | 低真实性 | auth=20 / spam=true / close | ✅ |
| PR1 | #11 remove_tax 完整修复 | F2P 成立 → 建议合并 | `merge` | ✅ |
| PR2 | #12 修好目标却弄坏别处 | F2P 成立但回归门拦 | `regression:fix` = fail（exit=1）→ 转人工 | ✅ **最关键一项生效** |
| PR3 | #13 假修复（abs→×-1） | F2P 不成立 → 不合并 | `reject` | ✅ |

PR#12 回归门的实际失败输出（`data/artifacts/github_John-is-playing_ratekit#12/regression_fix/stdout.log`）：

```text
..FFF...............................                                     [100%]
test_add_working_days_over_weekend: 2024-01-09 != 2024-01-08
test_add_working_days_span:         2024-01-16 != 2024-01-15
test_add_working_days_zero:         ...
```

> PR#12 没有批量结论，不是漏跑：`stages.py:471-477` 的 strict 分支在回归门不通过时
> 直接 `return`，拦截项**不入 fix_bug 队列**——这是设计使然。

---

## 2. 确凿缺陷（已定位到代码行）

### 🔴 A. 中文标题的重复预筛失效 → #8 漏检

**位置**：`src/fissue/pipeline/stages.py:687`（`_title_tokens`）、`:699`（`_similar_to`）

**根因**：`_similar_to` 只比对**标题**，相似度用 Jaccard，阈值 `≥ 0.25`；
而 `_title_tokens` 把中文切成**单字**。单字严重稀释相似度。

**实测**（用项目自身函数复现）：

```text
#8 tokens: ['不','价','像','出','反','含','好','对','总','推','来','的','税']
#2 tokens: ['remove_tax','严','价','低','偏','公','净','反','含','式','推','税',...]
Jaccard = 0.208  →  < 0.25，未过阈值
#8 的相似候选 = []          ← LLM 根本没收到 #2 作为候选
```

**影响**：`#8` 与 `#2` 语义高度重合（都是「含税反推不对」），却因为标题字面
Jaccard 只有 0.208，连候选都进不了重复判定。这不是模型判错，是**预筛阶段就漏了**。

**为什么 textkit 没暴露**：textkit 是英文标题，命中 `slugify` 这类 ASCII 词会被
直接抬到 `score = 0.5`（`_similar_to` 里的 ASCII 特判），天然过阈值。换成
AtomGit / Gitee 的中文仓库，这是**系统性失效**。

**修法（择一或组合）**：
- 中文改用**字符二元组（bigram）**做相似度，替代单字；
- 或对 CJK 单独设更低阈值（单字交集 ≥ N 个即入选）；
- 或把「标题 + 正文摘要」共同纳入预筛，而不只是标题。

**可补单测**：构造一对中文近义标题，断言 `_similar_to` 能召回；构造一对中文无关标题，断言不误召回。

---

### 🟠 B. flush 把 LLM 的 priority 写库，与规则结论矛盾

**位置**：`src/fissue/store/repository.py:788`（`save_batch_conclusion` 里
`item.priority = priority.value`）

**根因**：只有 verify 队列的**派发**走了 `_rule_priority` 规则纠偏
（`src/fissue/pipeline/flush.py:117-121`），**落库**没有纠偏，直接写入批量结论里
LLM 给的 priority。

**实际后果**（来自 ratekit 落库：

| 条目 | 评测阶段（`compute_priority` 规则） | `items` 表（被覆盖后） |
|---|---|---|
| issue #1 | tier1 | **tier2** |
| PR #11 | none（`only_issues=true` 时 PR 应恒为 none） | **tier1** |

**影响**：
- 报告 / 看板 / API 显示的优先级与规则结论**自相矛盾**；
- 破坏 `compute_priority` 在 `evaluator.py:255-262` 文档中明确的不变式：
  「`only_issues` 为真时 PR 一律 none」。

**注意**：实际派发是安全的（verify 队列有纠偏），所以这是**展示层与存储层的一致性 bug**，
不会导致误修，但会让报告不可信。

**修法**：写库前对**所有队列**套用 `_rule_priority`；或至少在 `save_batch_conclusion`
内对 PR / 重复 / 非 bug 条目兜底为 `Priority.NONE`。

**可补单测**：对 PR 条目写一条 batch conclusion（priority=tier1），断言 `items.priority == none`。

---

### 🟡 C. dry-run 下 fix 汇总与明细口径打架

**位置**：`src/fissue/fixer/autofix.py:78-92`（`FixReport.record`）与
`src/fissue/cli/ops.py:106-114`（`_attempt_row`）

**根因**：dry-run 且产出 patch 时，`record()` 把 `NEEDS_MANUAL` 计入 `succeeded`
（`:87-90`，语义是「补丁已产出即修复成功」），但 `_attempt_row()` 又原样输出
`attempt.outcome`（`needs_manual`）。

**实际输出**：同一屏里

```text
修复尝试 6，成功 6，需人工 0，跳过 0，失败 0，产出 PR 0      ← 汇总
│ github:.../ratekit#1 │ needs_manual │ 2 │ - │ ...patch │   ← 明细
```

**影响**：汇总说「全部成功」，明细说「全部需人工」，读者无法判断到底成没成。
`record()` 的文档注释（`autofix.py:65-67`）明确写着要「避免让报告里的数字完全不可信」，
但两处口径没对齐，目标没达成。

**修法**：汇总与明细用同一口径——要么明细也把 dry-run 的 patch 显示为成功态，
要么汇总单独列出「dry-run 产出补丁 N 条」，不走 `succeeded` 计数。

---

## 3. 提升项（非缺陷，属质量与标定）

### D. 验证器缺 `command` 只告警不重试

**位置**：`src/fissue/verifier/generator.py:104`（`warnings.append("可执行验证器缺少 command")`）

**现象**：ratekit #6 生成出一个 `command=None` 的「可执行」验证器（`verify-6`），
退化成「读输出判定」，最终 `f2p_satisfied=0`。整个生成过程只 debug 了一条告警。

**方向**：对 `kind=executable` 却缺 `command` 的产物，**自动重生成一轮**（复用现有的
`generation_rounds` 机制），而不是默默降级。

### E. 难度阈值边界太陡

**现象**：#1 难度 20 → tier2，#3 难度 15 → tier1，一档之差就翻转优先级，
导致 #1（期望 tier1）落到 tier2、#3（期望 tier2）升到 tier1。

**方向**：标定 `fix_policy.tier1.max_difficulty`，或对难度接近边界的条目做平滑/人工复核，
避免临界值抖动。

### F. 「API 设计偏好」被判高真实性

**现象**：#9 提议「`parse_amount` 解析失败返回 0 而非抛异常」，被判定为
`authenticity=90`。它**不是缺陷**，是 API 行为设计偏好——真实性评分应反映
「这是否确为缺陷」，故期望判低。

**方向上**：评测提示词里明确区分「缺陷」与「行为偏好变更」，后者真实性应下调。
（好的一面：#9 未被误打修复标签、未误修，所以风险可控。）

> **状态：已实现**，见第 5.6 节。

---

## 4. 建议的修复与验收

**建议优先修 A + B**：

- A 影响中文仓库的核心能力（重复检测在中文场景系统性失灵）；
- B 是存储/报告一致性 bug，会破坏 `compute_priority` 的不变式。

**验收方式（二选一）**：

1. **只跑项目单测**：`.venv/Scripts/python.exe -m pytest -q`，确认 394 项无回归（不连网、快）；
2. **单测 + 重跑 ratekit 比对**：连 LLM，直接验证
   - #8 是否被判为 #2 的重复；
   - #1 是否回到 tier1、PR#11 是否回到 none；
   - dry-run 汇总与明细是否一致。

C / D / E / F 可作为后续打磨项。

---

## 5. 修复记录（A / B / WSL 三项已修并验证）

> 本节记录**实际改动**：根因、改法、验证证据。
> 上面第 1–4 节保留为「修复前」的原始诊断，便于对照。

### 5.0 修复期间新发现：Windows 本地沙盒误选 WSL bash

这个不在原清单里，是验证过程中暴露的**环境兼容 bug**，且此前一直潜伏。

**现象**：同一份代码，在 Git Bash 里跑全绿（396 passed），在 PowerShell 里跑
**10 个用例全红**，报错都带同一条：

```text
<3>WSL (4921 - Relay) ERROR: CreateProcessCommon:818:
    execvpe(/bin/...): No such file or directory
```

| 环境 | `shutil.which("sh")` | `shutil.which("bash")` |
|---|---|---|
| Git Bash PATH | `Git\usr\bin\sh.exe` | — |
| **PowerShell（系统 PATH 只有 `Git\cmd`）** | **None** | **`C:\Windows\System32\bash.EXE`（WSL 垫片）** |

**根因**：`src/fissue/sandbox/runtime.py:242` 的
`shutil.which("sh") or shutil.which("bash")` 在取不到 `sh` 时，回退命中了
**WSL 的 bash 垫片**。WSL 垫片不认 `-lc`，于是所有走**本地沙盒**的用例全废——
`test_sandbox_verifier`(6) + `test_regression_gate`(3) + `test_e2e`(2)，这 10 个
文件与 A/B 的改动毫无关系。

**改法**（`sandbox/runtime.py`，新增 `_find_posix_shell` + `_is_posix_shell`）：

1. 逐个试 `sh` / `bash`，**排除** WSL 垫片（`System32` 下的 `bash.exe` / `wsl.exe`）
   与 WindowsApps 应用别名；
2. PATH 里都没有时，从 `shutil.which("git")` 的安装根推导 `usr\bin\sh.exe`。

**验证**（受限 PATH，等价 PowerShell 环境）：

```text
_find_posix_shell() → C:\Program Files\Git\usr\bin\sh.exe     ← 不再落到 WSL
test_sandbox_verifier + test_regression_gate → 78 passed
test_e2e                                     → 3 passed
全量                                         → 396 passed, 1 warning
```

### 5.1 A — 中文重复预筛

**改法**（`pipeline/stages.py`）：

| 项 | 修复前 | 修复后 |
|---|---|---|
| 中文分词 | 单字（`[\u4e00-\u9fff]` 逐字） | **字符二元组 bigram** |
| 相似度 | Jaccard（交/并） | **包含度**（交 / 较短一侧） |
| 阈值 | 0.25 | **0.22** |

两个关键点，都不是拍脑袋定的：

1. **只换 bigram 不够**：bigram 会同时放大并集，Jaccard 反而从 0.208 降到 0.16。
   必须同时把度量换成包含度——包含度回答「较短的标题是否基本被较长标题覆盖」，
   对「一条啰嗦、一条精炼」的长度差不敏感。
2. **阈值 0.22 是实测标定**：真实重复对 #2~#8 得 **0.286**，而仅同属
   `parse_amount`、实为设计偏好而非重复的 #3~#9 只得 **0.154**。0.22 落在两者
   中间，两侧各留 0.066 间隔。

**验证**：

```text
修复前: #8 -> []                     ← 漏检（单字 Jaccard 0.208 < 0.25）
修复后: #8 -> [(2, 0.29)]            ← 正确召回 #2，其余 9 条无新增误召回
```

**端到端实证**（`eval --all` 重跑）：

| | 修复前 | 修复后 |
|---|---|---|
| `#8` `is_duplicate` | `false` | **`true`** |
| `#8` `duplicate_of` | `[]` | **`[2]`** |
| `#8` `action` / `priority` | `fix_now` / `tier1` | **`triage` / `none`** |

`reasons`：「内容与已开启的 #2 标题及描述高度一致；作者自述不确定是否重复」。
即：预筛把 #2 送进候选 → LLM 正确判重复 → `compute_priority` 因
`spam.suspicious` 归零，**不再为一条重复 Issue 生成第二份补丁**。
夹具 README 里「#8 → 疑似重复 #2」这条期望，现在成立。

**新增单测**：`test_similar_to_recalls_chinese_duplicate_titles`
（断言召回 #2、且不误召 #3）。

### 5.2 B — 结论优先级以规则为准

**改法**（`pipeline/flush.py`）：把规则优先级从「verify 队列专属」提升为
**所有队列统一**，并回写进结论：

```python
priority = self._rule_priority(key, payload, fallback=priority)
c["priority"] = priority          # 新增，让落库口径与派发一致
```

这一处同时管住两个落库点：`batch_conclusions.priority` 与 `items.priority`
（经 `save_batch_conclusion`）。

**为什么没动 `repository.py`**：`flush.apply` 是所有 flush 的唯一入口
（`make_handler` → `handler` → `apply` → 落库），在那里纠偏最集中；若改在仓储层
会让存储层反向依赖业务规则。

**验证**（真实链路 `verify #11` → `flush fix_bug`）：

| | 修复前 | 修复后 |
|---|---|---|
| `items.priority`（PR#11） | `tier1` ❌ | **`none`** ✅ |
| 最新批量结论 priority | `tier1` ❌ | **`none`** ✅ |

LLM 在 flush 里给的是 tier1，规则（`only_issues=true` → PR 恒为 none）纠偏生效
并落库；同轮 `verify` 显示 PR#11 的 F2P 依然成立（base fail → fix pass，回归门
双 pass），证明 B 未影响验证流程本身。

**新增单测**：`test_flush_conclusion_priority_follows_rules_for_pr`
（走**生产路径** `make_handler` → `apply` → 落库；LLM 给 tier1，断言 PR 落 none）。

### 5.3 修复后的期望对照表（`eval --all` 复测）

| # | 条目 | 期望 | 复测结果 | 判定 |
|---|---|---|---|---|
| 1 | format_amount int 丢小数位 | tier1 | **tier1** | ✅（修复前 tier2） |
| 2 | remove_tax 公式错 | tier1 | tier1 | ✅ |
| 3 | parse_amount 丢负号 | tier2 | tier1 | ⚠️ 仍偏高（见 E） |
| 4 | percent_of 负数 | tier2 | tier1 | ⚠️ 偏高（重要性 75 越线） |
| 5 | is_weekend 漏周六 | tier2 | tier1 | ⚠️ 同上 |
| 6 | 精度口径不统一 | 不自动修 | none / triage | ✅ |
| 7 | 按小时计费 | feature | feature / triage | ✅ |
| **8** | **重复 #2** | **判重复** | **dup=true, of=[2]** | ✅ **本次修复** |
| 9 | 误报 | 低真实性 | auth=90 | ⚠️ 未变（见 F） |
| 10 | 刷量 | 低真实性 | auth=15 / spam=true / close | ✅ |
| 11–13 | 三个 PR | 一律 none | **全部 none** | ✅（B 生效） |

> `#3/#4/#5` 的 tier 偏高**不是本次改动引入**——是 `compute_priority` 的阈值
> 标定问题（重要性 75 恰好越过 `tier1.min_importance`），即提升项 **E**，
> 需先定标定口径再动。

### 5.4 改动清单与验证

| 文件 | 主题 |
|---|---|
| `src/fissue/pipeline/stages.py` | A：中文 bigram + 包含度 + 阈值 0.22 |
| `src/fissue/pipeline/flush.py` | B：规则优先级对所有队列生效并回写落库 |
| `src/fissue/sandbox/runtime.py` | WSL：Windows 下正确选 POSIX shell |
| `tests/test_pipeline.py` | 新增 2 个回归单测 |

验证：全量单测 **394 → 396 passed**（新增 2 个），在 Git Bash 与受限 PATH
（等价 PowerShell）下均全绿。

**C / D / E / F 四项均已实现**，见第 6 节。

---

## 6. 后续修复记录（C / D / E / F）

### 6.1 C — dry-run 汇总与明细统一口径

**改法**（`fixer/autofix.py` + `cli/ops.py`）：按「补丁已产出即修复成功」这一**既有
语义**统一到明细侧（而非改汇总——既有测试已锁定该语义）。

- `FixReport` 新增 `dry_run_patches` 计数，`summary` 追加
  「（其中 dry-run 产出补丁 N）」，让口径在汇总里显式可见；
- `_attempt_row(a, dry_run=...)`：dry-run 下有补丁的 `NEEDS_MANUAL` 显示为
  `success`，error 改写为「dry-run：已产出补丁，未提 PR」；补丁路径照常给出。
  非 dry-run 时维持原状，不把真失败冒充成成功。

新增单测：`test_fix_report_dry_run_patch_counted_and_summary_labeled`、
`test_attempt_row_matches_summary_under_dry_run`。

### 6.2 D — 可执行验证器缺 command 时重生成

**改法**（落点在 `verifier/runner.py` 的 `generate_and_validate`，非清单原先写的
`generator.py`——`validate_spec` 只产生 warning，无法触发重生成）：

可执行形态却没有 `command` 时，带着明确反馈走既有 `refine` 回路重生成；
最后一轮仍缺命令则直接判不可靠转人工，**不再空跑沙盒**。对应 ratekit #6 的
`command=None` 的「可执行」验证器。

新增单测：`test_generate_and_validate_retries_when_executable_lacks_command`、
`test_generate_and_validate_unreliable_when_command_never_given`。

### 6.3 E — 优先级分档策略可配置

**改法**（`config.py` + `ai/evaluator.py`）：新增 `fix_policy.tier_strategy` 三选一。

| 策略 | 行为 |
|---|---|
| `importance`（默认） | 难度是硬门槛，`min_importance` 区分 tier1/tier2。**行为与改动前一致** |
| `dual` | 难度、重要性各自判档，**取更严**的一档；tier1 难度门槛更紧时才真正生效 |
| `custom` | 交由 `custom_tier` 指定的用户函数判定；**`min_importance` 不生效** |

custom 函数签名 `func(difficulty, importance, policy) -> "tier1"|"tier2"|"none"`
（也接受 `Priority`）；支持 `pkg.mod:func` 与 `pkg.mod.func` 两种写法。函数加载或
执行失败时记 warning 并**回退默认策略**，避免「配置写错 → 静默不修」；加载结果
`lru_cache`。配置期校验：非法策略名、`custom` 却未给 `custom_tier`，均直接抛
`ConfigError`。

这解决了本文件 §5.3 里 `#3/#4/#5` 的 tier 偏差：设 `tier1.min_importance: 80`
（或改 `dual` 并把 `tier1.max_difficulty` 收到 20）即可让它们落到 tier2。

新增单测：dual 取严、custom 用用户函数且门槛不生效、custom 不可用回退、
非法配置被拒。

### 6.4 F — 「API 设计偏好」判低真实性

**改法**（`ai/prompts.py`）：在 `evaluation_prompt` 两处加指引。

- authenticity 维度说明：点明它衡量的是「**这是不是一个真实存在的问题**」，
  **不是**「这个诉求是否合理」；
- 打分要求：**行为/API 设计偏好变更 ≠ 缺陷**。若诉求只是「把现有行为换一种
  做法」而现有行为并非错误 → authenticity 判 ≤40、category 取 feature、
  action 建议 triage；反之，现有行为确实不符合其自身文档/契约/常识预期
  （如公式算错、边界漏判）才是缺陷，按真实程度给分。

反向条款是关键：只压不抬会把真 Bug 也误伤成低真实性。

对应 ratekit #9（`parse_amount` 解析失败应返回 0），原先被判 `authenticity=90`。

新增单测：`test_evaluation_prompt_distinguishes_design_preference_from_defect`、
`test_evaluation_prompt_pr_variant_keeps_guidance`（PR 分支同样带该指引）。

### 6.5 累计验证

全量单测：`394 → 396 → 400 → 405 → 407 passed`，无回归。

| commit | 主题 |
|---|---|
| `bda74c1` | A + B：重复预筛支持中文、结论优先级以规则为准 |
| `8e49b0c` | WSL：Windows 本地沙盒不再误选 bash 垫片 |
| `ccd8d05` | docs：缺陷清单与修复过程 |
| `9427d9e` | C：dry-run 汇总与明细统一口径 |
| `14dd85e` | D：验证器缺 command 时带反馈重生成 |
| `c79c65c` | E：优先级分档策略可配置 |
| （本次） | F：设计偏好变更判低真实性 |

**第 3 节的 C / D / E / F 四项均已实现。**

