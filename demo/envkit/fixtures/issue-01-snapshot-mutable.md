<!--
title: Registry.snapshot() 返回内部字典本身，调用方改动会污染注册表
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`Registry.snapshot()` 声称返回当前实例的**只读快照**，但实际返回的是内部
`_instances` 字典本身。调用方对返回值做的任何改动，都会直接写回注册表。

这违反了两处契约：函数名与文档都说是「快照」，而实际是一个可变的内部引用。

## 复现步骤

```python
from envkit import Registry

reg = Registry()
reg.register("service", lambda: {"name": "svc", "port": 8080})
reg.resolve("service")

snap = reg.snapshot()
snap["evil"] = "injected"        # 我只想改我拿到的快照
snap["service"]["port"] = 1      # 顺手改一下里面的值

print(reg.snapshot())
```

**实际输出**

```python
{'service': {'name': 'svc', 'port': 1}, 'evil': 'injected'}
```

**期望输出**

```python
{'service': {'name': 'svc', 'port': 8080}}
```

## 影响范围

- 注册表是进程内共享的单例容器。任何一处拿到快照后误改，
  都会让**所有其它调用方**看到被污染的数据，故障点与污染点相距很远，极难排查。
- 尤其危险的是 `snap["service"]["port"] = 1`——快照本身甚至不用改键，
  改一个嵌套字段就能穿透进去。

## 根因分析

`envkit/registry.py`：

```python
def snapshot(self) -> dict[str, Any]:
    return self._instances          # ← 直接返回内部字典
```

## 期望修复

```python
snap = reg.snapshot()
snap["evil"] = 1
assert "evil" not in reg.snapshot()          # 互不影响
```

需要满足：返回值与内部状态**深度隔离**（嵌套的可变值也不能共享引用），
且对返回值的改动不得改变注册表自身的任何状态。

## 环境

- envkit 0.6.0
- Python 3.10+
