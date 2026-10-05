# Fissue 测试夹具（三）：pipekit

一个**全新的、纯净的**测试仓库夹具，与前两套完全独立：

| 夹具 | 领域 | 形态 |
|---|---|---|
| `textkit` | 文本处理 | 单模块、独立函数 |
| `ratekit` | 金额 / 费率 | 单模块、数值边界 |
| **`pipekit`** | **分片流水线 / 区间** | **多模块、调用链耦合** |

它一次覆盖 **15 个 Issue + 5 个 PR**，其中刻意混入错误、无效、同源但不重复、刷量的请求。

## 这套夹具回答什么

前两套的缺陷都落在**单个函数**里。`pipekit` 刻意把同一个语义约定
（**区间该怎么算端点**）散落到 `intervals`、`slices`、`filterops` 三处，
再用 `pipeline` / `stats` 把它们串成调用链。于是它回答更硬的问题：

> **当 bug 藏在跨模块的调用链上，Fissue 能不能顺着报错点找到真正的根因文件、
> 写出能真正复现的验证器，并且不把别处改坏？**

最具代表性的两项：

- **Issue #3**：报错点在 `stats.count_batches`，根因却在 `filterops.in_ranges`。
- **PR 2**：只在调用点打了个补丁，绕开根因——目标用例仍然失败，应当被识破。

## 目录结构

```
demo/pipekit/
├── repo/                        待推送的测试仓库（独立 git 仓库）
│   ├── pipekit/                 库源码（9 处植入缺陷，跨 6 个模块）
│   ├── tests/                   61 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                    15 个 Issue 的内容与元数据
│   ├── issue-01-merge-touching-intervals.md        tier1：端点相接不合并 → 自动修复
│   ├── issue-02-merge-adjacent-open-range.md       tier2：相邻半开切片不合并 → 自动修复
│   ├── issue-03-in-ranges-drops-right-endpoint.md  tier1：漏右端点（跨模块）→ 自动修复
│   ├── issue-04-coverage-double-counts-overlap.md  tier2：重叠重复计数 → 自动修复
│   ├── issue-05-deep-merge-mutates-base.md         tier1：原地改入参 → 自动修复
│   ├── issue-06-resolve-index-rejects-negative.md  tier2：拒负索引 → 自动修复
│   ├── issue-07-slice-tail-zero-returns-all.md     tier2：n=0 边界 → 自动修复
│   ├── issue-08-drop-ranges-uses-end.md            tier1：用终点判定 → 自动修复
│   ├── issue-09-feature-tree-grouping.md           FEATURE → 只打标签
│   ├── issue-10-window-stats-semantics.md          难度高（需决策）→ 不自动修复
│   ├── issue-11-normalize-should-return-bool.md    **误报** → 真实性应判低
│   ├── issue-12-duplicate-count-batches.md         **相关但非重复** → 判重从严，不误标
│   ├── issue-13-spam-performance-rewrite.md        **刷量** → 真实性应判低
│   ├── issue-14-batch-bounds-assumes-sorted.md     tier2：假设输入有序 → 自动修复
│   └── issue-15-vague-sometimes-wrong.md           **无效/无法复现** → 真实性应判低
└── scripts/setup_github.py      一键建仓库 / 建 Issue / 建 PR
```

## 设计意图：每个 fixture 考什么

### 植入的 9 处真实缺陷（基线测试刻意不覆盖）

| # | 位置 | 表现 | 期望 Fissue 行为 |
|---|---|---|---|
| 1 | `intervals.merge` | `[(1,3),(3,5)]` 不合并（闭区间端点相接漏了） | tier1 → 自动修复 |
| 2 | `slices.merge_adjacent` | `[(1,4),(4,6)]` 不合并（半开区间相邻漏了） | tier2 → 自动修复 |
| 3 | `filterops.in_ranges` | `in_ranges(5,[(1,5)])` 为 `False`（漏右端点） | tier1 → 自动修复（**跨模块**） |
| 4 | `pipeline.coverage` | `[(0,4),(2,6)]` 算成 10（重叠重复计数） | tier2 → 自动修复 |
| 5 | `config.deep_merge` | 返回结果的同时把入参 `base` 改掉了 | tier1 → 自动修复 |
| 6 | `resolver.resolve_index` | 负索引被拒（`-1` 本应取最后一个） | tier2 → 自动修复 |
| 7 | `slices.slice_tail` | `n=0` 返回整个列表 | tier2 → 自动修复 |
| 8 | `pipeline.drop_ranges` | 用**终点**判定，误删/漏删分片 | tier1 → 自动修复 |
| 9 | `stats.batch_bounds` | 假设输入已排序，乱序时返回非法区间 | tier2 → 自动修复 |

> **关键**：基线 61 个测试是**全绿**的——每处缺陷都藏在没被覆盖的边界上。
> 这逼 Fissue 真正去「读代码 → 写验证器 → 跑 F2P」，而不是抄现成测试。

