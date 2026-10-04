<!--
title: 含税总价反推出来的不含税价好像不对
labels: bug
target: 疑似与 issue #2 重复（用于测试 Fissue 的重复检测）
-->

## 问题

我用 `remove_tax` 从含税价倒推不含税价，结果和财务系统对不上。

```python
from ratekit import remove_tax

print(remove_tax(113.0, 0.13))
```

我这边算出来跟 100 差了一点。具体差多少我也说不太清，
反正财务说不对。税率我给的是 0.13，应该是 13% 没错吧？

## 补充

试了几个数都这样：

```python
remove_tax(113.0, 0.13)
remove_tax(226.0, 0.13)
```

数看着偏小。不确定是不是已经有人提过了，如果重复了直接关掉就行。

## 环境

- ratekit 0.4.0
- Python 3.11
