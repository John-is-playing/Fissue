<!--
title: resolve_index 拒绝负数下标，slice(-1) 语法失效
labels: bug
target: tier2（难度低 + 重要性低）→ 应自动修复
-->

## 问题描述

`resolver.resolve_index(items, index)` 对负索引直接抛 `IndexError`，
但它的契约是「支持 Python 切片语义，`-1` 表示最后一个元素」。

## 复现步骤

```python
from pipekit import resolve_index

print(resolve_index(["a", "b", "c"], -1))
```

**实际输出**

```
IndexError: index 越界
```

**期望输出**

```
c
```

## 影响范围

- pipeline 配置里「取最后一批」是常见写法，负索引被拒会让这类配置直接崩。
- 影响面限于负索引调用；正索引与越界检测目前是好的，所以紧急度不高。

## 根因分析

`pipekit/resolver.py` 增加了一段多余的拦截：

```python
if index < 0:
    raise IndexError("index 越界")     # ← 提前拒掉了合法的负索引
return items[index]
```

`items[index]` 本身就能正确处理负索引，越界时也会自然抛 `IndexError`，
这段判断纯属画蛇添足。

## 期望修复

```python
resolve_index(["a", "b", "c"], -1)   # 'c'
resolve_index(["a", "b", "c"], 0)    # 'a'
resolve_index(["a", "b", "c"], 1)    # 'b'
resolve_index(["a", "b"], 5)         # 仍应抛 IndexError
resolve_index("abc", 0)              # 仍应抛 TypeError
```

## 环境

- pipekit 0.3.1
- Python 3.10+
