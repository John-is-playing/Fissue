<!--
title: sanitize_filename 不处理平台保留名，CON / NUL 会生成无法创建的文件名
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`sanitize_filename` 的职责是把用户输入清洗成**安全的文件名**，
但平台保留名（`CON` / `PRN` / `AUX` / `NUL` / `COM1` / `LPT1`）原样放行。

在 Windows 上这些名字代表设备，用来建文件会直接失败或产生诡异行为。

## 复现步骤

```python
from envkit import sanitize_filename

print(sanitize_filename("CON"))
print(sanitize_filename("nul.txt"))
print(sanitize_filename("COM1"))
```

**实际输出**

```
CON
nul.txt
COM1
```

**期望输出**

```
_CON
_nul.txt
_COM1
```

## 影响范围

- 用户上传的文件名叫 `NUL`、`con`、`aux` 时，落在磁盘上的写入会失败或
  被重定向到设备；`sanitize_filename` 正是为了拦住这类输入而存在的。
- 当前它把大量非法输入都正确清洗了，却唯独漏掉保留名这一类，
  给人一种「已经安全了」的错觉。

## 根因分析

`envkit/validate.py` 目前只做了字符替换与 `..` 处理：

```python
cleaned = name.replace("..", "_")
cleaned = _UNSAFE.sub("_", cleaned)
cleaned = cleaned.strip(" .")
```

没有任何针对保留名的判断（大小写不敏感：`con`、`Con`、`CON` 都要处理）。

## 期望修复

```python
assert sanitize_filename("CON") == "_CON"
assert sanitize_filename("con") == "_con"
assert sanitize_filename("NUL") == "_NUL"
assert sanitize_filename("nul.txt") == "_nul.txt"
assert sanitize_filename("report.txt") == "report.txt"   # 普通名字不变
```

判定要大小写不敏感，且只针对「主名恰好是保留名」的情况
（`console.txt` 不是保留名，不应被加下划线）。

## 环境

- envkit 0.6.0
- Python 3.10+（Windows 上影响最明显）
