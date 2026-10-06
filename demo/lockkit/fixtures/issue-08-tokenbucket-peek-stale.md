<!--
title: TokenBucket.peek 不按流逝时间补充，读数与实际可取值不一致
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`peek()` 的文档写明它返回**当前**可用令牌数，并且「与 `allow` 一样
先按流逝时间补充，只是不扣减」。

但实现直接返回了 `self._tokens`，跳过了 `_refill()`。
于是在两次 `allow` 之间推进时钟后，`peek()` 给出的读数是**陈旧**的：
它说只有 1 个，实际 `allow(2)` 却能成功。

## 复现步骤

```python
from lockkit import ManualClock, TokenBucket

clk = ManualClock()
bucket = TokenBucket(rate=10, capacity=3, clock=clk)

bucket.allow(2)                 # 取走 2 个，剩 1 个
print("peek:", bucket.peek())   # 1.0

clk.tick(1.0)                   # 过去 1 秒，rate=10 早该补满到容量 3
print("补充 1 秒后 peek:", bucket.peek())
print("实际 allow(2):", bucket.allow(2))
```

**实际输出**

```python
peek: 1.0
补充 1 秒后 peek: 1.0        ← 读数陈旧
实际 allow(2): True          ← peek 与真实可取量矛盾
```

**期望输出**

```python
peek: 1.0
补充 1 秒后 peek: 3.0        ← 已补满到容量
实际 allow(2): True
```

## 影响范围

- `peek` 是给学生 / 监控用的「还能过几个请求」探针。读数偏小会导致
  调用方**误判限流状态**，做出错误的退避或降级决策。
- 更糟的是它和 `allow` 的结论直接矛盾：`peek()` 说 1，
  `allow(2)` 却通过——两个 API 对同一时刻给出不同答案。
- 没有时间推进时 `peek` 是对的，所以只在「先取、再等、再看」的顺序下出现。

## 根因分析

`lockkit/limiter.py`：

```python
def peek(self) -> float:
    """返回当前可用的令牌数，不消耗令牌。"""
    return self._tokens          # ← 少了 self._refill()
```

## 期望修复

```python
clk = ManualClock()
bucket = TokenBucket(rate=10, capacity=3, clock=clk)
bucket.allow(2)
clk.tick(1.0)
assert bucket.peek() == 3.0            # 按流逝时间补充后再报数
assert bucket.peek() == bucket.peek()  # 仍是无副作用的纯读取
```

需要满足：`peek()` 与 `allow()` 看到同一份状态（都先补充）；
补充不得超过容量；`peek()` 依然**不消耗**任何令牌。

## 环境

- lockkit 0.7.0
- Python 3.10+
