<!--
title: normalize 应该返回 True/False 而不是抛异常
labels: bug
target: 误报/无效 → 真实性应判低
-->

## 问题描述

`intervals.normalize(start, end)` 在 `start > end` 时**抛异常**，
这让调用方必须写 try/except，非常麻烦。

建议改成返回布尔值：合法返回 `True`，非法返回 `False`。

```python
from pipekit import normalize

print(normalize(3, 1))   # 现在直接抛 ValueError，希望返回 False
```

## 我的理由

- 用返回值判断更简洁，不用异常控制流。
- 我看别的一些库就是这么设计的。

## 期望行为

```python
normalize(1, 5)   # True
normalize(3, 1)   # False
```

## 环境

- pipekit 0.3.1
