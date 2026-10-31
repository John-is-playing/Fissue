# 夹具：pipekit

第三套独立夹具。前两套的缺陷都落在单个函数里，`pipekit` 刻意把同一个语义约定（**区间怎么算端点**）
散落到 `intervals` / `slices` / `filterops` 三处，再用 `pipeline` / `stats` 串成调用链：

> **当 bug 藏在跨模块调用链上，Fissue 能不能顺着报错点找到真正的根因文件、写出能真正复现的验证器，
> 并且不把别处改坏？**

15 个 Issue + 5 个 PR，混入错误、无效、同源但不重复、刷量的请求。

## 目录结构

```
demo/pipekit/
├── repo/                    待推送仓库（独立 git 仓库）
│   ├── pipekit/             库源码（9 处植入缺陷，跨 6 个模块）
│   ├── tests/               61 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                15 个 Issue（issue-01 … issue-15）
└── scripts/setup_github.py  一键建仓库 / Issue / PR
```

## 设计意图

### 植入的 9 处真实缺陷（基线测试刻意不覆盖）

| # | 位置 | 表现 | 期望行为 |
|---|---|---|---|
| 1 | `intervals.merge` | `[(1,3),(3,5)]` 不合并（闭区间端点相接漏了） | tier1 → 自动修复 |
| 2 | `slices.merge_adjacent` | `[(1,4),(4,6)]` 不合并（半开区间相邻漏了） | tier2 → 自动修复 |
| 3 | `filterops.in_ranges` | `in_ranges(5,[(1,5)])` 为 `False`（漏右端点） | tier1 → 自动修复（**跨模块**） |
| 4 | `pipeline.coverage` | `[(0,4),(2,6)]` 算成 10（重叠重复计数） | tier2 → 自动修复 |
| 5 | `config.deep_merge` | 返回结果时把入参 `base` 改掉了 | tier1 → 自动修复 |
| 6 | `resolver.resolve_index` | 负索引被拒（`-1` 本应取最后一个） | tier2 → 自动修复 |
| 7 | `slices.slice_tail` | `n=0` 返回整个列表 | tier2 → 自动修复 |
| 8 | `pipeline.drop_ranges` | 用**终点**判定，误删/漏删分片 | tier1 → 自动修复 |
| 9 | `stats.batch_bounds` | 假设输入已排序，乱序时返回非法区间 | tier2 → 自动修复 |

**61 个基线测试全绿**——每处缺陷都藏在未覆盖的边界上。缺陷 #1/#2 是同一语义在闭区间/半开区间
两套坐标下的**对称错误**，不能靠「复制另一处写法」来修；#3 与 #8 故意挨着（一个在 `filterops`
判点、一个在 `pipeline` 判分片起点），必须分开修。

### 五个 PR：真好 / 半吊子 / 文档冒充 / 回归门 / 有效

| PR | 分支 | 关联 | 性质 | 期望行为 |
|---|---|---|---|---|
| 1 | `fix/merge-touching-intervals` | #1 | ✅ 完整修复 + 回归测试 | F2P + 回归门通过 → 建议合并 |
| 2 | `fix/count-batches-endpoint` | #3 | ⚠️ **只改调用点，绕开根因** | F2P 不成立 → 不合并 |
| 3 | `fix/feature-tree-docs` | #9 | ❌ **只改文档，冒充功能实现** | FEATURE + 无代码 → 不合并 |
| 4 | `fix/merge-adjacent-window` | #2 | ⚠️ **修好目标却弄坏别处** | F2P 成立但**回归门拦住** → 不合并 |
| 5 | `fix/coverage-dedup` | #4 | ✅ 完整修复 + 回归测试 | F2P + 回归门通过 → 建议合并 |

- **PR2（半吊子）**：把 `drop_ranges` 换成内联闭区间判断，看起来修好了报错点，但根因
  `filterops.in_ranges(5,[(1,5)])` 仍返回 `False`，#3 的核心用例依旧失败。
- **PR4（回归门）**：`merge_adjacent` 修对了，却在同一提交里把 `slice_tail` 改成 `items[-(n+1):]`，
  三个既有测试由绿变红——只看 F2P 会误判「可合并」。
- **PR3（文档冒充）**：只在 README 宣称「已支持 `build_tree`」并指向不存在的 `pipekit/tree.py`，
  检验 Fissue 会不会被文本说服。

### 其余 Issue

| Issue | 作用 |
|---|---|
| #10 口径不一致 | 难度高、需设计决策 → 检验「不该自动修的要拦住」 |
| #11 normalize 应返回布尔 | **误报**：抛异常是该库的正确设计 → 真实性应判低 |
| #12 有时少算一个分片 | 与 #3 **同源但证据独立**（无最小复现）→ 检验判重**不误标** |
| #13 性能 + 推广 | **刷量**：无复现、模板化、夹带联系方式 → 真实性应判低 |
| #14 batch_bounds 乱序 | 独立边界缺陷 → 自动修复 |
| #15 说不清哪里错 | **无效**：无最小复现 → 真实性应判低 |

## 一、推送到 GitHub（写操作请自行执行）

