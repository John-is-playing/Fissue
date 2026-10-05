<!--
title: Registry.resolve 传入不同参数时不重建，复用旧实例
labels: bug
-->

## 问题描述

`Registry.resolve(key, **kwargs)` 在首次解析时会用 `kwargs` 构建实例并缓存。
但再次解析同一个 key、且传入**不同的参数**时，它直接返回旧实例，
新参数被静默丢弃。

文档写的是「若传入的参数与首次不同，应重新构建并替换缓存」，
实现里没有这一步。

## 复现步骤

```python
from envkit import Registry

reg = Registry()
reg.register("conn", lambda host="localhost": {"host": host})

print(reg.resolve("conn", host="db1"))
print(reg.resolve("conn", host="db2"))
```

**实际输出**

```python
{'host': 'db1'}
{'host': 'db1'}
```

**期望输出**

```python
{'host': 'db1'}
{'host': 'db2'}
```

## 影响范围

- 多租户 / 多数据源场景下，第二个调用方以为自己连的是 `db2`，
  实际拿到的是第一个调用方的 `db1` 实例——**串库**。
- 参数被静默忽略，没有任何异常或日志，问题只会在数据层面暴露。

## 根因分析

`envkit/registry.py`：

```python
if key in self._instances:
    return self._instances[key]      # ← 不看本次的 kwargs 就返回

instance = self._factories[key](**kwargs)
self._instances[key] = instance
return instance
```

## 期望修复

```python
reg = Registry()
reg.register("conn", lambda host="localhost": {"host": host})
a = reg.resolve("conn", host="db1")
b = reg.resolve("conn", host="db2")
assert a["host"] == "db1"
assert b["host"] == "db2"
assert b is not a               # 参数变了就重建

# 参数不变时仍应复用缓存
c = reg.resolve("conn", host="db2")
assert c is b
```

注意「参数相同仍复用」与「参数不同即重建」都要成立。

## 环境

- envkit 0.6.0
- Python 3.10+
