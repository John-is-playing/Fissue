<!--
title: is_weekend 漏判星期六，从周六起算工作日会少算一天
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`is_weekend(date(2024, 1, 6))`（星期六）返回 `False`，只把星期日当成周末。

## 复现步骤

```python
from datetime import date
from ratekit import is_weekend

print(is_weekend(date(2024, 1, 6)))  # 星期六
print(is_weekend(date(2024, 1, 7)))  # 星期日
```

**实际输出**

```
False
True
```

**期望输出**

```
True
True
```

## 影响范围

- 周末被漏判一天，凡是「跳过周末」的排期/计费会把周六当成工作日。
- 影响面限于周末与跨周场景；工作日内部的计算不受影响。

## 根因分析

`ratekit/duration.py` 只比对了星期日的索引：

```python
return day.weekday() == 6      # ← 漏了星期六（weekday()==5）
```

## 期望修复

```python
is_weekend(date(2024, 1, 6))  # True（星期六）
is_weekend(date(2024, 1, 7))  # True（星期日）
is_weekend(date(2024, 1, 5))  # False（星期五）
```

> 注：`add_working_days` 目前用的是自己的 `weekday() < 5` 判断，
> 不依赖 `is_weekend`，所以修这里不会影响工作日推进的结果。

## 环境

- ratekit 0.4.0
- Python 3.10+
