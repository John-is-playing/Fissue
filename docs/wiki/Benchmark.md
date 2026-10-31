# 基准与可信度

> 五套对抗性夹具上的真实测量，**包括已知失败**。逐条出处见 [BENCHMARK.md](../../BENCHMARK.md)。
> 三条规则：**只写实测**、**已知失败照写**、**没跑过就写没跑**。

## 1. 方法学

夹具是刻意构造的对抗性样本仓库：植入真实缺陷 + 一套**全绿但不覆盖缺陷**的基线测试，再混入假修复、
只修一半、重复提交、刷量、误报等干扰项。

| 夹具 | 领域 | 规模 | 考什么 |
|---|---|---|---|
| `textkit` | 文本处理 | 5 I + 2 PR | 单函数语义 |
| `ratekit` | 金额 / 费率 | 10 I + 3 PR | 数值边界 |
| `pipekit` | 分片流水线 / 区间 | 15 I + 5 PR | **跨模块根因定位** |
| `envkit` | 配置 / 缓存 / 校验 | 15 I + 5 PR | 状态副作用、环境依赖、输入校验 |
| `lockkit` | 并发原语 / 资源管理 | 15 I + 5 PR | 资源不变量、超时上限、一次性语义 |

合计 **60 Issue + 20 PR**，链路 `fetch → eval → verify(F2P) → 回归门 → flush → fix --dry-run`。

| 指标 | 通过条件 |
|---|---|
| F2P 成立 | base exit≠0 **且** fix exit=0 |
| 回归门通过 | base 既有测试绿 **且** fix 既有测试绿 |
| 重复检测命中 | `is_duplicate=true` 且 `duplicate_of` 指向正确编号 |
| 刷量/误报拦截 | 低真实性条目 `priority=none` 且**未**生成补丁 |
| 假修复识破 | F2P 不成立 **或** 回归门拦下 |

> **前提**：主表来自**开发期累计运行**（2026-10-03~05），期间 Fissue 自己在修 bug——这是历史测量
> 记录，不是「冻结版本的官方跑分」。LLM 非确定性，同配置重跑数值会漂移。

## 2. 结果摘要

| 夹具 | 条目 | F2P 成立 | 回归门 fix 通过 | 回归门 fix 失败 | 批量结论 |
|---|---|---|---|---|---|
| `textkit` | 5 I + 2 PR | 5 | 0（注 1） | 0（注 1） | 3 fix / 1 merge / 3 needs_review / 1 reject |
| `ratekit` | 10 I + 3 PR | 9 | 9 | **1** | 6 fix / 2 merge / 1 reject |
| `pipekit` | 15 I + 5 PR | 3 | 3 | **1** | 11 fix / 2 merge / 1 needs_review / 1 skip |
| `envkit` | 15 I + 5 PR | 12 | 19 | **1** | 11 fix / 2 merge / 2 needs_review |
| `lockkit` | 15 I + 12 PR | 7 | 7 | **1** | 8 fix / 6 merge |

注 1：`textkit` 跑在回归门功能合入之前，历史记录里没有 `regression:*`；2026-10-30 在 MySQL 后端
补跑 `#1/#6/#7` 三条（六次运行全 pass），但**只有 3/7 条**。
注 2：`lockkit` 首次测量抓到 **27 条**（比自述 20 条多，多出来的是仓库既有其它 PR），**修复侧只跑
1 条**（`lockkit#2`），其余停在 `fix_queued`——反映的是「首次接入 + 单条修复通路」，不是全量跑分。

### 沙盒阶段明细

| 夹具 | base fail（可复现） | base pass（不可靠） | base error | fix pass | fix fail |
|---|---|---|---|---|---|
| `textkit` | 8 | 6 | 0 | 10 | 1 |
| `ratekit` | 10 | 1 | 0 | 9 | 1 |
| `pipekit` | 16 | 0 | 15 | 3 | 1 |
| `envkit` | 24 | 0 | 0 | 19 | 1 |
| `lockkit` | 22 | 0 | 0 | 8 | 0 |

`base pass` = 验证器不可靠；`base error` = 沙盒自身出错（结论不可信）；`regression:fix fail` = **成功
拦下的「修好目标却弄坏别处」**。

> `pipekit` 的 15 次 `base error` 属**同一个瞬态环境故障**（当晚基础镜像未构建、拉取又超时），
> 每条随后重跑成功（重跑 `error` 为 0）。**不作为能力证据**。

### 提示词重写前后（同一批待修条目）

| | 尝试 | 产出补丁 | 轮次分布 |
|---|---|---|---|
| 重写前 | 13 | **5（38%）** | 0~12 轮，含一次 12 轮纯空转（一个文件都没写） |
| 重写后 | 19 | **19（100%）** | 18 条恰好 2 轮，1 条 3 轮 |

逐仓库：`envkit` 5/5、`lockkit` 7/7、`pipekit` **7/7**；花费 19 条共 41 次调用、246,888 tokens。
不是「能力突然变强」，而是**把纪律写进提示词**后不再空转：判据从「读够了没」换成「够了就动手」。

## 3. 关键对抗样本

**假修复（F2P 不成立 → 拒绝）**：`ratekit` PR3 `percent_of`（abs → ×-1）`reject` · `pipekit` PR2
`drop_ranges`（根因未动）fix fail · `textkit` PR2 `slugify` 半吊子修复 fix fail。