缺陷 #1 / #2 是同一语义在两套坐标（闭区间 / 半开区间）下的**对称错误**：
它们不能靠「复制另一处的写法」来修，必须理解各自的口径。

缺陷 #3 与 #8 故意挨着：一个在 `filterops`（点是否在区间内），
一个在 `pipeline`（分片起点是否在区间内）。两者必须分开修，
不能用一个 `in_ranges` 的改动糊住两处。

### 五个 PR：真好 / 半吊子 / 文档冒充 / 回归门 / 无效

| PR | 分支 | 关联 | 性质 | 期望 Fissue 行为 |
|---|---|---|---|---|
| 1 | `fix/merge-touching-intervals` | #1 | ✅ 完整修复 + 回归测试 | F2P 成立 + 回归门通过 → 建议合并 |
| 2 | `fix/count-batches-endpoint` | #3 | ⚠️ **只改调用点，绕开根因** | F2P 不成立 → 建议不合并 |
| 3 | `fix/feature-tree-docs` | #9 | ❌ **只改文档，冒充功能实现** | FEATURE + 无代码 → 建议不合并 |
| 4 | `fix/merge-adjacent-window` | #2 | ⚠️ **修好目标却弄坏别处** | F2P 成立但**回归门拦住** → 建议不合并 |
| 5 | `fix/coverage-dedup` | #4 | ✅ 完整修复 + 回归测试 | F2P 成立 + 回归门通过 → 建议合并 |

**PR 2（半吊子）** 是最能体现「跨模块」价值的一项：它把
`drop_ranges` 换成内联的闭区间判断，看起来修好了报错点，
但根因 `filterops.in_ranges(5, [(1,5)])` 依旧返回 `False`——
Issue #3 的核心用例仍然失败。

**PR 4（回归门）** 把 `merge_adjacent` 修对了（目标用例通过），
却在同一提交里把 `slice_tail` 改成 `items[-(n + 1):]`，
让三个既有测试从绿变红。若只看 F2P 会误判为「可合并」，
只有跑了**既有测试回归门**才拦得住。

**PR 3（文档冒充）** 只在 README 里宣称「已支持 `build_tree`」并指向
一个并不存在的 `pipekit/tree.py`。它检验 Fissue 会不会被文本说服。

### 其余 Issue 的作用

| Issue | 作用 |
|---|---|
| #10 口径不一致 | 难度高、需设计决策 → 检验「不该自动修的要拦住」 |
| #11 normalize 应返回布尔 | **误报**：抛异常是该库的正确设计 → 真实性应判低 |
| #12 有时少算一个分片 | 与 #3 **同源但证据独立**（无最小复现）→ 检验判重**不误标** |
| #13 性能+推广 | **刷量**：无复现、模板化、夹带联系方式 → 真实性应判低 |
| #14 batch_bounds 乱序 | 独立边界缺陷 → 自动修复 |
| #15 说不清哪里错 | **无效**：无最小复现 → 真实性应判低 |

## 一、推送到 GitHub

GitHub 的写操作（建仓库 / 推分支 / 建 Issue-PR）由你自己执行：

```bash
# 1) 准备 token（勾 repo 权限）
#    https://github.com/settings/tokens  →  Generate new token (classic)  →  勾 repo
export GITHUB_TOKEN=ghp_xxxx

# 2) 先干跑看看计划（不写任何东西）
.venv/Scripts/python.exe demo/pipekit/scripts/setup_github.py --owner John-is-playing --dry-run

# 3) 真跑
.venv/Scripts/python.exe demo/pipekit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`（本机有 HTTPS 中间人，
> 全局 Python 缺 truststore 会 TLS 失败，必须用 .venv 里的解释器）。

脚本会依次完成：

1. 创建 `John-is-playing/pipekit` 仓库（已存在则复用）
2. 推送 `main` 分支
3. 从 `fixtures/*.md` 创建 15 个 Issue（带标签）
4. 推送 5 个修复分支并创建 5 个 PR（正文里带 `Closes #N`）

脚本是**可重入**的：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过，失败可放心重跑。

常用参数：

```bash
--name pipekit-demo    # 换个仓库名
--private              # 建私有仓库（注意：Fissue 需要 token 才能读私有库）
--no-prs               # 只建 Issue，先看看评测效果
```

### 手动推送（方式 B）

```bash
cd demo/pipekit/repo
git remote add origin https://github.com/John-is-playing/pipekit.git
git push -u origin main
git push origin fix/merge-touching-intervals fix/count-batches-endpoint \
                fix/feature-tree-docs fix/merge-adjacent-window fix/coverage-dedup
```

然后在网页上按 `fixtures/*.md` 头部的 `<!-- ... -->`（title / labels）逐个建 Issue，
再为五个分支各建一个 PR（base 选 `main`）。

## 二、用 Fissue 测它

