<!--
title: parse_amount 解析非法输入时应该返回 0 而不是抛异常
labels: bug
target: 误报/无效（库抛异常是正确设计）→ authenticity 应判低
-->

## 问题

`parse_amount` 遇到不合法的输入会直接抛异常，把程序搞崩：

```python
from ratekit import parse_amount

print(parse_amount("abc"))
```

**实际**

```
ValueError: could not convert string to float: 'abc'
```

**我的期望**

```
0.0
```

我觉得解析失败就返回 0 更友好，不应该抛异常。

## 说明

我们的表单里用户经常乱填，一抛异常整个页面就 500 了。
建议所有解析函数都改成解析失败返回 0。

## 环境

- ratekit 0.4.0
- Python 3.10
