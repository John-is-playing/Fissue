<!--
title: 配置合并与环境变量的类型口径不统一：字符串化、深浅合并、覆盖语义各异
labels: bug
target: 难度高（需跨模块重构 + 设计决策）→ 不自动修复，人工定夺
-->

## 问题描述

`envkit` 里「把多来源配置合成一份最终配置」这件事散落在
`config.load_settings`、`Registry.resolve` 的 kwargs、以及缓存初始化三处，
彼此的**类型口径与覆盖语义并不一致**：

1. `load_settings` 把环境变量的值**一律保留成字符串**
   （`{"PORT": 8080}` + `PORT=9090` → `"9090"`），
   调用方必须自己再转一次 `int()`。
2. 嵌套字典的合并方式不统一：有的地方整体替换，有的地方期望按层合并。
3. `get_bool` 只认一小撮词，未知值静默回退 `default`，
   而 `parse_port` 这类则是直接抛错——同一个库里「非法输入」的处置方式有两套。

## 现状示例

```python
from envkit import load_settings, get_bool, parse_port

print(load_settings({"PORT": 8080}, env={"PORT": "9090"}))
# {'PORT': '9090'}    ← 类型从 int 变成了 str

print(get_bool("X", default=False, env={"X": "enabled"}))   # False（静默回退）
print(parse_port("abc"))                                    # ValueError（直接抛）
```

两次「非法输入」，一次静默、一次抛错。

## 期望

希望统一成一套明确的口径，例如：

- 所有来源的值都按 `defaults` 里同名 key 的**类型**做转换
  （`defaults` 是 `int` 就转 `int`，失败则抛错）
- 「非法输入」要么一律抛错、要么一律回退，并写进文档
- 嵌套合并规则（深合并 vs 整体替换）在文档里讲清楚

## 为什么需要设计决策

上面每一条都有多种合理方案，选哪种会影响现有 API 的兼容性：

- 统一成「按类型转换 + 非法即抛错」会**破坏**现在「保留字符串」的行为，
  已有调用方需要跟着改。
- 保留字符串又会把类型转换的负担永远留给调用方。

因此需要先定口径、再考虑是否加 deprecation 过渡，**不适合自动生成补丁**。

## 环境

- envkit 0.6.0
- Python 3.10+
