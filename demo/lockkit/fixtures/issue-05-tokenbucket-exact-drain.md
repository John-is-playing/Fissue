<!--
title: TokenBucket.allow 用严格大于比较，恰好取尽全部令牌时被拒
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`allow(n)` 在「可用令牌数**恰好等于** `n`」时应当成功取尽，
但实现用的是 `self._tokens > n`，边界被判失败。

## 复现步骤

```python
from lockkit import TokenBucket

bucket = TokenBucket(rate=10, capacity=3)
print("初始令牌:", bucket.peek())        # 3
print("allow(3):", bucket.allow(3))
```

**实际输出**

```python
初始令牌: 3
allow(3): False
```

**期望输出**

```python
初始令牌: 3
allow(3): True
```

## 影响范围

- 限流器最常见的用法就是「按桶的容量整批取」——攒满一桶发一批。
  恰好取尽被判失败后，调用方会白白多等一个补充周期，
  吞吐量直接腰斩。
- 这个边界只在「可取数量 == 余额」时出现，`allow(2)`、`allow(4)`
  这类测试都碰不到。

## 根因分析

`lockkit/limiter.py`：

```python
def allow(self, n: float = 1) -> bool:
    if n <= 0:
        raise ValueError("n 必须为正数")

    self._refill()
    if self._tokens > n:          # ← 应是 >=：恰好取尽也是允许的
        self._tokens -= n
        return True
    return False
```

## 期望修复

```python
bucket = TokenBucket(rate=10, capacity=3)
assert bucket.allow(3) is True
assert bucket.peek() == 0
assert bucket.allow(1) is False      # 取尽之后再取就真的没有了
```

需要满足：`_tokens >= n` 时取走令牌并返回 `True`；
不足时**不扣减任何令牌**并返回 `False`（这条现有行为要保住）。

## 环境

- lockkit 0.7.0
- Python 3.10+
