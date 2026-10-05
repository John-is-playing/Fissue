<!--
title: pipeline.coverage 对重叠分片重复计数
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`pipeline.coverage(slices)` 直接累加每片的点数，分片**重叠**时会把重叠部分
重复计入，得出的覆盖点数比真实值偏大。

`coverage` 的文档说「重叠的部分只算一次」。

## 复现步骤

```python
from pipekit import coverage

print(coverage([(0, 4), (2, 6)]))
print(coverage([(0, 4), (0, 4)]))
```

**实际输出**

```
10
10
```

**期望输出**

```
7          # 0..6，共 7 个点
5
```

## 影响范围

- 依赖覆盖点数做容量估算 / 采样统计的地方会偏高。
- 只在分片重叠时出错；`plan()` 产出的分片互不重叠，所以常见路径看不出来。

## 根因分析

`pipekit/pipeline.py` 里手动累加，没有先合并：

```python
covered = 0
for start, end in slices:
    covered += end - start + 1     # ← 重叠区间被重复计入
return covered
```

同模块已经导入了 `intervals`，直接复用即可。

## 期望修复

```python
coverage([(0, 4), (2, 6)])    # 7
coverage([(0, 4), (5, 9)])    # 10（不相交，保持不变）
coverage([(0, 9)])            # 10
coverage([])                  # 0
```

修复后仍需保证 `coverage(plan(10, 5)) == 10`。

## 环境

- pipekit 0.3.1
- Python 3.10+
