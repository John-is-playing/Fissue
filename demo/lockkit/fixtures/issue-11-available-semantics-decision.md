<!--
title: 「可用数」的口径在 Semaphore / ResourcePool / TokenBucket 之间不一致
labels: bug, discussion
target: 难度高（需设计决策，会破坏现有 API）→ 不自动修复，只打标签
-->

## 问题描述

三个组件都对外暴露「当前还能用多少」的概念，但类型与语义各不相同：

| 组件 | 暴露形式 | 类型 | 语义 |
|---|---|---|---|
| `Semaphore` | `available` | `int` | 空闲许可数 |
| `ResourcePool` | `available` | `int` | 空闲资源数 |
| `ResourcePool` | `in_use` | `int` | 已借出数 |
| `TokenBucket` | `peek()` | `float` | 剩余令牌数 |

我想统一成一套口径，但**该怎么统一本身就是个设计问题**，我拿不准，希望维护者先给个结论再动手：

1. 统一叫 `available` 还是 `balance`？
2. `TokenBucket.peek()` 要不要改成一个属性 `available`？改了就是**破坏性变更**，
   现有 `bucket.peek()` 的调用方全部要改。
3. 令牌是浮点数，许可 / 资源是整数。统一后返回 `float` 还是 `int`？
4. `in_use` 只有 `ResourcePool` 有，要不要给 `Semaphore` 也补上？

## 影响

- 现在写通用监控代码必须为每个组件单独适配，很啰嗦
- 但任何统一方案都会碰到上面的取舍，**改错了就是大面积破坏**

## 备注

这是一个需要**维护者拍板**的设计决策，不是可以自动修掉的缺陷。
请先讨论、给出结论，暂时不要改代码。

## 环境

- lockkit 0.7.0
- Python 3.10+
