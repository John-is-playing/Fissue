<!--
title: load_settings 会改掉传入的 defaults，调用方的原始配置被污染
labels: bug
-->

## 问题描述

`load_settings(defaults, ...)` 文档明确说「`defaults` 本身不会被改动」，
但嵌套字典是浅拷贝的，函数写入嵌套字段时会直接把调用方传进来的对象改掉。

## 复现步骤

```python
from envkit import load_settings

defaults = {"db": {"host": "localhost", "port": 5432}}

settings = load_settings(defaults, env={})
settings["db"]["host"] = "prod-db"          # 我只想改我拿到的配置

print(defaults)
```

**实际输出**

```python
{'db': {'host': 'prod-db', 'port': 5432}}
```

**期望输出**

```python
{'db': {'host': 'localhost', 'port': 5432}}
```

## 影响范围

- `defaults` 常常是**模块级的常量字典**。它一旦被污染，
  同一进程内后续所有 `load_settings` 调用都会拿着被改过的「默认值」，
  而且是跨请求静默扩散，极难定位。
- 测试之间也会互相串味：前一个用例改了配置，后一个用例就失败。

## 根因分析

`envkit/config.py`：

```python
settings = dict(defaults)        # ← 浅拷贝，嵌套值仍是同一个对象
```

`dict()` 只复制了第一层，`settings["db"]` 与 `defaults["db"]` 指向同一个 dict。

## 期望修复

```python
defaults = {"db": {"host": "localhost"}}
settings = load_settings(defaults, env={})
settings["db"]["host"] = "prod-db"
assert defaults["db"]["host"] == "localhost"     # 原对象不受影响
```

需要**深度**隔离：嵌套的 dict / list 等可变值都不能与入参共享引用，
且返回的对象自身也要能在后续被安全修改。

## 环境

- envkit 0.6.0
- Python 3.10+
