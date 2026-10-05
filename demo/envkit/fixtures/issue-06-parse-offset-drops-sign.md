<!--
title: parse_offset 丢失负号，"-05:30" 被解析成正的 330
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`parse_offset` 对带负号的偏移会丢掉符号，返回正数。

`"+08:00"` 能正确返回 `480`，但 `"-05:30"` 应该返回 `-330`，实际返回 `330`。

## 复现步骤

```python
from envkit import parse_offset

print(parse_offset("+08:00"))
print(parse_offset("-05:30"))
print(parse_offset("-00:45"))
```

**实际输出**

```
480
330
45
```

**期望输出**

```
480
-330
-45
```

## 影响范围

- 所有西半球的时区（美洲全境）都会被算反，`-05:30` 变成 `+05:30`，
  时间换算直接差 11 小时。
- `split_iso` 内部调用了本函数，所以 `split_iso(...)["offset"]` 一并出错。

## 根因分析

`envkit/timeutil.py`：

```python
body = raw.lstrip("+-")          # ← 把符号整个丢了

...
return hours * 60 + minutes      # ← 无条件返回正数
```

实现里既没有记录符号，返回时也没有按符号取反。
（文档写的 `"-05:30"` 示例期望值是 `-330`，实现与文档不符。）

## 期望修复

```python
assert parse_offset("+08:00") == 480
assert parse_offset("-05:30") == -330
assert parse_offset("-00:45") == -45
assert parse_offset("Z") == 0
assert parse_offset("03:00") == 180      # 无符号按正数处理（保持不变）
```

## 环境

- envkit 0.6.0
- Python 3.10+
