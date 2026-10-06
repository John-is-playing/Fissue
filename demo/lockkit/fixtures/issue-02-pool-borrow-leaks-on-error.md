<!--
title: ResourcePool.borrow() 在代码块内抛异常时不会归还资源
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`ResourcePool.borrow()` 声称「退出时自动归还」，但实现里没有 `try/finally`。
只要 `with` 代码块里抛出异常，`release()` 就被跳过，
这个资源**永久泄漏**——再也没法被取出。

## 复现步骤

```python
from lockkit import ResourcePool

pool = ResourcePool(1, lambda: object())
print("初始可用:", pool.available)      # 1

try:
    with pool.borrow():
        raise RuntimeError("业务出错")
except RuntimeError:
    pass

print("异常之后可用:", pool.available)
print("还能借到吗:", pool.acquire())
```

**实际输出**

```python
初始可用: 1
异常之后可用: 0
还能借到吗: None
```

**期望输出**

```python
初始可用: 1
异常之后可用: 1
还能借到吗: <object object at ...>
```

## 影响范围

- 资源池通常包着连接 / 句柄这类昂贵资源。一次异常就永久少一个，
  跑得越久可用资源越少，最终整个池被耗尽，且**没有任何报错**。
- 正常路径（无异常退出）完全正常，所以基线测试与快速自测都发现不了。

## 根因分析

`lockkit/pool.py`：

```python
@contextmanager
def borrow(self) -> Iterator[Any]:
    item = self.acquire()
    if item is None:
        raise RuntimeError("资源池已耗尽")
    yield item
    self.release(item)            # ← 抛异常时这行不执行
```

## 期望修复

```python
with pytest.raises(RuntimeError):
    with pool.borrow():
        raise RuntimeError("boom")

assert pool.available == 1        # 异常路径也要归还
```

需要满足：无论 `with` 代码块正常结束还是抛异常，资源都必须归还；
「资源池已耗尽」这一前置错误不在此列（没借到就无所谓归还）。

## 环境

- lockkit 0.7.0
- Python 3.10+
