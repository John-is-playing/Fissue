<!--
title: pipeline.drop_ranges 用分片终点判断，误删不该删的分片
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`drop_ranges` 的文档写的是「剔除**起点**落在过滤区间内的分片」，
但实现里比较的是分片的**终点**。

这导致两类错误：本该剔除的分片（起点在区间内）留下了，
而起点不在区间内、只是终点恰好落进去的分片被误删。

## 复现步骤

```python
from pipekit import drop_ranges

print(drop_ranges([(0, 4)], [(4, 4)]))          # 起点 0 不在 (4,4) 内 → 应保留
print(drop_ranges([(5, 9)], [(5, 5)]))          # 起点 5 在 (5,5) 内 → 应剔除
```

**实际输出**

```
[]              ← 误删了
[(5, 9)]        ← 该删的没删
```

**期望输出**

```
[(0, 4)]
[]
```

## 影响范围

- 「把某段范围对应的分片摘掉」是流水线最常见的操作之一：
  只要过滤范围的端点与分片边界重合（非常常见），删除结果就是错的。
- 下游 `stats.count_batches` / `stats.batch_bounds` 会跟着一起错。

## 根因分析

`pipekit/pipeline.py`：

```python
return [s for s in slices if not filterops.in_ranges(s[1], ranges)]
#                                                     ↑ 取了终点；文档与语义都要求用起点 s[0]
```

## 期望修复

```python
drop_ranges([(0, 4)], [(4, 4)])                 # [(0, 4)]
drop_ranges([(5, 9)], [(5, 5)])                 # []
drop_ranges([(0, 4), (5, 9)], [(6, 8)])         # [(0, 4), (5, 9)]
drop_ranges([(0, 4), (5, 9)], [(5, 9)])         # [(0, 4)]
```

注意：本用例是「分片起点」判定，与 `in_ranges` 自身的闭区间判定（见 #3）是两回事，
修的时候别把两者混在一起。

## 环境

- pipekit 0.3.1
- Python 3.10+
