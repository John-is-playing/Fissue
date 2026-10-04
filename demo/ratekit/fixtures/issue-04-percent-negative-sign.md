<!--
title: percent_of 对负百分比取绝对值，折扣方向算反
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`percent_of(value, percent)` 在 `percent` 为负时返回了正值，把符号抹掉了。

## 复现步骤

```python
from ratekit import percent_of

print(percent_of(200, -5))
print(percent_of(200, 5))
```

**实际输出**

```
10.0
10.0
```

**期望输出**

```
-10.0
10.0
```

## 影响范围

- 负百分比常用于表示降价、返还、反向调整。
  符号被抹掉后，`+5%` 和 `-5%` 算出来一模一样，方向性错误。
- 只在传负数时触发，正数百分比完全正常，因此影响面不大。

## 根因分析

`ratekit/pct.py` 对负数分支取了绝对值：

```python
if percent < 0:
    return value * abs(percent) / 100   # ← abs 抹掉了符号
return value * percent / 100
```

## 期望修复

两条分支其实可以合并，符号应当保留：

```python
percent_of(200, -5)   # -10.0
percent_of(200, 5)    # 10.0
percent_of(100, 0)    # 0.0
```

## 环境

- ratekit 0.4.0
- Python 3.10+
