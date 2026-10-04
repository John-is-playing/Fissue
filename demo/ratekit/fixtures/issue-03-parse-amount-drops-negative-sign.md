<!--
title: parse_amount 丢失负号，负数金额被解析成正数
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`parse_amount("-5.00")` 返回 `5.0`，负号被吃掉了。

## 复现步骤

```python
from ratekit import parse_amount

print(parse_amount("-5.00"))
print(parse_amount("$-12.30"))
```

**实际输出**

```
5.0
12.3
```

**期望输出**

```
-5.0
-12.3
```

## 影响范围

- 退款、冲正、费用抵扣这类**负向金额**会被解析成正数，
  进而把「退你 5 元」记成「收你 5 元」。
- 影响面限于带负号的输入；日常正数金额不受影响，因此紧急度不高。

## 根因分析

`ratekit/money.py` 在清洗时把 `-` 也一并替换掉了：

```python
cleaned = cleaned.replace(",", "").replace("-", "")   # ← 连负号一起删了
return float(cleaned)
```

## 期望修复

保留开头的负号，其余清洗逻辑不变：

```python
parse_amount("-5.00")    # -5.0
parse_amount("$-12.30")  # -12.3
parse_amount("1,234.50") # 1234.5（保持不变）
```

## 环境

- ratekit 0.4.0
- Python 3.10+
