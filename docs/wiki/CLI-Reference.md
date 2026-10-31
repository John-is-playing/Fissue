# CLI 参考

> 命令、常用参数、全局参数、退出码。完整参数以 `fissue <命令> --help` 为准；
> 场景化用法见[使用场景](Workflows.md)，细节见[使用手册](../USAGE.md)。

## 命令一览

| 命令 | 说明 |
|---|---|
| `fissue onboard` | 初始化向导（模型 / 数据库 / 凭据 / 仓库，并抓第一轮）；无头用 `--yes` + 参数 |
| `fissue db init` / `db check` | 建表迁移 / 连通性自检 |
| `fissue fetch` | 抓取 Issue/PR（增量） |
| `fissue eval` | AI 评测（四维评分 + 标签 + 反刷子） |
| `fissue verify` | 生成验证器 + 沙盒 F2P 验证 |
| `fissue flush` | 手动 flush 验证 / 修复队列 |
| `fissue requeue` | 把卡住的条目放回流水线 |
| `fissue force` | 强制修复：无视评测结论推进修复流程 |
| `fissue fix` | 自动修复并提 PR（`--dry-run` 只产 patch） |
| `fissue report` / `export` | Markdown/JSON 报告 / 全量导出 |
| `fissue status` | 条目统计、队列、模型用量、沙盒可用性 |
| `fissue serve` | 常驻：扫描 + 调度 + 验证 + 通知（`--with-web` 带 Web） |
| `fissue web` | Web 工作台 + REST API |
| `fissue sandbox serve` / `check` | 转发组件 / 环境就绪检查 |
| `fissue bench capture\|diff\|list` | 跑分基线冻结 / 逐项对比 / 列出 |
| `fissue platforms doctor` | 平台能力体检（只读默认，`--write` / `--probe-push` 才写） |
| `fissue auth status\|set-password\|reset\|key …` | 本机自救与 API Key 管理 |

## 全局参数

```
-c, --config PATH   指定 config.yaml
-e, --env PATH       指定 .env
-v, --verbose        调试日志
--json               JSON 输出（便于脚本消费）
```

## 逐命令

### `fetch`

```bash
fissue fetch [-r owner/name] [-p 平台] [--full] [--limit N] [--concurrency N]
```

`-r` 可写 `owner/name` 或 `gitee:owner/name`（`-p` 显式指定平台，默认 github）；`--full` 忽略
增量游标；`--limit` 本轮上限；`--concurrency` 并发。

### `eval`

```bash
fissue eval [-r owner/name] [--all] [--deep] [--limit N] [--json]
```

`--all` 含历史未评测；`--deep` 让 PR 额外拉 diff 评代码质量（更贵更准）。
**没有 `--key`**（批量语义），单条用 `--limit 1`。

### `verify`

```bash
fissue verify [-r owner/name] [--key KEY] [--limit N] [--json]
```

跑整批前会做**镜像前置检查**：只查存在性、不触发拉取；缺镜像时毫秒级拦下并给出 `docker build` 命令。

### `flush`

```bash
fissue flush [--queue verify|fix_bug|fix_feature] [--force|--auto] [--limit N]
```

不带 `--queue` 时 flush 全部；`--auto` 按触发条件判断（cron 常用），`--force` 强制。

### `requeue`

```bash
fissue requeue --stale [--dry-run] [--yes]     # 结论过期但修复已开工
fissue requeue --failed [--yes]                # 失败条目重试
```

`--failed` 的回退目标按已有结论决定：有评测 → `evaluated`，没有 → `new`。默认先列计划再确认。
`--stale` 不自动重评：自动回落会给「重复修复 / 重复建 PR」再开一道口子，只留痕（`stale_reasons`）。

### `force`

```bash
fissue force -k platform:owner/name#number [--dry-run] [--yes]
fissue force -k platform:owner/name#number --undo
```

「要不要修」由人拍板：FEATURE 短路、分类 / 刷量 / 可复现闸门、LLM 裁决、修复侧优先级闸门
**全部旁路**，条目直落待修 tier1。**验证器闸门不旁路**——没有验证器无从判定修复，会顺手生成一个。
已在修复流程中（`fixing` / `pr_created` / `fix_queued`）时保持原状（防重复开 PR）。PR 不能强制。

### `fix`

```bash
fissue fix [-r owner/name] [--key KEY] [--limit N] [--plan] [--dry-run] [--rounds N]
```

`--plan` 先规划优先级；`--dry-run` 只产 patch；`--rounds` 覆盖 Agent 轮次上限。

### `report` / `export` / `status`

```bash
fissue report [-r owner/name] [-f markdown|json] [-o 路径] [--with-body]
fissue export [-f json|markdown] [-o 路径] [--category X] [--status Y]
fissue status [--json]
```

`status` 打印条目统计、队列、当日用量（prompt/completion、估算美元、调用次数）、沙盒可用性。

### `serve` / `web` / `sandbox`

```bash
fissue serve [--once] [--with-web]
fissue web [--host H] [--port P] [--reload]
fissue sandbox check · fissue sandbox serve
```

### `bench`

```bash
fissue bench capture [-r owner/name] [--label 名字] [-o 路径] [--json]
fissue bench list [--json]
fissue bench diff 旧.json 新.json [--json]
```

**退出码**：`0` 可比无变化｜`1` **不可比**（不能当回归依据）｜`2` 可比有变化。基线落
`data/benchmarks/`，是唯一进版本库的 `data/` 子目录。

### `auth`

```bash
fissue auth status · set-password · reset · key list|create|revoke
```

界面进不去时的**本机自救通道**（口令忘了、手机丢了、Key 全吊销）。

### `platforms doctor`

```bash
fissue platforms doctor                                  # 只读体检 + 克隆（克隆是只读的，但是 verify/fix 第一道门）
fissue platforms doctor -r gitee:owner/repo
fissue platforms doctor -r gitee:owner/repo --write --number IKM265   # 写探测，建完即删
fissue platforms doctor -r github:owner/repo --probe-push             # 推分支后删远端分支
```

回答「**这套令牌 + 这个仓库到底能做什么**」：只读 / 克隆 / 打标签 / 评论 / 推分支逐项探测，
建 PR **永不探测**（会留需人关闭的 PR）并显示为 `－` 加原因。

三态必须分开：`✅` 通过 / `❌` 失败 / `－` **没探测**——把「跳过」显示成通过等于把没验过的
说成验过了。写探测与推分支**自清**，以「痕迹实际还在不在」为准。退出码：有探测失败为 `1`
（跳过不算失败），可直接用于部署自检。

## 产物位置

```
data/
  artifacts/<key>/base/       # 验证器 base 阶段输出（stdout/stderr/meta）
  artifacts/<key>/fix/        # 修复阶段输出
  artifacts/<key>/agent/      # Agent 每轮输出
  manual/<key>/report.md      # 人工处理报告
  manual/<key>/attempt.patch
  patches/<key>.patch         # dry-run / patch_only 补丁
  exports/                    # report / export
  benchmarks/                 # bench 基线（进版本库）
```

---

相关：[快速开始](Getting-Started.md) · [配置参考](Configuration.md) · [使用场景](Workflows.md) ·
[故障排查](Troubleshooting.md) · [Web 与 REST API](Web-and-API.md)