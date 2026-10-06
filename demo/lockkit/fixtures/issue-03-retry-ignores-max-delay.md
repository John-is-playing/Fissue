<!--
title: RetryPolicy.delay_for 不按 max_delay 截断，退避等待无限增长
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`RetryPolicy` 支持 `max_delay`，文档写的是「单次等待的**上限**秒数」。
但 `delay_for()` 只做了指数增长，**完全没有读 `max_delay`**，
于是等待时间会一路翻倍到不可控。

## 复现步骤

```python
from lockkit import RetryPolicy

policy = RetryPolicy(attempts=8, base_delay=1.0, factor=2.0, max_delay=5.0)
for attempt in range(1, 7):
    print(attempt, policy.delay_for(attempt))
```

**实际输出**

```python
1 1.0
2 2.0
3 4.0
4 8.0      ← 已经超过 max_delay=5.0
5 16.0
6 32.0
```

**期望输出**

```python
1 1.0
2 2.0
3 4.0
4 5.0
5 5.0
6 5.0
```

## 影响范围

- `max_delay` 是调用方用来给重试**封顶**的安全阀。它失效后，
  配合 `retry_call` 会真的 `sleep` 出 32s、64s、128s……
  线上表现为进程卡死、请求堆积、健康检查超时。
- 更隐蔽的是：前几次的等待时间是对的，只有重试次数够多才暴露，
  所以「重试两次就好了」的测试永远发现不了。

## 根因分析

`lockkit/retry.py`：

```python
def delay_for(self, attempt: int) -> float:
    if attempt < 1:
        raise ValueError("attempt 从 1 起")
    return self.base_delay * (self.factor ** (attempt - 1))   # ← 没用 max_delay
```

## 期望修复

```python
policy = RetryPolicy(attempts=8, base_delay=1.0, factor=2.0, max_delay=5.0)
assert policy.delay_for(3) == 4.0
assert policy.delay_for(4) == 5.0      # 截断到上限
assert policy.delay_for(10) == 5.0     # 永远不超过上限
```

需要满足：`delay_for` 的返回值恒不超过 `max_delay`（当它非 `None` 时）；
`max_delay=None` 表示不封顶，行为保持不变。

## 环境

- lockkit 0.7.0
- Python 3.10+
