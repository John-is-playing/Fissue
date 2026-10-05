<!--
title: 支持从 TOML / YAML 文件装载配置，与 load_settings 合并
labels: enhancement, feature
target: FEATURE（只评测打标签，不生成验证器，不自动修复）
-->

## 背景

现在 `load_settings` 只能从环境变量取值。实际项目里配置还有另外两个来源：
配置文件（YAML / TOML）与命令行参数。

## 期望能力

希望新增一个入口，把文件的配置与默认值合并：

```python
from envkit import load_settings
from envkit.files import load_toml          # 期望新增

settings = load_settings(
    {"db": {"host": "localhost", "port": 5432}},
    source=load_toml("config.toml"),
    env=os.environ,
)
```

优先级期望为：**命令行 > 环境变量 > 配置文件 > 默认值**。

## 验收标准

- 支持 `.toml` 与 `.yaml` 两种格式
- 嵌套字典合并（而不是整体覆盖）
- 文件不存在时给出清晰提示，而不是抛 `FileNotFoundError`
- 新增 `envkit/files.py`，不破坏现有 API

## 备注

这是一个**新增功能**，不是缺陷：现有 `load_settings` 的行为不需要改动。

## 环境

- envkit 0.6.0
- Python 3.10+
