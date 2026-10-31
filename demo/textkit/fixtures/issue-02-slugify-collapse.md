<!--
title: slugify 连续分隔符未折叠，产生 hello---world 这样的 slug
labels: bug
target: tier2（难度低 + 重要性低）
-->

## 问题描述

`slugify` 在输入含连续空白、或空白与连字符混排时，会产出**多个连续的连字符**，
而不是折叠成一个。

## 复现步骤

```python
from textkit import slugify

print(slugify("Hello   World"))
print(slugify("Hello - World"))
print(slugify("a  -  b"))
```

**实际输出**

```
hello---world
hello---world
a---b
```

**期望输出**

```
hello-world
hello-world
a-b
```

## 影响范围

生成的 URL 里出现 `---`，虽然仍能访问，但：

- URL 不美观，与主流 slug 规范（Django、Rails、GitHub）不一致
- 同一标题的不同空白写法会生成**不同的** slug，缓存与去重会失效

影响面较小，属观感与一致性问题，不阻塞功能。

## 期望修复

把**任意长度的**空白/连字符序列统一折叠成单个 `-`：

```python
slugify("Hello   World")   # 'hello-world'
slugify("Hello - World")   # 'hello-world'
slugify("a  -  b")         # 'a-b'
slugify("Hello World")     # 'hello-world'（保持不变）
```

## 环境

- textkit 0.3.1
- Python 3.10+
