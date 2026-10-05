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

`pipekit/filterops.py`：

```python
return any(start <= point < end for start, end in ranges)
#                    ↑ 用了半开判定；闭区间应为 point <= end
```

库里其它地方（`intervals.overlaps`、文档）都是闭区间口径，这里落了单。

## 期望修复

```python
in_ranges(5, [(1, 5)])                # True
in_ranges(4, [(2, 4)])                # True
in_ranges(0, [(1, 5)])                # False（界外仍为 False）
in_ranges(6, [(1, 5)])                # False
count_batches([(0, 4), (5, 9)], [(5, 5)])   # 1
drop_ranges([(5, 9)], [(5, 9)])             # []
```

## 环境

- pipekit 0.3.1
- Python 3.10+
