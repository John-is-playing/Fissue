<!--
title: TTLCache 读取时忽略过期时间，过期数据不会失效
labels: bug
-->

## 问题描述

`TTLCache` 的 `get()` 完全不检查过期时间。只要一条记录写进去过，
之后无论过多久都能读到，`ttl` 形同虚设。

`contains` 与 `purge` 都正确实现了过期判定，只有 `get` 漏了——
这说明 `get` 的实现漏掉了本该有的判断，而不是设计如此。

## 复现步骤

用一个可控时钟，避免依赖真实时间、保证稳定复现：

```python
from envkit import TTLCache

class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def tick(self, s): self.now += s

clock = Clock()
cache = TTLCache(ttl=10, clock=clock)

cache.set("token", "abc")
clock.tick(11)          # 已远超 ttl

print(cache.get("token"))
print("token" in cache)
```

**实际输出**

```
abc
False
```

**期望输出**

```
None
False
```

同一个缓存对象上，`get` 说「还在」，`in` 说「没了」——自相矛盾。

## 影响范围

- 过期的 token / 鉴权结果 / 配置快照会被继续当成有效值使用。
- 在本库内部，`get` 与 `contains` 对同一条记录给出相反结论，
  任何按 `contains` 做前置判断、再用 `get` 取值的代码都会取到脏数据。

## 根因分析

`envkit/cache.py` 的 `get()` 只查了 key 是否存在，没有比较过期时间：

```python
value, _expires_at = entry
self._hits += 1
return value
```

而同一个文件里 `contains()` 的写法是正确的：

```python
return self._clock() < entry[1]
```

## 期望修复

```python
cache = TTLCache(ttl=10, clock=clock)
cache.set("k", "v")
clock.tick(9)     # 未过期
assert cache.get("k") == "v"
clock.tick(1)     # 刚好到期
assert cache.get("k") is None
```

到期的边界（`now >= expires_at` 视为过期）应与 `contains` / `purge` 保持一致。

## 环境

- envkit 0.6.0
- Python 3.10+
