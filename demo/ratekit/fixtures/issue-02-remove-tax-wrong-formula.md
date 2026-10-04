<!--
title: remove_tax 公式错误，含税价反推净额严重偏低
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`remove_tax(gross, rate)` 用于从含税价反推净额，但当前的公式是错的：
它算的是「再打一次折」，而不是「除以 1+税率」。

## 复现步骤

```python
from ratekit import apply_tax, remove_tax

print(remove_tax(113.0, 0.13))
print(apply_tax(remove_tax(113.0, 0.13), 0.13))
```

**实际输出**

```
98.31
111.09
```

**期望输出**

```
100.0
113.0
```

## 影响范围

- 13% 税率下，100 元的净额被算成 98.31，偏低 1.7%。
- 更严重的是 **`remove_tax` 与 `apply_tax` 不再互逆**：一来一回金额就变了，
  凡是靠这两个函数做对账/冲红的链路都会产生无法收敛的尾差。

## 根因分析

`ratekit/tax.py`：

```python
def remove_tax(gross: float, rate: float) -> float:
    if rate < 0:
        raise ValueError("rate 不能为负数")
    return gross * (1 - rate)      # ← 应为 gross / (1 + rate)
```

`apply_tax(net) = net * (1 + rate)`，所以它的逆运算必须是 `gross / (1 + rate)`。

## 期望修复

```python
remove_tax(113.0, 0.13)   # 100.0
remove_tax(100.0, 0.0)    # 100.0   （零税率不变）
apply_tax(remove_tax(113.0, 0.13), 0.13)  # 113.0（互逆）
```

## 环境

- ratekit 0.4.0
- Python 3.10+
