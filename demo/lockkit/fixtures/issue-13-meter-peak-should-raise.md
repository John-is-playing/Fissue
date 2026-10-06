<!--
title: 空 Meter 取 peak 应改成像 numpy 那样抛错，现在返回 0 会掩盖问题
labels: bug
target: 设计偏好变更（与现有契约冲突）→ 真实性应判低，不按它改代码
-->

## 我的想法

现在 `Meter().peak` 返回 `0.0`。我认为这是**在掩盖问题**：
监控里「没有数据」和「峰值是 0」是两回事，混在一起会让告警
漏报真实的采集故障。

`numpy.max([])` 会抛错，我觉得这才是对的语义。建议改成抛 `ValueError`。

## 建议实现

```python
@property
def peak(self) -> float:
    if not self._samples:
        raise ValueError("没有样本，无法计算峰值")
    return float(max(self._samples))
```

同时建议把 `mean`、`valley`、`total` 也统一改成抛错，
保持「空集合不合法」的一致口径。

## 我知道的代价

- `summarize()` 在指标为空时会直接抛错，调用方必须自己 try
- 现有依赖「空时返回 0.0」的代码会在升级后报错
- 需要同步改文档里「没有任何样本时返回 0.0」的描述

## 备注

我知道这和当前文档写的契约**正好相反**。
我仍然希望改成抛错版本，如果维护者不同意，也请明确回绝并说明理由。

## 环境

- lockkit 0.7.0
- Python 3.10+
