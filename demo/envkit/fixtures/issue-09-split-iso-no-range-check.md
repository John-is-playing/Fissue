<!--
title: split_iso 不校验时间字段范围，'08:75:00' 这种非法时间被原样接受
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`split_iso` 的文档声明「时间字段越界（时 0..23、分/秒 0..59）应抛 `ValueError`」，
但实现只校验了格式能拆成三段，没有校验各段数值的范围。

## 复现步骤

```python
from envkit import split_iso

print(split_iso("2024-03-05T08:75:00+08:00"))
print(split_iso("2024-03-05T25:00:00+08:00"))
```

**实际输出**

```python
{'year': 2024, 'month': 3, 'day': 5, 'hour': 8, 'minute': 75, 'second': 0, 'offset': 480}
{'year': 2024, 'month': 3, 'day': 5, 'hour': 25, 'minute': 0, 'second': 0, 'offset': 480}
```

**期望输出**

```
ValueError: 非法时间：'2024-03-05T08:75:00+08:00'
ValueError: 非法时间：'2024-03-05T25:00:00+08:00'
```

## 影响范围

- 非法时间戳会被解析成「看起来正常」的字典，
  下游把 `minute=75` 直接拿去参与时间运算，结果毫无意义。
- 与 `parse_offset` 的严格校验（分钟 ≥60 会报错）相比，
  同一模块内的口径不一致。

## 根因分析

`envkit/timeutil.py`：

```python
hour_of_day, minute, second = (int(x) for x in time_fields)
offset = parse_offset(offset_part) if offset_part else 0
```

拆分后没有对 `hour_of_day` / `minute` / `second` 做范围检查。

## 期望修复

```python
split_iso("2024-03-05T08:30:00+08:00")        # 正常（保持不变）
split_iso("2024-03-05T23:59:59+08:00")        # 边界内，正常

# 以下都应抛 ValueError
split_iso("2024-03-05T24:00:00+08:00")        # hour 越界
split_iso("2024-03-05T08:75:00+08:00")        # minute 越界
split_iso("2024-03-05T08:30:99+08:00")        # second 越界
```

注意 `23:59:59` 是合法的，不要误伤边界值。

## 环境

- envkit 0.6.0
- Python 3.10+
