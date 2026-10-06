<!--
title: Meter.peak 在没有任何样本时抛 ValueError，应返回 0.0
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`Meter` 各项派生指标在空样本时都约定返回 `0.0`：`mean`、`valley`
都显式做了判空，文档也写着「没有任何样本时返回 0.0」。
唯独 `peak` 忘了判空，直接调用 `max([])`，抛 `ValueError`。

## 复现步骤

```python
from lockkit import Meter, summarize

print("空 meter 的 mean:", Meter().mean)      # 0.0
print("空 meter 的 valley:", Meter().valley)  # 0.0
print("空 meter 的 peak:", Meter().peak)      # 炸了
```

**实际输出**

```python
空 meter 的 mean: 0.0
空 meter 的 valley: 0.0
Traceback (most recent call last):
  ...
ValueError: max() arg is an empty sequence
```

**期望输出**

```python
空 meter 的 mean: 0.0
空 meter 的 valley: 0.0
空 meter 的 peak: 0.0
```

`summarize({...})` 里只要有一个空 `Meter`，整张报表就会崩掉。

## 影响范围

- 监控报表在「当前还没有任何流量」时是常态场景。一个空指标就
  让整个 `summarize` 报表 500，监控页直接不可用。
- 只有在指标恰为空时才触发；一旦有了样本就完全正常，
  所以本地随手一测（先 `record` 再取 `peak`）永远碰不到。

## 根因分析

`lockkit/metrics.py`：

```python
@property
def peak(self) -> float:
    """峰值；没有任何样本时返回 0.0。"""
    return float(max(self._samples))     # ← 少了空列表分支
```

同类指标 `valley` 的写法可直接对照。

## 期望修复

```python
assert Meter().peak == 0.0
assert summarize({"wait": Meter()})["wait"]["peak"] == 0.0
assert Meter().record(5).peak == 5.0      # 非空时行为不变
```

需要满足：空样本返回 `0.0`（与 `mean` / `valley` 口径一致）；
非空时仍返回真实最大值。

## 环境

- lockkit 0.7.0
- Python 3.10+
