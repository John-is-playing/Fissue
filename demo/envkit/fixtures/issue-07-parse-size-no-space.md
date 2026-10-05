<!--
title: parse_size 遇到无空格写法直接抛错，"512KB"、"1.5GB" 全都不认
labels: bug, good first issue
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`parse_size` 的文档明确写着「支持带空格与不带空格两种写法」，
但实现只按空格切分，不带空格的写法直接抛 `ValueError`。

## 复现步骤

```python
from envkit import parse_size

print(parse_size("1.5 MB"))     # 带空格 —— 正常
print(parse_size("512KB"))      # 不带空格
```

**实际输出**

```
1500000
ValueError: could not convert string to float: '512KB'
```

**期望输出**

```
1500000
512000
```

## 影响范围

- `"512KB"` / `"1.5GB"` / `"2TB"` 是不带空格的最常见写法
  （配置文件、命令行参数、用户手输基本都是这么写的）。
- 直接抛异常，调用方若没做兜底就会整个流程中断。

## 根因分析

`envkit/units.py`：

```python
number, _, unit = raw.partition(" ")     # ← 只按空格切，无空格时 unit 为空
unit = unit.strip() or "B"               # ← 于是被当成纯字节数
```

`"512KB"` 走到这里 `number` 是 `"512KB"`（数字部分没被拆出来），
`float("512KB")` 自然失败。

## 期望修复

```python
assert parse_size("512KB") == 512_000
assert parse_size("512 KB") == 512_000      # 带空格（保持不变）
assert parse_size("1.5GB") == 1_500_000_000
assert parse_size("2 TB") == 2_000_000_000_000
assert parse_size("4096") == 4096           # 纯数字（保持不变）
assert parse_size("2 G") == 2_000_000_000   # 单字母别名也要支持
```

数字与单位之间应能正确拆分，无论中间有没有空格。

## 环境

- envkit 0.6.0
- Python 3.10+
