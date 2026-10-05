<!--
title: get_bool 对环境变量大小写敏感，TRUE 被当成无法识别
labels: bug
-->

## 问题描述

`get_bool` 的文档写着「取值大小写不敏感（`TRUE` / `True` / `true` 等价）」，
但实现没有做大小写归一。小写的 `true` 能识别，`TRUE`、`True` 会被当成
无法识别的值，直接落到 `default`。

## 复现步骤

```python
from envkit import get_bool

print(get_bool("DEBUG", default=False, env={"DEBUG": "true"}))   # 小写
print(get_bool("DEBUG", default=False, env={"DEBUG": "TRUE"}))   # 大写
print(get_bool("DEBUG", default=False, env={"DEBUG": "True"}))   # 首字母大写
```

**实际输出**

```
True
False
False
```

**期望输出**

```
True
True
True
```

## 影响范围

- 环境变量在不同平台/不同部署脚本里的大小写并不统一。
  `DEBUG=TRUE` 是最常见的写法之一，却会静默失效。
- 失效方向是「静默取默认值」，不报错、不打日志，排查时最先怀疑的是别处。

## 根因分析

`envkit/config.py`：

```python
word = raw.strip()          # ← 只去了空白，没有 lower()
if word in _TRUE_WORDS:
```

`_TRUE_WORDS` / `_FALSE_WORDS` 里的词全是小写，所以只有恰好小写才命中。

## 期望修复

```python
assert get_bool("F", env={"F": "TRUE"}) is True
assert get_bool("F", env={"F": "True"}) is True
assert get_bool("F", env={"F": "OFF"}) is False
assert get_bool("F", env={"F": "yes"}) is True
```

大小写归一后，原有的小写识别与未知值回退到 `default` 的行为保持不变。

## 环境

- envkit 0.6.0
- Python 3.10+