```bash
export GITHUB_TOKEN=ghp_xxxx
.venv/Scripts/python.exe demo/pipekit/scripts/setup_github.py --owner John-is-playing --dry-run
.venv/Scripts/python.exe demo/pipekit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`（全局 Python 缺 truststore，会 TLS 失败）。

脚本依次：建仓库（已存在则复用）→ 推 `main` → 建 15 个 Issue → 推 5 个修复分支并建 5 个 PR。
**可重入**：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过。参数：`--name`、`--private`、`--no-prs`。

手动方式：`cd demo/pipekit/repo && git push -u origin main`，再推
`fix/merge-touching-intervals fix/count-batches-endpoint fix/feature-tree-docs
fix/merge-adjacent-window fix/coverage-dedup`，然后按 fixtures 头部注释建 Issue、按分支建 PR。

## 二、用 Fissue 测它

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: pipekit
    base_branch: main
    collect: [issue, pr]
    since_days: 0                       # 确保 15 个 Issue 全抓到
    test_hint: "python -m pytest -q"
```

```bash
fissue fetch  --repo John-is-playing/pipekit --limit 80     # 应拉到 15 Issue + 5 PR
fissue eval   --repo John-is-playing/pipekit --all
fissue report --repo John-is-playing/pipekit
fissue verify --repo John-is-playing/pipekit
fissue flush
fissue fix    --repo John-is-playing/pipekit --dry-run --limit 9
```

## 三、期望结果对照表

| # | 条目 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|
| 1 | merge 端点相接 | bug | **tier1** | base 失败 | 自动修复 |
| 2 | merge_adjacent 相邻 | bug | **tier2** | base 失败 | 自动修复 |
| 3 | in_ranges 漏右端点 | bug | **tier1** | base 失败（跨模块定位） | 自动修复 |
| 4 | coverage 重复计数 | bug | **tier2** | base 失败 | 自动修复 |
| 5 | deep_merge 改入参 | bug | **tier1** | base 失败 | 自动修复 |
| 6 | resolve_index 负索引 | bug | **tier2** | base 失败 | 自动修复 |
| 7 | slice_tail n=0 | bug | **tier2** | base 失败 | 自动修复 |
| 8 | drop_ranges 用终点 | bug | **tier1** | base 失败 | 自动修复 |
| 9 | 树形分组 | **feature** | none | 不验证 | 只打标签 |
| 10 | 统计口径不一致 | bug | none | 需决策 | 不修复 |
| 11 | normalize 返回布尔 | **低真实性** | none | — | 判为误报 |
| 12 | count_batches 少算 | bug | none | — | **相关但非重复** |
| 13 | 性能 + 推广 | **低真实性** | none | — | 判为刷量 |
| 14 | batch_bounds 乱序 | bug | **tier2** | base 失败 | 自动修复 |
| 15 | 说不清哪里错 | **低真实性** | none | — | 判为无效 |
| PR1 | merge 相接修复 | bug | none | **F2P + 回归通过** | 建议合并 |
| PR2 | drop_ranges「修复」 | bug | none | **F2P 不成立**（根因未动） | 建议不合并 ⚠️ |
| PR3 | build_tree 文档 | feature | none | 无代码变更 | 建议不合并 |
| PR4 | merge_adjacent 修复 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR5 | coverage 去重修复 | bug | none | **F2P + 回归通过** | 建议合并 |

三行最关键：**PR2** 若被判可合并 → 验证器没覆盖根因 `in_ranges` 断言；**PR4** 若被判可合并 →
回归门没生效（检查 `verifier.regression_gate` 是否 `strict`）；**PR3** 若被判可合并 → 评测被
文档文本说服。

## 四、本地自检（不连网）

```bash
cd demo/pipekit/repo && .venv/Scripts/python.exe -m pytest -q      # 基线应 61 passed
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
from pipekit import *
from pipekit.intervals import merge
from pipekit.slices import merge_adjacent
print(merge([(1,3),(3,5)]))            # [(1,3),(3,5)]（缺陷）
print(merge_adjacent([(1,4),(4,6)]))   # [(1,4),(4,6)]（缺陷）
print(in_ranges(5, [(1,5)]))           # False（缺陷）
print(coverage([(0,4),(2,6)]))         # 10（缺陷）
print(slice_tail([1,2], 0))            # [1,2]（缺陷）
print(drop_ranges([(1,4)], [(4,4)]))   # [(1,4)]（缺陷）
base = {'a': {'x': 1}}; deep_merge(base, {'a': {'y': 2}})
print(base)                            # 被污染（缺陷）
"
```

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用 |
| `pipekit 工作区不干净` | repo 有未提交改动 | `git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异 / PR 已存在 | 脚本会跳过；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe` |

> 换行符：验证 PR 时用 `git apply` 打平台 diff，CRLF 会导致补丁应用失败。`repo/.gitattributes`
> 固定了 `* text=auto eol=lf`。

---

相关：[夹具总览](../README.md) · [BENCHMARK.md](../../BENCHMARK.md) · [验证器与 F2P](../../docs/wiki/Verifier-and-F2P.md)