<!--
title: batch_bounds 在分片未排序时返回错误的起止范围
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`stats.batch_bounds(slices, ranges)` 的契约是返回剩余分片覆盖的
`(最小起点, 最大终点)`，但它实际取的是「第一片的起点」和「最后一片的终点」。

当调用方传入的分片**没有按起点排序**时，返回的区间端点就是错的
（甚至会出现 `start > end` 这种非法区间）。

## 复现步骤

```python
from pipekit import batch_bounds

print(batch_bounds([(5, 9), (0, 4)], []))
print(batch_bounds([(5, 9), (0, 4)], [(0, 0)]))
```

**实际输出**

```
(5, 4)          ← 非法区间：起点比终点还大
(5, 4)
```

**期望输出**

```
(0, 9)
(5, 9)
```

## 影响范围

- `drop_ranges` 会保持输入顺序，所以只要上游的分片顺序不是升序，
  这里就会出错。
- 返回 `start > end` 的区间会让下游的范围校验、切片运算直接失败。
- 只在输入乱序时触发；`plan()` 产出的分片是有序的，常规路径看不出来。

## 根因分析

`pipekit/stats.py`：

```python
remaining = pipeline.drop_ranges(slices, ranges)
if not remaining:
    return None
return (remaining[0][0], remaining[-1][1])   # ← 假设了输入已排序
```

契约要求的是「最小 / 最大」，应当显式取 `min` / `max`，而不是依赖顺序。

## 期望修复

```python
batch_bounds([(5, 9), (0, 4)], [])            # (0, 9)
batch_bounds([(5, 9), (0, 4)], [(0, 0)])      # (5, 9)
batch_bounds([(0, 4), (5, 9)], [(6, 8)])      # (0, 9)（有序输入保持不变）
batch_bounds([], [(1, 2)])                     # None
```

## 环境

- pipekit 0.3.1
- Python 3.10+
