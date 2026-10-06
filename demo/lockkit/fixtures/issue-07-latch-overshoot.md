<!--
title: Latch 单次 count_down 超过剩余次数时 open 状态错乱
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`Latch(count)` 应表示「累计落下达到 `count` 次即打开」。
但 `remaining` 用的是 `max(0, …)` 做了下限裁剪，
而 `open` 判断的是 `_remaining == 0`，两者口径不一致：
一次落下的次数**超过**剩余次数时，`_remaining` 变成负数，
`open` 既不认为已打开，`remaining` 又显示 0。

## 复现步骤

```python
from lockkit import Latch

latch = Latch(2)
print("remaining:", latch.remaining)          # 2

latch.count_down(5)                            # 一次落 5 次，远超需要的 2 次
print("remaining:", latch.remaining)          # 0
print("open:", latch.open)
print("count_down 返回值:", latch.count_down(0) if False else "-")
```

**实际输出**

```python
remaining: 2
remaining: 0
open: False
```

**期望输出**

```python
remaining: 2
remaining: 0
open: True
```

`count_down(5)` 的返回值同样应当是 `True`（它表示闩锁是否已打开）。

## 影响范围

- 闩锁经常用来「等 N 个分片全部就绪」。批量就绪（一次报多个）时
  只报一次就再也不会打开，等待方永久阻塞。
- 只在「一次落下跨过阈值」时出现；逐次 `count_down()` 的测试测不到。

## 根因分析

`lockkit/gate.py`：

```python
@property
def remaining(self) -> int:
    return max(0, self._remaining)       # ← 对外裁到 0

@property
def open(self) -> bool:
    return self._remaining == 0          # ← 但内部是负数，判不出已打开
```

## 期望修复

```python
latch = Latch(2)
assert latch.count_down(5) is True
assert latch.open is True
assert latch.remaining == 0

latch2 = Latch(3)
assert latch2.count_down(1) is False     # 没到阈值仍是关闭
assert latch2.open is False
```

需要满足：累计落下次数**达到或超过** `count` 即视为已打开；
`remaining` 对外始终不小于 0；未达阈值时保持关闭。

## 环境

- lockkit 0.7.0
- Python 3.10+
