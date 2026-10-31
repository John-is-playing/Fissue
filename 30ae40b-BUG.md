# ratekit 实测缺陷清单与修复记录

> 基线 commit `30ae40b`（新增 ratekit 夹具 10 Issue + 3 PR）。用 `demo/ratekit` 完整跑通
> `fetch → eval → verify → flush → fix --dry-run` 后，逐项比对「期望 vs 落库」发现的差异。
> 只记录**已定位根因**的问题；前半是修复前诊断，§5–§6 是实际改动与验证。

## 0. 前置结论

排查前基线：`pytest -q` → **394 passed**。所以下面不是「改坏了」，而是换到
「领域不同、中文标题、含重复/刷量/误报」的场景后才照出来的问题。

ratekit 整体表现：6 个可复现 Issue 的 F2P + 回归门 + 修复补丁全部通过；
PR#12「修好目标却弄坏别处」被回归门拦下；PR#13 假修复被 `reject`；
#10 刷量判 `authenticity=20` + `is_spam=true` + `action=close`。问题集中在
**重复检测、优先级落库一致性、报告口径**三处。

## 1. 期望 vs 实际（修复前）

| # | 条目 | 期望 | 实际 | 判定 |
|---|---|---|---|---|
| 1 | format_amount 丢小数位 | bug / tier1 | tier2 | ⚠️ 差一档 |
| 2 | remove_tax 公式错 | tier1 | tier1 | ✅ |
| 3 | parse_amount 丢负号 | tier1 | tier1 | ✅ |
| 4–5 | percent_of 负数 / is_weekend 漏周六 | tier2 | tier2 | ✅ |
| 6 | 精度口径不统一 | 不自动修 | base 即 pass → 未入队 | ✅ |
| 7 | 按小时计费 | feature | labeled | ✅ |
| 8 | 含税反推不对（重复 #2） | 判重复 #2 | **未判重复**，独立入 fix 队列 | ❌ 缺陷 A |
| 9 | 非法输入应返回 0（误报） | 低真实性 | authenticity=90 | ⚠️ 提升项 F |
| 10 | 性能 + 推广（刷量） | 低真实性 | auth=20 / spam / close | ✅ |
| PR#11 | remove_tax 完整修复 | 建议合并 | `merge` | ✅ |
| PR#12 | 修好目标却弄坏别处 | 回归门拦 | `regression:fix` fail → 转人工 | ✅ 最关键 |
| PR#13 | 假修复（abs→×-1） | 不合并 | `reject` | ✅ |

PR#12 无批量结论不是漏跑：`stages.py` 的 strict 分支在回归门不通过时直接 `return`，
拦截项不入 `fix_bug` 队列——设计使然。

## 2. 确凿缺陷

### 🔴 A. 中文标题的重复预筛失效（`pipeline/stages.py`）

`_similar_to` 只比对标题、Jaccard 阈值 0.25，而 `_title_tokens` 把中文切成**单字**，
单字稀释相似度：#8 与 #2 的 Jaccard 仅 0.208 < 0.25，**候选都没进**，LLM 根本没机会判重复。

textkit 没暴露是因为英文标题命中 `slugify` 这类 ASCII 词会被抬到 0.5；换成中文仓库是
**系统性失效**。

### 🟠 B. flush 落库覆盖规则优先级（`store/repository.py`）

只有 verify 队列**派发**走了 `_rule_priority` 纠偏，`save_batch_conclusion` **落库**却直接写
LLM 给的 priority。后果：报告/看板/API 与规则结论自相矛盾（#1 tier1→tier2、PR#11 应为
none 却写 tier1），破坏 `compute_priority` 的不变式。实际派发安全，但报告不可信。

### 🟡 C. dry-run 下 fix 汇总与明细口径打架

`FixReport.record` 把「补丁已产出」计入 `succeeded`，而 `_attempt_row` 又输出
`needs_manual`——同屏出现「成功 6，需人工 0」与「全部 needs_manual」。

## 3. 提升项

| 项 | 现象 | 方向 | 状态 |
|---|---|---|---|
| D | `kind=executable` 却缺 `command`，静默退化成读输出判定，f2p=0 | 带反馈重生成，末轮仍缺则转人工 | 已实现 |
| E | 难度/重要性阈值边界太陡，#1 与 #3 同难度差一档就翻转 | 阈值可配 + 分档策略 | 已实现 |
| F | 「API 设计偏好」被判高真实性（#9 auth=90） | 提示词区分「缺陷」与「行为偏好变更」 | 已实现 |

