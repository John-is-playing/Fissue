<!--
title: slugify 生成的 URL 里出现多余的横线
labels: bug
target: 疑似与已有 issue 重复（用于测试 Fissue 的重复检测）
-->

## 问题

用 `slugify` 生成的 URL 片段里有多余的横线，看起来不太对。

## 复现

```python
from textkit import slugify

slugify("my  title")
```

得到的字符串里横线比预期多。具体期望几个横线我也说不太准，
感觉应该只有一个才对。

## 补充

试了几个输入都这样：

```python
slugify("a  b")
slugify("x - y")
```

横线数量看着偏多。

不确定这个问题是不是已经有人提过了，如果重复了可以直接关掉。

## 环境

- textkit 0.3.1
- Python 3.11
