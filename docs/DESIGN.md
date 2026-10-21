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
│  service/   daemon（扫描/评测/验证/flush/修复循环）· notify       │
├──────────────────────────────────────────────────────────────────┤
│ 能力层                                                            │
│  ai/        客户端 · 提示词 · 评估器                               │
│  verifier/  生成器 · F2P 执行器                                    │
│  sandbox/   转发组件 · Docker 运行时 · 线协议                       │
│  platforms/ GitHub · Gitee · AtomGit · GitLab 适配器               │
│  workspace  本地 git 工作副本                                      │
├──────────────────────────────────────────────────────────────────┤
│ 基础层                                                            │
│  config · models · store（SQLite / PostgreSQL + JSON/MD 导出）· errors · 日志 │
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
>
> **flush 之前会先回收「入队后未收尾」的残留**（`pending` 超龄）：那种行会让第 3 条兜底
> 永久失效，条目也卡在 `queued` 再也无人碰。批次本身用条件 UPDATE **原子抢占**，
> 同一批不会被两个写者（daemon / Web / CLI）各跑一次。

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
* **无测试框架** → 降级为 `checklist` 型，由 LLM 按证据逐条判定
  （有命令时用执行输出；无可执行命令时用仓库上下文 + 清单引用的文件内容
  作证据，修复后阶段再附上本次改动 diff）；force 修复的清单条目修完后由
  LLM 在修复工作区上重判（全部满足才提 PR，未满足项作为反馈返修）；
  也可直接用 `fissue verify` 人工复核。

### 4.1 既有测试回归门（pass-to-pass）

F2P 只表达**一个**方向：目标用例修复前失败、修复后通过。它表达不了
「别弄坏别的」——例如把 `split(" ")` 改成 `split()`，目标用例（空串返回 0）
修对了，却悄悄改掉了连续空格的既有语义。补上这个方向的唯一可靠判据是
**仓库自己的既有测试套件**（详见 [REGRESSION-GATE.md](REGRESSION-GATE.md)）。

```
确定性验证器：base 失败 + fix 通过   → f2p_satisfied
回归门（新增）：base 既有测试绿 + fix 仍绿 → regression_ok
```

两者**都**满足才允许自动修复 / 建议合并。回归门不新建工作区，只是在同一
工作区上多跑一条命令；成本随**运行时间**增长，与 Issue 数、LLM token 无关。

判定矩阵（`judge_regression`，`verifier/runner.py`）：

| base 既有测试 | fix 既有测试 | 结论 |
|---|---|---|
| 通过 | 通过 | ✅ 可继续 |
| 通过 | 失败 | ❌ `strict` 拒绝（warn 仅告警） |
| 失败 | — | ⚠️ 仓库本身不绿 → 本门不可信，放行 |
| ERROR/TIMEOUT | — | ⚠️ 同「不绿」，不可归咎于修复 |

> **第 3 行是安全底线**：老仓库普遍存在本来就失败/跳过的测试。若把
> 「base 不绿」当成修复的错，会把**所有**条目误杀。因此 `regression_gate`
> 只在「base 绿、fix 红」时才拦人，默认取 `strict`（`off | warn | strict` 三档，
> 配置项见 `config.example.yaml`）。

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

* `server` 模式下调用方**不持有 Docker 句柄**，只走受控的本地 socket 通道；
  `inline` 模式下 Docker 句柄就在调用方进程内。默认是 `inline`（没起
  `fissue sandbox serve` 时），所以这条保证**有条件**，别当无条件事实用。
* 容器以 `--network=none`、`--read-only`、`--cap-drop=ALL`、
  `--security-opt no-new-privileges`、非 root（1000:1000）运行。
* 限 CPU / 内存 / PID 数 / 执行时长。
* 只挂载**隔离工作区**（临时目录，可选 loopback 虚拟盘），绝不挂宿主普通目录。
  工作区以 rw 挂进容器，但其中的 **`.git` 是只读的**（`index` 除外，见 `workspace_mounts`）：
  它是宿主的执行入口（hooks、`filter.*`、`diff.*.textconv`、`credential.helper`）与伪造入口
  （`objects`/`refs`）。真容器验证过：`.git/config`、`.git/hooks`、`.git/refs`、`.git/objects`
  写操作一律 `Read-only file system`，而工作区、`.git/index` 与 `git status` 照常可用。
* Docker 不可用时**默认拒绝执行**（不再静默降级）；确需降级要显式打开
  `sandbox.allow_local_fallback`，降级后没有任何隔离，且环境变量按白名单透传。
* `sandbox.runtime` **只接受** `docker` / `local`：拼错（`Docker`、`podman`、带尾空格）在
  加载配置时就被拒，运行期另有一道 fail-closed——不会再出现「静默落到无隔离的本地执行」。
* 执行超时后**强制回收容器**（`docker rm -f`），不留后台容器继续吃资源；
  单流输出保留上限 2MB，超出丢弃并在 stderr 标注「已截断」。

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
| `.git` 加固 | 容器对工作区可写，而 `.git` 是**宿主的执行入口**（`hooks/`、`filter.*.clean` 在 `git add` 时执行、`diff.*.textconv` 在 `git diff` 时执行、`credential.helper` 更直接）。宿主 git 一律带 `-c core.hooksPath=<devnull>`；克隆后与**每次沙盒跑完**都重建 `.git/config`、删掉 `.git/hooks`；`push --no-verify` |
| 提交内容完整性 | 提交前把索引拉回 HEAD、确认仍在自己的分支、并核对「待提交的文件正好是 Agent 改过的那些」，多出来的**拒绝提交**（`git commit` 提交的是索引，而 `.git` 在容器可写范围内） |
| 令牌卫生 | 推送用**不带令牌**的 URL，令牌经子进程环境变量 + 凭据助手递给 git——不进命令行、不写入 git 配置；所有输出过 `sanitize()` |

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

存储默认 **SQLite**（零依赖；生产 / 多机可换 PostgreSQL），12 张表：

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

**增量**分两层口径：

* `items.content_hash`（标题 / 正文 / 标签 / **评论条数** / 状态 / merged）判「平台侧这条
  与库里不一样」——变了就重写字段与评论表；
* 但**只有实质字段**（`MATERIAL_FIELDS`：标题 / 正文 / 标签 / 状态 / merged）变化才让
  **已有结论作废**：还没进入执行的条目回落 `new` 重评；修复已开工的
  （`fix_queued` / `fixing` / `pr_created`）只把「结论过期」留痕（`stale_reasons`，导出与
  API 可见），由 `fissue requeue --stale` 人工放行。
  为什么不直接拿 `content_hash` 判过期：它含**评论条数**，那样每来一条评论都要重评测一次，
  活跃 issue 上等于持续烧 token。
* 打标签是**我们自己的写**，所以写成功后必须把标签并回本地快照（`Repository.merge_item_labels`，
  取并集）：否则下一次抓取必然读到「平台 ≠ 库里」，把刚做出的结论作废重评——与「评论条数」
  同一类自伤开销，只是触发者是自己的写操作（实测见 `BENCHMARK.md` §4.32）。

SQLite 是**默认**后端（本地开发与测试全程在用），已对 `with_for_update` 等方言差异做处理，
并开 **WAL** + 30s busy 超时（驱动默认只有 5s；回滚日志模式下长**读**事务会把写者的提交挡住）。
PostgreSQL 服务于生产 / 多机并发，但**未在真跑里测量过**（`BENCHMARK.md` §5），选它请自行评估。

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