## 4. 验收方式

1. 只跑单测：`pytest -q`，确认无回归（不连网、快）；
2. 单测 + 重跑 ratekit：验证 #8 判重复、#1 回 tier1、PR 一律 none、dry-run 口径一致。

## 5. 修复记录（A / B / WSL）

### 5.0 顺带发现：Windows 误选 WSL bash（`sandbox/runtime.py`）

同一份代码 Git Bash 下全绿、PowerShell 下 10 个用例全红，报
`WSL ... CreateProcessCommon`。根因：`which("sh") or which("bash")` 在取不到 `sh` 时命中了
`C:\Windows\System32\bash.EXE` 垫片，而垫片不认 `-lc`。改法：新增 `_find_posix_shell`，
排除 WSL 垫片与 WindowsApps 别名，PATH 无解时从 `git` 安装根推导 `usr\bin\sh.exe`。
验证：受限 PATH 下 78 + 3 passed，全量 396 passed。

### 5.1 A — 中文重复预筛

| 项 | 修复前 | 修复后 |
|---|---|---|
| 中文分词 | 单字 | **字符二元组 bigram** |
| 相似度 | Jaccard | **包含度**（交 / 较短一侧） |
| 阈值 | 0.25 | **0.22** |

两个关键点都不是拍脑袋：

1. **只换 bigram 不够**——bigram 同时放大并集，Jaccard 反而从 0.208 降到 0.16，必须换成
   包含度（回答「较短标题是否基本被较长标题覆盖」，对长度差不敏感）。
2. **0.22 是实测标定**：真重复对 #2~#8 得 0.286，同属 `parse_amount` 但属设计偏好的 #3~#9 得
   0.154，阈值落在中间，两侧各留 0.066 间隔。

验证：`#8 -> [(2, 0.29)]`（修复前 `[]`），其余 9 条无新增误召回；端到端 `eval --all` 后
#8 `is_duplicate=true`、`duplicate_of=[2]`、`action=triage`、`priority=none`，
不再为重复 Issue 生成第二份补丁。

### 5.2 B — 结论优先级以规则为准

在 `pipeline/flush.py` 把规则优先级从「verify 队列专属」提升为**所有队列统一**，并回写进结论：

```python
priority = self._rule_priority(key, payload, fallback=priority)
c["priority"] = priority
```

之所以不改仓储层：`flush.apply` 是所有 flush 的唯一入口，在那里纠偏最集中；改仓储会让存储层
反向依赖业务规则。验证：PR#11 的 `items.priority` 与批量结论均由 tier1 变 **none**，同轮 F2P
仍成立。

### 5.3 修复后复测（A/B）

#1 回到 tier1、#8 判重复、11–13 三个 PR 一律 none；#4/#5 仍偏高（重要性 75 越线），属标定
问题（E），非本次改动引入。

### 5.4 改动清单

| 文件 | 主题 |
|---|---|
| `pipeline/stages.py` | A：中文 bigram + 包含度 + 阈值 0.22 |
| `pipeline/flush.py` | B：规则优先级对所有队列生效并回写落库 |
| `sandbox/runtime.py` | WSL：Windows 下正确选 POSIX shell |
| `tests/test_pipeline.py` | 新增 2 个回归单测 |

全量单测 394 → 396 passed，Git Bash 与受限 PATH 下均全绿。

## 6. 后续修复（C / D / E / F）

### 6.1 C — 统一 dry-run 口径

按「补丁已产出即修复成功」这一既有语义统一到**明细侧**（既有测试已锁定该语义）：
`FixReport` 新增 `dry_run_patches` 计数，`summary` 追加「（其中 dry-run 产出补丁 N）」；
`_attempt_row(a, dry_run=...)` 在 dry-run 且有补丁时显示 `success`，非 dry-run 维持原状，
不把真失败冒充成功。

### 6.2 D — 可执行验证器缺 command 时重生成

落点在 `verifier/runner.py:generate_and_validate`（不是 `generator.py`——`validate_spec` 只产生
warning，无法触发重生成）：缺 `command` 时带明确反馈走既有 `refine` 回路；末轮仍缺则判不可靠
转人工，不再空跑沙盒。

