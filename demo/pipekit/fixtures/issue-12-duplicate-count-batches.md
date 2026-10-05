<!--
title: count_batches 有时候会少算一个分片
labels: bug
target: 与 #3 同源但证据独立 → 不应判为重复（判重从严的负样本）
-->

## 问题描述

我在用 `stats.count_batches` 统计剩余批数的时候，偶尔发现结果比预期少一个。

大概是过滤区间的边界没算对，导致过滤的时候多删了一片。

```python
from pipekit import count_batches

# 大概是这样用的，具体数字记不清了
print(count_batches(slices, ranges))
```

## 补充

过滤用的 ranges 边界正好跟分片起点重合的时候最容易出现，别的场景没复现出来。

## 环境

- pipekit 0.3.1
