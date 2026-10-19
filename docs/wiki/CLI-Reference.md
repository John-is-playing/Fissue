# CLI 参考

> 命令总览 + 常用参数 + 全局参数 + 退出码。完整参数以 `fissue <命令> --help` 为准。
> 场景化用法见[使用场景与配方](Workflows.md)。

---

## 命令一览

| 命令 | 说明 |
|---|---|
| `fissue onboard` | **初始化向导**：交互式配好模型 / 数据库 / 平台凭据 / 监控仓库，并抓第一轮。无头机器用 `--yes` + 参数走非交互 |
| `fissue db init` / `fissue db check` | 建表 / 迁移；检查连通性 |
| `fissue fetch` | 抓取 Issue/PR（增量） |
| `fissue eval` | AI 评测（四维评分 + 标签 + 反刷子） |
| `fissue verify` | 生成验证器 + 沙盒 F2P 验证 |
| `fissue flush` | 手动 flush 验证 / 修复队列 |
| `fissue requeue` | 把卡住的条目放回流水线 |
| `fissue fix` | 自动修复并提 PR（`--dry-run` 只产 patch） |
| `fissue report` | 生成 Markdown / JSON 报告 |
| `fissue export` | 导出全量数据 |
| `fissue status` | 查看当前状态：条目统计、队列、模型用量、沙盒可用性 |
| `fissue serve` | 常驻服务：增量扫描 + 队列调度 + 阈值通知（**也含验证**） |
| `fissue web` | Web 工作台 + REST API |
| `fissue sandbox serve` / `fissue sandbox check` | 起沙盒转发组件 / 检查沙盒环境是否就绪 |
| `fissue bench` | 跑分基线：`capture` 冻结 / `diff` 逐项对比 / `list` 列出 |
| `fissue auth` | 本机自救：`status / set-password / reset / key list|create|revoke|import-legacy` |

---

## 全局通用参数

几乎所有命令都支持：

```
-c, --config PATH   指定 config.yaml
-e, --env PATH      指定 .env
-v, --verbose       输出调试日志
--json              JSON 输出（便于脚本消费）
```

---

## 逐命令参数

### `fetch`

```bash
fissue fetch [-r owner/name] [-p 平台] [--full] [--limit N] [--concurrency N]
```

- `-r/--repo`：`owner/name`（写 `--platform` 可显式指定平台，默认 github；也可写成 `gitee:owner/name`）
- `--full`：忽略增量游标，全量重抓
- `--limit`：本轮最多抓多少
- `--concurrency`：并发数

### `eval`

```bash
fissue eval [-r owner/name] [--all] [--deep] [--limit N] [--json]
```

- `--all`：评测所有未评测的（含历史）
- `--deep`：PR 额外拉 diff 评代码质量（更贵更准）
- 注：`eval` **没有 `--key` 参数**（它是批量语义），单条请用 `--limit 1`。

### `verify`

```bash
fissue verify [-r owner/name] [--key KEY] [--limit N] [--json]
```

- `--key`：只验证这一条（`平台:owner/repo#编号`）
- 跑整批前会做一次**基础镜像前置检查**：只查存在性、不触发拉取；缺镜像时毫秒级拦下整批并给出 `docker build` 命令。

### `flush`

```bash
fissue flush [--queue verify|fix_bug|fix_feature] [--force/--auto] [--limit N]
```

- 不带 `--queue` 时 flush 全部队列
- `--auto`：按触发条件判断（不强制），常用于 cron；`--force` 强制

### `requeue`

```bash
fissue requeue --stale [--dry-run] [--yes]     # 结论过期但修复已开工
fissue requeue --failed [--yes]                # 失败条目重试
```

`--failed` 的回退目标按**已有结论**决定：有评测 → `evaluated`（重跑验证），没有 → `new`（重跑评测）。默认先列计划再询问确认。

> 为什么 `--stale` 不自动重评：条目内容实质变化时它可能已开工，自动回落会给「重复修复 / 重复建 PR」再开一道口子。
> 那里只留痕（`stale_reasons`，导出与 API 可见），由人确认后放回。

### `fix`

```bash
fissue fix [-r owner/name] [--key KEY] [--limit N] [--plan] [--dry-run] [--rounds N]
```

- `--plan`：先规划优先级
- `--dry-run`：只产出 patch，不建分支、不提 PR
- `--rounds`：覆盖 Agent 循环轮次上限

### `report` / `export`

```bash
fissue report [-r owner/name] [-f markdown|json] [-o 路径] [--with-body]
fissue export [-f json|markdown] [-o 路径] [--category X] [--status Y]
```

- `report --with-body`：附带 Issue 正文
- `export --category` / `--status`：按条件过滤

### `status`

```bash
fissue status [--json]
```

打印条目统计、队列情况、当日用量（prompt/completion token、估算美元、调用次数）、沙盒可用性。

### `serve`

```bash
fissue serve [--once] [--with-web]
```

- `--with-web`：同进程带起 Web/API
- `--once`：只跑一轮就退出（适合系统 cron / 任务计划）

### `web`

```bash
fissue web [--host H] [--port P] [--reload]
```

### `sandbox`

```bash
fissue sandbox check        # 检查 Docker 与隔离参数
fissue sandbox serve        # 起转发组件（宿主侧常驻；Windows 不支持 Unix socket，会自动 inline）
```

### `bench`

```bash
fissue bench capture [-r owner/name] [--label 名字] [-o 路径] [--json]
fissue bench list [--json]
fissue bench diff 旧.json 新.json [--json]
```

**退出码**：`0` 可比且无变化｜`1` **不可比**（结论不能当回归依据）｜`2` 可比但有变化。
基线默认落在 `data/benchmarks/`，是**唯一进版本库的 `data/` 子目录**。

### `auth`

```bash
fissue auth status
fissue auth set-password
fissue auth reset                     # 清 2FA / 重置口令
fissue auth key list|create|revoke
fissue auth key import-legacy
```

界面上进不去时的**本机自救通道**（口令忘了、手机丢了、Key 全吊销）。

---

## 常见产物位置

```
data/
  artifacts/<key>/base/       # 验证器 base 阶段输出（stdout/stderr/meta）
  artifacts/<key>/fix/        # 修复阶段输出
  artifacts/<key>/agent/      # Agent 循环每轮输出
  manual/<key>/report.md      # 需人工处理的完整报告
  manual/<key>/attempt.patch  # 尝试过的补丁
  patches/<key>.patch         # dry-run / patch_only 产出的补丁
  exports/                    # report / export 输出
  benchmarks/                 # bench capture 的基线（进版本库）
```

---

## 相关页

[快速开始](Getting-Started.md) · [配置参考](Configuration.md) · [使用场景与配方](Workflows.md) · [故障排查](Troubleshooting.md) · [Web 与 REST API](Web-and-API.md)