把仓库登记进 `config.yaml` 的 `repos:`：

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: pipekit
    base_branch: main
    collect: [issue, pr]
    since_days: 0        # 0 = 不限制时间窗口，确保 15 个 Issue 全抓到
    test_hint: "python -m pytest -q"    # 明确告诉它怎么跑测试
```

然后跑完整链路：

```bash
# 1) 抓取（应拉到 15 个 Issue + 5 个 PR）
fissue fetch --repo John-is-playing/pipekit --limit 80

# 2) 评测（四维评分 + 分类 + 反刷子）
fissue eval --repo John-is-playing/pipekit --all

# 3) 看结果：#1/#3/#5/#8 应为 tier1，#2/#4/#6/#7/#14 应为 tier2
fissue report --repo John-is-playing/pipekit

# 4) 生成验证器并做 F2P 验证
fissue verify --repo John-is-playing/pipekit

# 5) 批量定论 + 打标签
fissue flush

# 6) 试跑自动修复（只出补丁，不提 PR）
fissue fix --repo John-is-playing/pipekit --dry-run --limit 9
```

## 三、期望结果对照表

| # | 条目 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|
| 1 | merge 端点相接 | bug | **tier1** | base 失败（可复现） | 自动修复 → 提 PR |
| 2 | merge_adjacent 相邻 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 3 | in_ranges 漏右端点 | bug | **tier1** | base 失败（跨模块定位） | 自动修复 → 提 PR |
| 4 | coverage 重复计数 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 5 | deep_merge 改入参 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 6 | resolve_index 负索引 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 7 | slice_tail n=0 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 8 | drop_ranges 用终点 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 9 | 树形分组 | **feature** | none | 不验证 | 只打标签 |
| 10 | 统计口径不一致 | bug | none | 难自动化（需决策） | 不修复，打标签 |
| 11 | normalize 返回布尔 | **低真实性** | none | — | 判为误报 |
| 12 | count_batches 少算 | bug | none | — | **相关但非重复**（不误标重复） |
| 13 | 性能+推广 | **低真实性** | none | — | 判为刷量 |
| 14 | batch_bounds 乱序 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 15 | 说不清哪里错 | **低真实性** | none | — | 判为无效 |
| PR1 | merge 相接修复 | bug | none | **F2P 成立 + 回归通过** | 建议合并 |
| PR2 | drop_ranges「修复」 | bug | none | **F2P 不成立**（根因未动） | 建议不合并 ⚠️ |
| PR3 | build_tree 文档 | feature | none | 无代码变更 | 建议不合并 |
| PR4 | merge_adjacent 修复 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR5 | coverage 去重修复 | bug | none | **F2P 成立 + 回归通过** | 建议合并 |

三行最关键：

- **PR2**：如果被判「可合并」，说明 Fissue 只看了报错点的用例，
  没意识到根因在 `filterops`——检查验证器是否覆盖了 Issue 正文里的
  `in_ranges` 断言。
- **PR4**：如果被判「可合并」，说明回归门没生效——检查
  `verifier.regression_gate` 是否为 `strict`（`warn` 只告警不拦）。
- **PR3**：如果被判「可合并」，说明评测被文档文本说服，
  应当确认「feature + 无代码变更」会走不合并分支。

## 四、本地自检（不连网）

推之前可以先在本地确认夹具是自洽的：

```bash
cd demo/pipekit/repo
.venv/Scripts/python.exe -m pytest -q          # 基线应 61 passed

# 确认 9 处缺陷确实存在
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
from pipekit import *
from pipekit.intervals import merge
from pipekit.slices import merge_adjacent
print(merge([(1, 3), (3, 5)]))            # 期望 [(1, 3), (3, 5)]（缺陷）
print(merge_adjacent([(1, 4), (4, 6)]))   # 期望 [(1, 4), (4, 6)]（缺陷）
print(in_ranges(5, [(1, 5)]))             # 期望 False（缺陷）
print(coverage([(0, 4), (2, 6)]))         # 期望 10（缺陷）
print(slice_tail([1, 2], 0))              # 期望 [1, 2]（缺陷）
print(drop_ranges([(1, 4)], [(4, 4)]))    # 期望 [(1, 4)]（缺陷）
base = {'a': {'x': 1}}; deep_merge(base, {'a': {'y': 2}})
print(base)                               # 期望被污染（缺陷）
"
```

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | token 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用（脚本会自动跳过） |
| `pipekit 工作区不干净` | repo 有未提交改动 | `cd demo/pipekit/repo && git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异，或 PR 已存在 | 脚本会跳过已存在的；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe`（含 truststore） |

> **为什么强调换行符**：Fissue 验证 PR 时用 `git apply` 打平台返回的 diff。
> 如果仓库里混入 CRLF，diff 的上下文行与实际文件字节不一致，补丁会应用失败。
> 所以 `demo/pipekit/repo/.gitattributes` 里固定了 `* text=auto eol=lf`。
