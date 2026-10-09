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

**注意：这一条要修好，需要链路上两处都改对；只改 `drop_ranges` 仍然过不了验证。**

**缺陷 A（本 Issue 的根因）**：`pipekit/pipeline.py`

```python
return [s for s in slices if not filterops.in_ranges(s[1], ranges)]
#                                                     ↑ 取了终点；文档与语义都要求用起点 s[0]
```

**缺陷 B（被它调用的下层，独立 Issue 见 #3）**：`pipekit/filterops.py`

```python
return any(start <= point < end for start, end in ranges)
#                    ↑ 用了半开判定；闭区间应为 point <= end
```

看起来「把 `s[1]` 改成 `s[0]`」就够了，其实不然——改完它问的就是**起点**，
而起点恰好等于单点区间的值时，`in_ranges` 仍会因为半开判定返回 `False`：

```python
drop_ranges([(5, 9)], [(5, 5)])
# 改完 A 之后：问 in_ranges(5, [(5, 5)])
#             5 <= 5 < 5  →  False  →  分片没被剔除，仍是 [(5, 9)]
```

所以必须**同时**把 `in_ranges` 的闭区间判定修对。

> 两处必须**分别**修：这个用例考的是「分片起点」判定（问哪一点），
> `in_ranges` 考的是「闭区间端点算不算在内」。**问对了点，还要那个点能被正确判为『在内』。**
> 修的时候别把两者混在一起糊过去——它们在同一条链上，但改动互不覆盖。

## 期望修复

```python
drop_ranges([(0, 4)], [(4, 4)])                 # [(0, 4)]
drop_ranges([(5, 9)], [(5, 5)])                 # []   ← 需要 A 与 B 都修好
drop_ranges([(0, 4), (5, 9)], [(6, 8)])         # [(0, 4), (5, 9)]
drop_ranges([(0, 4), (5, 9)], [(5, 9)])         # [(0, 4)]
```

第 2 行**只有在 A 与 B 都修好后才会成立**——它走的是
`drop_ranges` → `in_ranges` 这条跨模块链路。

## 环境

- pipekit 0.3.1
- Python 3.10+
