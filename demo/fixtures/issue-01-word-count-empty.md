<!--
title: word_count("") 返回 1，空字符串应返回 0
labels: bug, good first issue
target: tier1（难度低 + 重要性高）
-->

## 问题描述

`word_count("")` 返回 `1`，但空字符串里显然有 **0** 个单词。这个错误会让所有基于
单词数做统计/计费/分页的上层调用全部偏移 1。

## 复现步骤

```python
from textkit import word_count

print(word_count(""))
```

**实际输出**

```
1
```

**期望输出**

```
0
```

## 影响范围

- 空输入是最常见边界情况之一（读空文件、前端空提交、CSV 空行都会产生 `""`）。
- 上层若用 `word_count` 做「是否有内容」的判断，会把空文本误判为有内容。
- 用 `word_count` 做计费的场景会多算 1 个词。

## 根因分析

`textkit/stats.py`：

```python
def word_count(text: str) -> int:
    return len(text.split(" "))
```

`"".split(" ")` 返回 `['']`（含一个空字符串元素的列表），长度是 1。
应该先剥离首尾空白，或改用 `text.split()`。

## 期望修复

`word_count("")` 返回 `0`，其余用例不受影响：

```python
word_count("")            # 0
word_count("   ")         # 0
word_count("hello")       # 1
word_count("hello world") # 2
```

## 环境

- textkit 0.3.1
- Python 3.10+
