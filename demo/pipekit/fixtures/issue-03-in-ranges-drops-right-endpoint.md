<!--
title: in_ranges 漏掉区间右端点，进而算错剩余分片数
labels: bug, priority
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`filterops.in_ranges(point, ranges)` 对**右端点**返回 `False`，
即点恰好等于某个区间的 `end` 时不算「在区间内」。

由于本库统一使用**闭区间**，`in_ranges(5, [(1, 5)])` 应当是 `True`。

这个错误还会沿调用链扩散：`pipeline.drop_ranges`、`stats.count_batches`、
`stats.batch_bounds` 全部依赖 `in_ranges`。

## 复现步骤

```python
from pipekit import in_ranges, count_batches
from pipekit import drop_ranges

print(in_ranges(5, [(1, 5)]))                  # 端点
print(count_batches([(0, 4), (5, 9)], [(5, 5)]))
print(drop_ranges([(5, 9)], [(5, 9)]))
```

**实际输出**

```
False          ← 应为 True
2              ← 应为 1
[(5, 9)]       ← 应为 []
```

**期望输出**

```
True
1
[]
```

## 影响范围

- 这是**跨模块**缺陷：定位它需要顺着 `stats` → `pipeline` → `filterops` 找到根因，
  而不是在报错的函数里改。
- 只要过滤区间的边界与分片起点/终点重合，就会漏剔除一整片，导致批量数偏大、
  范围端点偏大。

## 根因分析

**注意：这一条报错牵出的是链路上两处独立缺陷，只改其中一处无法通过验证。**

**缺陷 A（本 Issue 的根因）**：`pipekit/filterops.py`

```python
return any(start <= point < end for start, end in ranges)
#                    ↑ 用了半开判定；闭区间应为 point <= end
```

库里其它地方（`intervals.overlaps`、文档）都是闭区间口径，这里落了单。

**缺陷 B（链路上的另一处，独立 Issue 见 #8）**：`pipekit/pipeline.py`

```python
return [s for s in slices if not filterops.in_ranges(s[1], ranges)]
#                                                     ↑ 取了分片**终点**；
#                                                       文档与语义都要求用起点 s[0]
```

`drop_ranges` 的文档写的是「剔除**起点**落在过滤区间内的分片」，实现却拿分片的
**终点**去问 `in_ranges`。因此即便 `in_ranges` 修好了，
`drop_ranges([(5, 9)], [(5, 5)])` 仍是拿 `9` 去问、问不中，分片不会被剔除。

> 两处必须**分别**修：只用 `in_ranges` 的改动糊不住 `drop_ranges` 的取点问题，
> 反之亦然。修完 `drop_ranges` 后，它问的那一点必须仍是**分片起点**。

## 期望修复

```python
in_ranges(5, [(1, 5)])                # True      ← 缺陷 A
in_ranges(4, [(2, 4)])                # True      ← 缺陷 A
in_ranges(0, [(1, 5)])                # False（界外仍为 False）
in_ranges(6, [(1, 5)])                # False
count_batches([(0, 4), (5, 9)], [(5, 5)])   # 1   ← 需要 A 与 B 都修好
drop_ranges([(5, 9)], [(5, 9)])             # []  ← 需要 A 与 B 都修好
```

`count_batches` / `drop_ranges` 两行**只有在 A 与 B 都修好后才会成立**——
它们走的是 `stats` → `pipeline` → `filterops` 这条完整链路。

## 环境

- pipekit 0.3.1
- Python 3.10+
