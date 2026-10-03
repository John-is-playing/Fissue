# 架构设计

> 本文说明 Fissue 的分层、数据流、状态机与安全模型。想快速上手请看 README。

## 1. 分层

```
┌──────────────────────────────────────────────────────────────────┐
│ 交付形态层                                                        │
│  CLI (typer)   │  REST API (FastAPI)  │  Web 仪表盘  │  常驻服务   │
├──────────────────────────────────────────────────────────────────┤
│ 编排层                                                            │
│  pipeline/  fetcher · stages · queue · flush · context            │
│  fixer/     agent（受控循环）· autofix（提 PR 编排）               │
│  service/   daemon（扫描/评测/flush/修复循环）· notify            │
├──────────────────────────────────────────────────────────────────┤
│ 能力层                                                            │
│  ai/        客户端 · 提示词 · 评估器                               │
│  verifier/  生成器 · F2P 执行器                                    │
│  sandbox/   转发组件 · Docker 运行时 · 线协议                       │
│  platforms/ GitHub · Gitee · AtomGit · GitLab 适配器               │
│  workspace  本地 git 工作副本                                      │
├──────────────────────────────────────────────────────────────────┤
│ 基础层                                                            │
│  config · models · store（PostgreSQL + JSON/MD 导出）· errors · 日志 │
└──────────────────────────────────────────────────────────────────┘
```

**依赖方向严格单向**：上层依赖下层，下层不知道上层的存在。
`store` 与 `platforms` 互不依赖；`ai` 不知道沙盒；`sandbox` 不知道 AI。

## 2. 四条流水线

用户定义的规则（Q1）映射为四条互不干扰的路径：

| 入口 | 分类 | 处理链路 | 终点 |
|---|---|---|---|
| Issue | BUG | 评测难度 → 生成验证器 → 沙盒 base 验证 → verify 队列 → 批量定论 | tier1/tier2 → 自动修复提 PR；其余打标签等开发者 |
| Issue | FEATURE | 评测可行性/难度/必要性 → 打标签 | 等开发者 |
| PR | BUG | 判提交者 → 找关联 Issue → 生成验证器 → 沙盒合并并验证 → fix_bug 队列 → 批量定论 | 打标签**建议**合并，等开发者手动合并 |
| PR | FEATURE | 同上，入 fix_feature 队列（共用沙盒） | 同上 |

### 状态机（status）

```
new ──评测──► evaluated ──验证可复现──► queued ──flush 定论──► labeled
  │                                        │                     │
  │                                        │            ┌────────┴────────┐
  │                                        │            ▼                 ▼
  │                                        │        fix_queued        skipped / needs_manual
  │                                        │            │
  │                                        │            ▼
  │                                        │         fixing ──► pr_created
  │                                        │            │
  │                                        └────────────┴──► needs_manual / failed
  └── 无法自动化验证 ──► needs_manual
```

## 3. 队列与批量触发（Q9）

三套队列：`verify`（Issue-BUG 验证）、`fix_bug`、`fix_feature`（PR 合并验证，
Bug/Feature 各一套，共用沙盒）。

flush 触发条件（三者取或）：

1. **数量阈值**：已完成未定论条数 ≥ `flush_size`。
2. **空闲超时**：队首完成条目静默 ≥ `idle_flush_seconds`。
3. **队列排空**：无 pending/running 条目（哪怕只有 1 条）。
4. **手动**：`fissue flush` / `POST /api/v1/queues/flush`。

> 为什么需要空闲超时？常驻服务里「队列为空」往往只在真正空闲时出现，
> 若某个条目卡在 pending（例如沙盒超时），靠前两条可能永远等不到 flush。
>
> 为什么批量而不是逐条？同一批条目的验证输出放在同一上下文里，
> 模型能横向比较（例如识别出成批的重复提交），也更省钱。

## 4. 验证器与 F2P（Q7）

**F2P = fail-to-pass**：

```
base 阶段：在未修复代码上跑 → 必须失败（exit≠0）→ 证明问题真实存在
fix  阶段：在修复/合并后代码上跑 → 必须通过（exit=0）→ 证明修复有效
```

只有两者同时成立才算 `f2p_satisfied`。

防御性设计：

* **base 就通过** → 判定「验证器不可靠」，触发 LLM 修正（最多 `verifier.max_rounds` 轮）；
  仍不可靠则标记 `needs_manual`，绝不据此自动修码。
* **base 直接 ERROR/TIMEOUT** → 结论不可信，不进入自动修复。
* **无测试框架** → 降级为 `checklist` 型，由 LLM 逐条判读执行输出；
  也可直接用 `fissue verify` 人工复核。

## 5. 安全模型

### 5.1 沙盒隔离（Q8）

```
调用方 ──ExecRequest──► ForwarderClient ──本地 socket──► ForwarderServer
                                                              │
                                                              ▼
                                                        DockerRuntime
                                                              │
                                                              ▼
                                                     隔离容器
```

