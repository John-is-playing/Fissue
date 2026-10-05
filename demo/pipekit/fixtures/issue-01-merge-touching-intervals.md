<!--
title: intervals.merge 不合并端点相接的区间
labels: bug, good first issue
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`intervals.merge()` 在合并区间时漏掉了**端点相接**的情况。

本库统一约定区间是**闭区间** `[start, end]`，所以 `[1, 3]` 与 `[3, 5]`
在 `3` 这个点上重叠，理应合并成 `[1, 5]`。但当前实现把它们当成两个不相干的区间。

## 复现步骤

```python
from pipekit.intervals import merge

print(merge([(1, 3), (3, 5)]))
print(merge([(1, 3), (2, 5)]))   # 这条是对的，能合并
```

**实际输出**

```
[(1, 3), (3, 5)]
[(1, 5)]
```

**期望输出**

```
[(1, 5)]
[(1, 5)]
```

## 影响范围

- 分片流水线里相邻的分片非常常见（例如按固定窗口切分后又各自微调过边界），
  漏合并会让下游把「同一段连续数据」当成两段处理。
- `pipekit.pipeline.coverage()` 依赖 `merge()`，虽然本例的点数合计恰好相同，
  但只要出现重复覆盖就会一起算错。

## 根因分析

`pipekit/intervals.py` 的合并循环用了严格小于：

```python
if start < last_end:      # ← 端点相接（start == last_end）时判定为不相交，漏合并
    result[-1] = (last_start, max(last_end, end))
else:
    result.append((start, end))
```

`overlaps()` 的判定就是 `a[0] <= b[1] and b[0] <= a[1]`，合并时应当与它同口径。

## 期望修复

```python
merge([(1, 3), (3, 5)])   # [(1, 5)]
merge([(4, 5), (1, 2)])   # [(1, 2), (4, 5)]（真正不相交的仍要保持分离）
merge([])                 # []
```

## 环境

- pipekit 0.3.1
- Python 3.10+