### 6.3 E — 分档策略可配置（`fix_policy.tier_strategy`）

| 策略 | 行为 |
|---|---|
| `importance`（默认） | 难度是硬门槛，`min_importance` 区分 tier1/tier2；与改动前一致 |
| `dual` | 难度、重要性各自判档，**取更严** |
| `custom` | 由 `custom_tier` 指定的用户函数判定，`min_importance` 不生效 |

custom 签名 `func(difficulty, importance, policy) -> "tier1"|"tier2"|"none"`（也接受 `Priority`），
支持 `pkg.mod:func` 与 `pkg.mod.func`；加载/执行失败记 warning 并回退默认策略（避免「配置写错
→ 静默不修」）。配置期校验：非法策略名、`custom` 未给 `custom_tier` 均抛 `ConfigError`。

### 6.4 F — 设计偏好判低真实性（`ai/prompts.py`）

- authenticity 维度点明它衡量「**这是否是一个真实存在的问题**」，不是「诉求是否合理」；
- 打分要求：行为/API 设计偏好变更 ≠ 缺陷，若只是「把现有行为换一种做法」且现有行为并非
  错误 → authenticity ≤40、category 取 feature、action 建议 triage；
- **反向条款**：若现有行为确实不符其文档/契约/常识（公式错、边界漏判），仍按真实程度给分——
  只压不抬会把真 Bug 误伤。

### 6.5 累计验证

单测 394 → 396 → 400 → 405 → 407 passed，无回归。

| commit | 主题 |
|---|---|
| `bda74c1` | A + B |
| `8e49b0c` | WSL shell 选择 |
| `ccd8d05` | 缺陷清单与修复过程 |
| `9427d9e` | C |
| `14dd85e` | D |
| `c79c65c` | E |
| `29b1e82` | F |
| `28420e6` | 补记 C/D/E/F |
| `0bd0e41` | F 收紧：不误伤新增功能 |

### 6.6 E/F 复测（`tier1.min_importance` 70 → 80）

| # | 条目 | 难度/重要性 | 期望 | 复测 | 判定 |
|---|---|---|---|---|---|
| 1 | format_amount | 15 / 85 | tier1 | tier1 | ✅ |
| 2 | remove_tax | 5 / 95 | tier1 | tier1 | ✅ |
| 3 | parse_amount 丢负号 | 15 / 85 | ~~tier2~~ → **tier1** | tier1 | ✅ 期望已修正 |
| 4 | percent_of | 10 / 65 | tier2 | tier2 | ✅ |
| 5 | is_weekend | 10 / 75 | tier2 | tier2 | ✅ |
| 6 | 精度口径 | 80 / 85 | none | none | ✅ |
| 7 | 按小时计费 | 45 / 60 | feature | feature / auth=90 | ✅ |
| 8 | 重复 #2 | 25 / 80 | 判重复 | dup=true, of=[2] | ✅ |
| 9 | 误报 | 30 / 40 | 低真实性 | auth=30 | ✅（原 90） |
| 10 | 刷量 | 50 / 10 | 低真实性 | auth=10 / spam=true | ✅ |
| 11–13 | 三个 PR | — | 一律 none | 全部 none | ✅ |

**#3 为何仍是 tier1**：门槛提到 80 后它重评重要性为 85。这不是阈值能解决的——「把正数金额当
负数解析」会让资金方向颠倒，模型判高重要性不算错，是**原期望偏低**。压到 tier2 需把门槛提到
86+，但会把 #1（85）一起打下去，得不偿失。已同步 `demo/ratekit/README.md` 与
`fixtures/issue-03-*.md` 的 target。

**F 两个方向都验到**：#9（设计偏好 90 → 30，属设计偏好变更非缺陷）；#7（新增功能
95 → 35 → 收紧后 90，明确新增能力的需求不被误伤）。

> 提示词与模型输出非确定，以上为单次实测，不是稳定保证。单测只锁定「规则写对了」。

---

相关：[BENCHMARK.md](BENCHMARK.md) · [demo/ratekit/README.md](demo/ratekit/README.md) ·
[docs/DESIGN.md](docs/DESIGN.md) · [docs/REGRESSION-GATE.md](docs/REGRESSION-GATE.md)