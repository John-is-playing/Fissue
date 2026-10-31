# 快速开始

> 十分钟跑通「抓取 → 评测 → 沙盒验证 → 批量定论 → 自动修复」。浓缩版；完整参数见
> [CLI 参考](CLI-Reference.md)，细节以 [使用手册](../USAGE.md) 为准。

## 1. 前置条件

| 项 | 要求 | 检查 | 缺失时 |
|---|---|---|---|
| Python | ≥ 3.10 | `python --version` | 装 3.10+ |
| LLM API Key | **必需**（`eval/verify/fix`） | — | 见第 3 步 |
| Docker | 建议（沙盒隔离） | `docker info` | 可降级本地执行，但**无隔离** |

> 单测完全离线：不需要 Key，也不需要 Docker。

## 2. 安装

```bash
python -m venv .venv && source .venv/Scripts/activate   # Linux/macOS 用 .venv/bin/activate
pip install -e ".[dev]"
fissue --help
```

## 3. 配置

```bash
cp .env.example .env && cp config.example.yaml config.yaml
```

最少只填一项：

```bash
FISSUE_LLM_API_KEY=sk-你的真实key
```

`config.yaml` 里**必改**的是登记仓库：

```yaml
repos:
  - platform: github          # github | gitee | atomgit | gitlab
    owner: psf
    name: requests
    since_days: 30            # 0 = 不限
    labels_exclude: [wontfix, duplicate]
```

> Web 工作台与 `fissue onboard` 改配置只写 `config.local.yaml` 覆盖层，手写的 `config.yaml`
> 一字不动。生效优先级：进程环境 > `.env` > 覆盖层 > `config.yaml` > 内置默认。

## 4. 沙盒与数据库

```bash
docker build -t fissue/sandbox-base:latest docker/sandbox    # 首次 3~8 分钟
fissue sandbox check
fissue db init && fissue db check
```

`db init --drop` 清空全部数据（危险）。

## 5. 五步流程

```bash
# 1) 抓取（增量 + 内容指纹去重 + 标签过滤）
fissue fetch --repo psf/requests --limit 50

# 2) 评测：分类 + 四维评分 + 难度 + 反刷子 + 优先级
fissue eval --all --limit 100

# 3) 验证：生成验证器，沙盒里跑 base 阶段
fissue verify --repo psf/requests --limit 10

# 4) 批量定论：打标签，派发修复
fissue flush --auto

# 5) 自动修复：先只产补丁，确认后再提 PR
fissue fix --plan --dry-run --limit 5
fissue fix --plan --limit 3
```

**F2P** 是核心判据：base 阶段（未修复）**必须失败**，fix 阶段（修复后）**必须通过**。

> FEATURE 类在评测就结束，只打标签，不生成验证器、不自动修复。
> 首次上线建议 `pr_strategy.mode: patch_only`，观察几轮补丁质量再放开 `fork`。

## 6. 起服务 / 看板

```bash
fissue serve              # 常驻：扫描 + flush + 验证 + 通知
fissue serve --with-web   # 同进程带 Web/API
fissue serve --once       # 只跑一轮，适合 cron
fissue web                # 工作台 + REST API（默认 127.0.0.1:8000）
```

## 7. 首次试跑（不写平台）

```bash
fissue fetch --repo psf/requests --limit 20
fissue eval --limit 20
fissue status
fissue report --repo psf/requests --out data/exports/first.md
```

只读平台数据、写本地库，**不会**打标签或提 PR。

---

相关：[配置参考](Configuration.md) · [CLI 参考](CLI-Reference.md) · [使用场景](Workflows.md) ·
[故障排查](Troubleshooting.md) · [使用手册](../USAGE.md)