> `textkit` PR2 最经典：用 `\s+` 折叠空格，`Hello   World` 修对了，但 `Hello - World` 仍产出
> `hello---world`；**只看目标用例的验证器会判它可合并**。

**回归门拦截（F2P 成立但弄坏别处）**：`ratekit` PR2 `is_weekend` · `pipekit` PR4 `merge_adjacent` ·
`envkit` PR4 `sanitize`（`regression:fix` = fail）· `lockkit` PR4 `should_retry` 合同被破坏
（既有测试 `1 failed, 86 passed`）。四套夹具的对抗样本**全部有效**。

## 4. 已知偏差（公开保留）

| 项 | 说明 |
|---|---|
| `envkit` 5 处 tier 偏差 | README 期望 #2/#4/#6/#8/#9 为 tier2，实测 tier1。两种读法都写：① README 期望过期（`min_importance` 开发期 70→80，这 5 条重要性 ≥80）；② 若以 README 为规格则是标定问题。**不改 README**——期望表是夹具的规格 |
| `lockkit` 4 处 tier 偏差 | 期望 #1/#3/#5/#8 为 tier1，实测全 tier2（重要性 55~78）。**功能后果为零**：两个 tier 都进修复队列，只影响标签 |
| 两者方向相反 | 说明这是**阈值标定问题**，不是「模型一律偏松/偏严」 |
| 2026-10-30 复测 | `envkit` 上次 5 处全部转命中，却反向多出 `#3/#5/#7`；`lockkit` 4 → 5 处。→ **偏差清单每次都在变**：评分密集在 62~82，门槛 80 正压右端，单次抖动即可跨线。处置：不改夹具期望、不调阈值凑命中 |
| `textkit` 缺回归门数据 / 18 次 Agent error | 未经回归门检验；早期 Agent 稳定性不足 |
| 修复结果分布 | 全部 dry-run（`patch_only`），`needs_manual` 是**预期语义**（补丁已产出但未提 PR） |

## 5. 已测量与尚未测量

| 项 | 状态 |
|---|---|
| `pipekit` Issue 侧修复 | **7/7**（2026-10-14）——此前是 0/2、一次都没落笔 |
| `lockkit` 全量 | **已测量**（10-13）：27 条全有结论，`lockkit#2` 3 轮修好并提 **PR #28** |
| 真实提 PR（`mode: fork`） | **已测量**：`envkit#4` → PR #31、`lockkit#2` → PR #28，改动只有源码文件、未夹带验证器测试 |
| 四平台链路 | **只读全部跑通**；写操作 GitHub / 极狐 GitLab / AtomGit 已验证。**Gitee** 打标签、评论、仓库级写都通，只有「建/改 Issue」404（先前的「打标签被拒」实为我们发错请求体形状，已修） |
| PostgreSQL 后端 | **已测量**（`1.1.0-b4`）真实部署验证；验证的是部署与并发路径，跑分口径仍来自 SQLite |
| 冻结基线 | 四份（10-10 `7351432`、10-13 `6f1f03b`、10-14 `8a6e760`、10-15 `6d8ab43`），均在干净工作区 capture。**逐对都不可比**，`bench diff` 会明确标出 |

## 6. 方法学局限

非确定性（LLM 每次输出不同，数字是单次历史测量）· 非冻结（主表跨 2026-10-03~05，各夹具跑在不同
版本）· 累计数据（结论取最新一次，阶段统计是累计）· 模型差异（换供应商全部数字都变）· 夹具偏差
（由本项目设计，可能「照着长处出题」——反面证据是 `pipekit` 曾整天跑不通，靠改提示词转正）。

## 7. 结论

**站得住**：回归门在 3 套夹具上全部拦下假修复 · 假修复 / 刷量 / 误报 / 设计偏好变更被拦在自动修复
之外 · 重复检测能召回中文同义重复且不误标 · 真实提 PR 端到端跑通 · `lockkit` 首次测量完成。

**公开保留**：「跨模块根因定位」曾拿不到正例（`pipekit` 0/2），同日被自己推翻（只改提示词后 7/7）
——保留记录是为说明「先怀疑判据与提示词、再谈换模型」这条顺序的价值；tier 标定存在方向相反的期望
不一致。

## 8. 自己复现

```bash
python -m venv .venv && source .venv/Scripts/activate && pip install -e ".[dev]"
cp .env.example .env && cp config.example.yaml config.yaml && fissue db init
# 夹具仓库需先自行推送到平台（这一步产生平台写操作）
fissue fetch  --repo John-is-playing/ratekit --limit 50
fissue eval   --repo John-is-playing/ratekit --all
fissue verify --repo John-is-playing/ratekit
fissue flush  --queue verify
fissue fix    --repo John-is-playing/ratekit --dry-run --limit 6
fissue report --repo John-is-playing/ratekit --format markdown --out data/exports/ratekit.md
```

正式跑分请用 `fissue bench`：它同时记录前提与结果，并先判可比性。

---

相关：[项目概览](Overview.md) · [验证器与 F2P](Verifier-and-F2P.md) · [贡献指南](Contributing.md) ·
[开发与发布](Development.md) · [BENCHMARK.md](../../BENCHMARK.md)