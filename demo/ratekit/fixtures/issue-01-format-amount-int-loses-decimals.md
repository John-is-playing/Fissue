<!--
title: format_amount 传整数时不保留小数位，账单金额少显示两位
labels: bug, good first issue
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`format_amount(1234)` 返回 `'1,234'`，而不是 `'1,234.00'`。

只要调用方传进来的 `value` 是 **Python int**（而不是 float），小数部分就整个丢了。
金额显示少两位，在发票、账单、对账页面上都会直接出错。

## 复现步骤

```python
from ratekit import format_amount

print(format_amount(1234))     # 整数
print(format_amount(1234.0))   # 浮点
```

**实际输出**

```
1,234
1,234.00
```

**期望输出**

```
1,234.00
1,234.00
```

## 影响范围

- 从数据库/接口读回来的金额经常是 `int`（分为单位的整数、JSON 里的整数金额），
  所以这条路径命中率很高。
- 对账时 `1,234` 与 `1,234.00` 肉眼看着一样，但下游按字符串解析会直接算错。

## 根因分析

`ratekit/money.py` 里对整数做了特判：

```python
if isinstance(value, int):
    return f"{value:,}"          # ← 没有用 digits
return f"{value:,.{digits}f}"
```

`format_amount` 的契约是「固定小数位」，不应该因为入参是 `int` 就改行为。

## 期望修复

```python
format_amount(1234)            # '1,234.00'
format_amount(0)               # '0.00'
format_amount(-1234)           # '-1,234.00'   负整数也必须补足小数位
format_amount(1234, digits=0)  # '1,234'       显式要求 0 位时不加小数点
```

其余情况（千分位、`digits=1`、浮点入参）保持不变。

## 环境

- ratekit 0.4.0
- Python 3.10+
