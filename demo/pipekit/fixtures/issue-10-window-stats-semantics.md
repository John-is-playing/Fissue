<!--
title: 覆盖统计与分片计数对重叠分片的处理口径不一致
labels: bug, discussion
target: 难度高（需设计决策）→ 不应自动修复
-->

## 问题描述

同一批分片，`coverage()` 与 `count_batches()` 对「重叠」的态度不一致：

- `coverage()` 会把重叠部分合并后去重
- `count_batches()` 按分片个数计数，重叠分片算两片

于是一批重叠的分片会同时得到「覆盖 7 个点」和「共 2 批」，两边的口径对不上。
下游若同时用这两个数做校验（例如「批数 × 批大小 ≈ 覆盖点数」），会永远对不平。

```python
from pipekit import coverage, count_batches

slices = [(0, 4), (2, 6)]
print(coverage(slices), count_batches(slices, []))   # 7 2
```

## 需要决策的点

这不是一个「把哪个函数改对」的问题，而是**口径要先定义清楚**：

1. 流水线是否允许产生重叠分片？如果不允许，应当在 `plan()` 阶段就拒绝/归一。
2. 如果允许，`count_batches` 该按「合并后的批数」还是「输入分片数」计数？
3. `batch_bounds` 的起止范围该不该受重叠影响？

## 为什么不该自动修复

- 没有唯一的正确答案，取决于产品对「批」的定义。
- 无论往哪个方向改，都会动到 `stats` 的公开语义，需要人类拍板。
- 三个函数的口径必须**一起**定，单独改一个只会制造新的不一致。

## 建议

先明确「批」的语义并写进 README，再据此统一 `coverage` / `count_batches` / `batch_bounds`
三者。在语义定下来之前，保持现状并标记为「需人工」。

## 环境

- pipekit 0.3.1
- Python 3.10+
