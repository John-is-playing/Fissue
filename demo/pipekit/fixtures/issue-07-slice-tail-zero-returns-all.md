<!--
title: slice_tail 的 n=0 返回整个列表
labels: bug, good first issue
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`slices.slice_tail(items, n)` 在 `n == 0` 时返回了**整个列表**，而契约是返回空列表。

## 复现步骤

```python
from pipekit import slice_tail

print(slice_tail([1, 2], 0))
```

**实际输出**

```
[1, 2]        ← 应该是空的
```

**期望输出**

```
[]
```

## 影响范围

- 分页时「取末尾 0 个」意味着这一页为空，返回整表会让下游误以为还有大量数据。
- 只在 `n == 0` 时出错，属于典型边界遗漏。

## 根因分析

`pipekit/slices.py` 用了切片语法：

```python
return items[-n:]      # ← n == 0 时是 items[0:]，即整个列表
```

Python 里 `items[-0:]` 等价于 `items[0:]`。需要单独处理 `n == 0`。

## 期望修复

```python
slice_tail([1, 2], 0)     # []
slice_tail([1, 2, 3, 4], 2)   # [3, 4]
slice_tail([1, 2, 3], 5)      # [1, 2, 3]
slice_tail([1, 2], -1)        # 仍应抛 ValueError
```

## 环境

- pipekit 0.3.1
- Python 3.10+
