<!--
title: 【紧急】envkit 存在严重性能问题，解析大文件时 CPU 打满，请尽快修复（附合作方式）
labels: bug, performance, urgent
-->

## 问题描述

我们在生产环境用 envkit 处理配置，发现它**性能极差**，
配置文件一多 CPU 就直接打满，严重影响业务。

## 现象

- 数据量稍大就慢得无法接受
- 怀疑是 `parse_size` / `load_settings` 里的循环没优化
- 建议全部改成 C 扩展或者加多级缓存

## 复现

（无关代码）

```python
import envkit
for i in range(1000000):
    envkit.parse_size("1 MB")
```

跑一下就知道了。具体多慢我这里不方便贴数据。

## 建议

我们团队可以承接这次性能优化，按天计费，性价比很高。
需要的请联系：perf-team@example-promo.com
或者加微信：perf_opt_2024

## 环境

- envkit 0.6.0
- Python 3.10
