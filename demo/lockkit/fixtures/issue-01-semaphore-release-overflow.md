<!--
title: Semaphore.release() 可以无限归还，可用许可数会超过初始值
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`Semaphore` 只维护了一个 `available` 计数器，`release()` 直接自增，
完全不管已经归还了多少次。于是**多归还几次就能凭空造出许可**，
`available` 会超过创建时声明的 `permits`。

这违反了两处契约：

1. 信号量的许可总数是固定的，归还不能使其超过初始值；
2. 类文档写明「`release()` 归还的许可不得使可用数超过初始许可数」。

## 复现步骤

```python
from lockkit import Semaphore

sem = Semaphore(1)
assert sem.acquire() is True        # 借走唯一一个许可
assert sem.available == 0

sem.release()                       # 正常归还
sem.release()                       # 又还了一次（调用方多还 / 重复归还）
sem.release()

print(sem.available)
```

**实际输出**

```python
3
```

**期望输出**

```python
1
```

## 影响范围

- 信号量是限流 / 并发控制的基础设施。许可数被凭空放大后，
  并发度的硬上限直接失效——本该挡住第 2 个请求，现在放进来 4 个。
- 这类缺陷在单次「借—还」配对的正常路径上看不出来，
  必须**多还**才暴露；因此很容易在 code review 里被放过。

## 根因分析

`lockkit/pool.py`：

```python
def release(self) -> None:
    """归还一个许可。"""
    self._available += 1          # ← 没有跟 _limit 比较
```

## 期望修复

```python
sem = Semaphore(1)
sem.acquire()
sem.release()
sem.release()
assert sem.available == 1          # 依然封顶在初始许可数
```

需要满足：`available` 的任何时候都不超过 `limit`；
消除冗余归还带来的影响（多余的归还不产生额外许可，也不抛错）。

## 环境

- lockkit 0.7.0
- Python 3.10+
