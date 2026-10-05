<!--
title: 请求支持树形分组（tree grouping）能力
labels: feature, enhancement
target: FEATURE → 只打标签、不生成验证器
-->

## 需求描述

希望 pipekit 支持**树形分组**：把一个区间的数据点按给定层级拆成父子结构，
便于做分层采样。

期望的 API 大致是：

```python
from pipekit import build_tree

tree = build_tree(0, 99, [10, 3])
# 先按 10 个一组、再按 3 个一组，产出嵌套结构
```

## 动机

现在的 `plan()` 只能产出一层平铺的分片。分层采样需要先粗分组再细分组，
调用方只能自己套两层循环，容易把边界处错。

## 建议的实现方向

- 新增 `pipekit/tree.py`，复用 `intervals` 的闭区间语义
- `build_tree(start, end, widths)` 逐层切分，末层不超过 `end`
- 与 `plan()` 保持一致的边界约定

## 备注

这是新能力，不涉及现有行为变更；如果暂时排不上，先记录即可。
