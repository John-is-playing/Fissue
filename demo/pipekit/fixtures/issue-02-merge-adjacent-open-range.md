<!--
title: slices.merge_adjacent 不合并相邻（不重叠）的切片
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`slices.merge_adjacent()` 只会合并**真正重叠**的切片，漏掉了**相邻**的切片。

`:mod:`pipekit.slices`` 处理的是**半开区间** `[start, end)`。
半开语义下 `[1, 4)` 与 `[4, 6)` 端点相接，覆盖 `1..5` 是连续的，应当合并为 `[1, 6)`。

## 复现步骤

```python
from pipekit.slices import merge_adjacent

print(merge_adjacent([(1, 4), (4, 6)]))   # 相邻
print(merge_adjacent([(1, 4), (2, 6)]))   # 重叠
```

**实际输出**

```
[(1, 4), (4, 6)]
[(1, 6)]
```

**期望输出**

```
[(1, 6)]
[(1, 6)]
```

## 影响范围

- 分页场景里，一个逻辑页常常被拆成两个连续的切片；漏合并会让「这个页覆盖了哪些元素」
  算错。
- 影响面限于「恰好相邻」的输入，日常不重叠的切片不受影响，所以紧急度不高。

## 根因分析

`pipekit/slices.py`：

```python
if start < last_end:      # ← 只判重叠；相邻时 start == last_end，被判为分离
    result[-1] = (last_start, max(last_end, end))
else:
    result.append((start, end))
```

半开区间相邻的判定应是 `start <= last_end`（重叠时也成立）。

## 期望修复

```python
merge_adjacent([(1, 4), (4, 6)])   # [(1, 6)]
merge_adjacent([(1, 4), (2, 6)])   # [(1, 6)]
merge_adjacent([])                 # []
```

## 环境

- pipekit 0.3.1
- Python 3.10+