* 调用方**不持有 Docker 句柄**，只走受控的本地 socket 通道。
* 容器以 `--network=none`、`--read-only`、`--cap-drop=ALL`、
  `--security-opt no-new-privileges`、非 root（1000:1000）运行。
* 限 CPU / 内存 / PID 数 / 执行时长。
* 只挂载**隔离工作区**（临时目录，可选 loopback 虚拟盘），绝不挂宿主普通目录。
* Docker 不可用时降级为本地子进程并**显式告警**——没有隔离，只应本地开发用。

### 5.2 自动修复的护栏

模型说了不算，宿主侧强制执行：

| 护栏 | 说明 |
|---|---|
| 路径白名单 | 只允许仓库内相对路径；`..`、绝对路径直接拒绝 |
| 保护路径 | `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` 等拒绝写入 |
| 验证器锁定 | 验证器文件在修复期间不可写（防「改测试骗过测试」） |
| 规模上限 | 变更文件数 ≤ `max_changed_files`，diff 行数 ≤ `max_diff_lines`，超出即回滚 |
| F2P 复核 | 提 PR 前再跑一次完整 F2P，不接受 Agent 自说自话 |
| 策略闸门 | 仅 Issue、仅 BUG、仅 tier1/tier2、排除疑似刷量/重复 |
| 令牌卫生 | 推送用一次性内嵌令牌 URL，不写入 git 配置；所有输出过 `sanitize()` |

### 5.3 人工闸门

* PR：**永不自动合并**，只打标签给建议；`approve-merge` 也只是打标签 + 评论。
* 修复失败：生成 `data/manual/<key>/report.md`（含动作轨迹、被拒写入、最后一次验证输出）。
* `--dry-run`：只产出 patch，不建分支不提 PR。

## 6. AI 层

### 预算与并发

* 并发由 `llm.concurrency` 的信号量限制。
* 每次调用写入 `llm_usage` 表（purpose / model / tokens / cost）。
* `BudgetGuard` 在每次调用前查当日累计用量，超 `daily_tokens_per_repo` 或
  `daily_usd_per_repo` 时按 `hard_stop` 抛异常或仅告警。
* 主模型失败自动降级到 `fallback_model`。

### 提示词设计

集中在 `ai/prompts.py`，共同约定：

1. 只依据给定材料，**不得臆造**；信息不足时降低 `confidence`。
2. 每维评分必须给 `reason`，尽量在 `evidence` 里引用原文。
3. 输出必须是合法 JSON；`ai/client.extract_json` 做鲁棒解析
   （直解 → 去 ```` ```json ```` 围栏 → 括号平衡截取）。
4. 内容注入统一经 `render_item()` 截断（正文 6k 字符、评论 15 条、diff 20k 字符），
   防止超长 Issue 烧掉预算。

### 关键词预筛

分类先用词边界匹配的关键词预筛，一边倒时直接定论、不调 LLM。
中文关键词用子串匹配；ASCII 关键词用 `(?<![a-z0-9_])kw(?![a-z0-9_])`
避免 `request` 误命中 `requests.get`。

## 7. 存储

PostgreSQL 为主库，11 张表：

| 表 | 作用 |
|---|---|
| `repos` | 登记的仓库 + 增量游标 |
| `items` | Issue/PR 主表（含内容指纹 `content_hash`） |
| `comments` | 评论 |
| `evaluations` | 评测历史（取最新为当前结论） |
| `verifiers` / `verifier_runs` | 验证器定义与每次执行结果 |
| `queue_entries` | 队列条目 |
| `batch_conclusions` | 批量 flush 的结论 |
| `fix_attempts` | 自动修复尝试与 PR 产物 |
| `llm_usage` | token/成本用量（预算来源） |
| `scan_runs` | 扫描记录 |
| `notifications` | 通知去重（`dedup_key` 唯一） |

**增量**靠 `items.content_hash`：标题/正文/标签数量/评论数/状态任一变化即视为
内容变更，需要重新评测；否则跳过（省钱）。

SQLite 亦可运行（本地开发与测试），代码中已对 `with_for_update` 等方言差异做处理。

## 8. 扩展指南

**加一个平台**：继承 `PlatformAdapter`，实现 `_headers` / `fetch_items` /
`fetch_diff` / `item_url`，再注册进 `platforms/registry.py:ADAPTERS`。
若目标平台是 v5 风格（`/repos/:owner/:repo/...`），直接继承 `V5Adapter` 即可。

**加一个通知渠道**：在 `service/notify.py` 实现 `Channel.send`，
注册进 `CHANNELS`，然后在 `config.yaml` 的 `notify.channels` 里写 `type`。

**换 AI 供应商**：改 `llm.base_url` + `llm.model` 即可，协议为 OpenAI 兼容的
`/chat/completions`。不支持 `response_format` 的端点会被自动识别并降级为文本解析。

**调整自动修复策略**：`config.yaml` 的 `fix_policy`（准入阈值）与
`auto_fix`（护栏、提 PR 方式、失败处置）。
