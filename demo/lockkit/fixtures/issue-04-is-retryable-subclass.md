<!--
title: is_retryable 用 type(exc) in retry_on 判定，子类异常不会被重试
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`is_retryable` 的文档明确写着「判定包含**子类**」，
但实现用的是 `type(exc) in retry_on`——精确类型匹配。
于是 `retry_on=(OSError,)` 时，`TimeoutError`（`OSError` 的子类）
被判为不可重试，直接放弃。

## 复现步骤

```python
from lockkit import is_retryable, retry_call, RetryPolicy

print("TimeoutError 是否可重试:", is_retryable(TimeoutError("slow"), (OSError,)))

calls = {"n": 0}
def flaky():
    calls["n"] += 1
    raise TimeoutError("still slow")

try:
    retry_call(flaky, RetryPolicy(attempts=3, base_delay=0.0, retry_on=(OSError,)),
               sleep=lambda _: None)
except TimeoutError:
    pass
print("实际执行次数:", calls["n"])
```

**实际输出**

```python
TimeoutError 是否可重试: False
实际执行次数: 1
```

**期望输出**

```python
TimeoutError 是否可重试: True
实际执行次数: 3
```

## 影响范围

- 网络 / IO 代码几乎都写成「`retry_on=(OSError,)`」，
  而实际抛出的往往是 `ConnectionResetError`、`TimeoutError` 这些子类。
  结果就是**配了重试却一次都不重试**，瞬时抖动全部变成用户可见的失败。
- 只测「恰好等于列出的类型」永远发现不了；必须喂一个子类。

## 根因分析

`lockkit/retry.py`：

```python
def is_retryable(exc: BaseException, retry_on: tuple[type[BaseException], ...]) -> bool:
    return type(exc) in retry_on      # ← 精确匹配，不含子类
```

## 期望修复

```python
assert is_retryable(TimeoutError("x"), (OSError,)) is True   # 子类
assert is_retryable(ValueError("x"), (KeyError,)) is False  # 无关类型仍不可重试
```

需要满足：`is_retryable` 按 `isinstance` 语义判定（含子类）；
不属于所列类型的异常仍返回 `False`。修复后 `retry_call` 对子类异常也会重试。

## 环境

- lockkit 0.7.0
- Python 3.10+
