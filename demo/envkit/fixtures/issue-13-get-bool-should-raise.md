<!--
title: get_bool 遇到无法识别的值应该抛错，而不是悄悄返回默认值
labels: bug
-->

## 问题描述

`get_bool("X", env={"X": "enabled"})` 返回了默认值，一声不吭。
我认为这对配置读取来说很危险：环境变量明明**存在**，只是拼写不在白名单里，
却被当成「没设置」。

我希望改成：值存在但无法识别时**抛 `ValueError`**，让部署直接失败暴露问题。

## 建议实现

```python
def get_bool(name, default=False, *, env=None):
    raw = env.get(name)
    if raw is None:
        return default
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f"无法识别的布尔值：{raw!r}")     # ← 改这里
```

## 影响

- 现有依赖「静默回退」的部署会在升级后直接启动失败
- 需要同步改文档里的「变量不存在或无法识别时返回默认值」这句

## 环境

- envkit 0.6.0
- Python 3.10+
