<!--
title: Once 在首次执行失败后也标记为已完成，后续调用直接返回 None
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`Once` 的契约是「保证被包装的操作**最多成功执行一次**」，并且
「首次调用失败 → 不缓存，后续调用会重新执行」。

实现里 `except` 分支却把 `_done` 置成了 `True`，还把 `_value` 留成 `None`。
于是第一次失败之后，`Once` 就像一个**成功返回了 `None` 的已完成对象**：
后续调用不再执行，直接返回 `None`。

## 复现步骤

```python
from lockkit import Once

once = Once()

def init():
    raise ValueError("数据库还没起来")

try:
    once.call(init)
except ValueError as e:
    print("首次失败:", e)

print("done:", once.done)
print("第二次调用结果:", once.call(lambda: "初始化成功"))
```

**实际输出**

```python
首次失败: 数据库还没起来
done: True
第二次调用结果: None
```

**期望输出**

```python
首次失败: 数据库还没起来
done: False
第二次调用结果: 初始化成功
```

## 影响范围

- `Once` 的典型用途是「初始化只做一次」。启动期依赖没就绪导致首次失败后，
  真实现被永久跳过，后续拿到的是 `None`——调用方往往在很远的地方
  才因 `None` 报错，故障点与根因相距极远。
- 这个分支要求「第一次就失败」，在 happy-path 测试里永远不会走到。

## 根因分析

`lockkit/gate.py`：

```python
try:
    value = fn()
except BaseException:
    self._done = True          # ← 失败也标成已完成
    raise
```

## 期望修复

```python
once = Once()
with pytest.raises(ValueError):
    once.call(init)

assert once.done is False
assert once.call(lambda: "ok") == "ok"      # 失败后可以重试
assert once.calls == 2
```

需要满足：失败**不**写入已完成状态、不缓存任何返回值；
异常照旧向上传播；成功之后仍然只执行一次。

## 环境

- lockkit 0.7.0
- Python 3.10+
