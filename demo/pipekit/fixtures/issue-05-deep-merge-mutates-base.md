<!--
title: config.deep_merge 会修改传入的 base 字典
labels: bug
target: tier1（难度低 + 重要性高）→ 应自动修复
-->

## 问题描述

`config.deep_merge(base, override)` 声称返回一个新的合并结果、不修改入参，
但它直接对 `base` 原地写入，**调用方的 base 配置对象被污染了**。

## 复现步骤

```python
from pipekit import deep_merge

base = {"a": {"x": 1}}
deep_merge(base, {"a": {"y": 2}})
print(base)
```

**实际输出**

```
{'a': {'x': 1, 'y': 2}}      ← base 被改了
```

**期望输出**

```
{'a': {'x': 1}}              ← base 必须保持不变
```

## 影响范围

- 分层配置的典型用法是「一份全局默认 base + 每仓库的 override」。
  base 被污染后，**第二个仓库拿到的是上一个仓库合并过的配置**，
  出现难以排查的配置串味。
- 更隐蔽的是：污染在第一次调用时发生，报错却往往出现在很久之后的另一次调用上。

## 根因分析

`pipekit/config.py`：

```python
def deep_merge(base, override):
    result = base                  # ← 只复制了引用，后续写入直接落在 base 上
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)   # 递归同样原地改
        else:
            result[key] = value
    return result
```

注意递归分支：即使外层改成浅拷贝，嵌套字典仍会被改到，必须让递归也基于拷贝。

## 期望修复

```python
base = {"a": {"x": 1}}
merged = deep_merge(base, {"a": {"y": 2}})
assert base == {"a": {"x": 1}}                    # 未被修改
assert merged == {"a": {"x": 1, "y": 2}}          # 结果正确
```

其余合并语义（覆盖标量、递归合并、空字典）保持不变。

## 环境

- pipekit 0.3.1
- Python 3.10+
