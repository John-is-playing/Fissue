# 架构设计

> Fissue 的分层、流水线、状态机、F2P / 回归门与安全模型。上手路径见 [USAGE.md](USAGE.md)，
> 回归门细节见 [REGRESSION-GATE.md](REGRESSION-GATE.md)，安全清单见 [SECURITY.md](../SECURITY.md)。

## 1. 分层

```
交付形态   CLI(typer) · REST API(FastAPI) · Web 仪表盘 · 常驻服务
编排层     pipeline/(fetcher·stages·queue·flush·context) · fixer/(agent·autofix) · service/(daemon·notify)
能力层     ai/(client·prompts·evaluator) · verifier/(generator·runner) · sandbox/(forwarder·runtime) ·
           platforms/(github·gitee·atomgit·gitlab) · workspace
基础层     config · models · store(SQLite/PG/MySQL + JSON/MD 导出) · errors · logging
```

**依赖方向严格单向**：上层依赖下层，下层不知道上层。`store` 与 `platforms` 互不依赖；
`ai` 不知道沙盒，`sandbox` 不知道 AI。

## 2. 四条流水线

| 入口 | 分类 | 链路 | 终点 |
|---|---|---|---|
| Issue | BUG | 评难度 → 写验证器 → 沙盒 base 验证 → verify 队列 → 批量定论 | tier1/tier2 → 自动修复提 PR；其余打标签 |
| Issue | FEATURE | 评可行性/难度/必要性 → 打标签 | 等开发者 |
| PR | BUG | 判提交者 → 找关联 Issue → 写验证器 → 沙盒合并验证 → fix_bug 队列 → 定论 | 打标签建议合并，人工合并 |
| PR | FEATURE | 同上，入 fix_feature 队列（共用沙盒） | 同上 |

### 状态机

```
new ──评测──► evaluated ──验证可复现──► queued ──flush 定论──► labeled
  │                                        │            ├─► fix_queued ─► fixing ─► pr_created
  │                                        │            └─► skipped / needs_manual
  └── 无法自动验证 ──► needs_manual
```

## 3. 队列与批量触发

三套队列：`verify`（Issue-BUG 验证）、`fix_bug`、`fix_feature`（PR 合并验证）。

flush 触发（取或）：**数量阈值**（已完成未定论 ≥ `flush_size`）· **空闲超时**（队首静默 ≥
`idle_flush_seconds`）· **队列排空**（无 pending/running，哪怕只有 1 条）· **手动**
（`fissue flush` / `POST /api/v1/queues/flush`）。

> 为什么需要空闲超时：常驻服务里「队列为空」往往只在真正空闲时出现，若有条目卡在 pending
> （如沙盒超时），只靠前两条可能永远等不到 flush。
>
> 为什么批量：同一批验证输出放同一上下文，模型能横向比较（识别成批重复提交），也更省钱。
>
> **flush 前先回收「入队后未收尾」的残留**（`pending` 超龄）：那种行会让排空兜底永久失效，
> 条目卡在 `queued` 无人碰。批次用条件 UPDATE **原子抢占**，同一批不会被两个写者各跑一次。

## 4. 验证器与 F2P

```
base：未修复代码上跑 → 必须失败（exit≠0）→ 问题真实存在
fix ：修复/合并后跑   → 必须通过（exit=0）→ 修复有效
```

两者同时成立才是 `f2p_satisfied`。防御性设计：

- **base 就通过** → 验证器不可靠 → 触发 LLM 修正（最多 `verifier.max_rounds` 轮），仍不行标
  `needs_manual`，绝不据此自动修码；
- **base ERROR/TIMEOUT** → 结论不可信，不进入自动修复；
- **无测试框架** → 降级为 `checklist`，LLM 按证据逐条判定（有命令用输出；无命令用仓库上下文
  + 清单引用文件，修复后再附 diff）。force 条目修完由 LLM 在修复工作区重判，全部满足才提 PR；
- **有测试框架的仓库不允许降级**：配了 `test_hint` 时生成端拒绝 `kind=checklist`，强制可执行
  验证器——清单是主观闸门，不能作自动提 PR 的唯一依据；
- **清单条目分级**：判定 JSON 的 `runtime_required` 标记「需真实外部服务」的条目，闸门只卡代码
  可验证项，运行时项只输出人工验证命令。

### 4.1 回归门（pass-to-pass）

F2P 只表达一个方向，表达不了「别弄坏别的」——例如把 `split(" ")` 改成 `split()`，目标用例
修对了，却改掉连续空格的既有语义。补上这个方向的唯一可靠判据是**仓库自己的测试套件**：

```
确定性验证器：base 失败 + fix 通过   → f2p_satisfied
回归门      ：base 既有测试绿 + fix 仍绿 → regression_ok
```

两者**都**满足才允许自动修复 / 建议合并。回归门不新建工作区，只在同一工作区多跑一条命令，
成本随**运行时间**增长，与 Issue 数、token 无关。

| base 既有测试 | fix 既有测试 | 结论 |
|---|---|---|
| 通过 | 通过 | ✅ 可继续 |
| 通过 | 失败 | ❌ `strict` 拒绝（`warn` 仅告警） |
| 失败 | — | ⚠️ 仓库本身不绿 → 本门不可信，放行 |
| ERROR/TIMEOUT | — | ⚠️ 同上 |

第 3 行是**安全底线**：老仓库普遍有本来就失败的测试，把「base 不绿」当成修复的错会把所有条目
误杀。默认 `strict` 因此只在「base 绿、fix 红」时拦人。详见 [REGRESSION-GATE.md](REGRESSION-GATE.md)。

## 5. 安全模型

### 5.1 沙盒隔离

```
调用方 ──ExecRequest──► ForwarderClient ──本地 socket──► ForwarderServer ──► DockerRuntime ──► 隔离容器
```

- `server` 模式调用方**不持有 Docker 句柄**；`inline` 模式下句柄就在调用方进程内，**而这是默认**
  （没起 `fissue sandbox serve` 时），所以这条保证是**有条件**的。
- 容器 `--network=none`、`--read-only`、`--cap-drop=ALL`、`no-new-privileges`、非 root（1000:1000），
  限 CPU / 内存 / PID / 时长。
- 只挂**隔离工作区**（临时目录，可选 loopback 虚拟盘）；工作区 rw，但 **`.git` 只读**（`index`
  除外）——它既是宿主的执行入口（hooks、`filter.*`、`diff.*.textconv`、`credential.helper`），
  也是伪造入口（`objects`/`refs`）。
- Docker 不可用时**默认拒绝执行**，要降级须显式 `sandbox.allow_local_fallback`。
- `sandbox.runtime` **只接受** `docker` / `local`，拼错在加载配置时被拒，运行期另有一道
  fail-closed，不会再静默落到无隔离的本地执行。
- 超时后**强制回收容器**（`docker rm -f`）；单流输出上限 2MB，超出丢弃并在 stderr 标注。

### 5.2 自动修复护栏（模型说了不算）

| 护栏 | 说明 |
|---|---|
| 路径白名单 | 只允许仓库内相对路径；`..`、绝对路径拒绝 |
| 保护路径 | `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` 等拒绝写入 |
| 验证器锁定 | 修复期间验证器文件不可写（防「改测试骗过测试」） |
| 规模上限 | 变更文件数 ≤ `max_changed_files`，diff 行数 ≤ `max_diff_lines`，超出回滚 |
| F2P 复核 | 提 PR 前重跑完整 F2P，不接受 Agent 自述 |
| 策略闸门 | 仅 Issue、仅 BUG、仅 tier1/tier2，排除疑似刷量/重复 |
| `.git` 加固 | 宿主 git 带 `-c core.hooksPath=<devnull>`；克隆后与每次沙盒跑完重建 `.git/config`、删 `.git/hooks`；`push --no-verify` |
| 提交完整性 | 提交前把索引拉回 HEAD、确认在自己分支、核对「待提交文件正好是 Agent 改过的那些」，多出来的拒绝提交 |
| 令牌卫生 | 推送 URL 不带令牌，令牌经子进程环境变量 + 凭据助手递给 git；输出过 `sanitize()` |

### 5.3 人工闸门

PR **永不自动合并**，只打标签给建议（`approve-merge` 也只是标签 + 评论）；修复失败生成
`data/manual/<key>/report.md`；`--dry-run` 只产 patch。

## 6. AI 层

- 并发由 `llm.concurrency` 信号量限制；每次调用写 `llm_usage`（purpose / model / tokens / cost）。
- `BudgetGuard` 调用前查当日累计，超 `daily_tokens_per_repo` 或 `daily_usd_per_repo` 时按
  `hard_stop` 抛异常或告警；主模型失败自动降级 `fallback_model`。
- 提示词集中在 `ai/prompts.py`：只依据给定材料**不得臆造**（信息不足降 `confidence`）；每维
  必须给 `reason` 并尽量引用原文；输出必须合法 JSON（`extract_json` 直解 → 去围栏 → 括号平衡）；
  内容注入统一经 `render_item()` 截断（正文 6k 字符、评论 15 条、diff 20k 字符）。
- **关键词预筛**：一边倒时直接定论、不调 LLM。中文用子串匹配，ASCII 用
  `(?<![a-z0-9_])kw(?![a-z0-9_])` 避免 `request` 命中 `requests.get`。

## 7. 存储

12 张表：`repos`（+增量游标）· `items`（含 `content_hash`）· `comments` · `evaluations`（取最新）·
`verifiers` / `verifier_runs` · `queue_entries` · `batch_conclusions` · `fix_attempts` · `llm_usage` ·
`scan_runs` · `notifications`（`dedup_key` 唯一）。

**增量分两层口径**：

- `content_hash`（标题/正文/标签/**评论条数**/状态/merged）判「平台侧与库里不一样」，变了就重写；
- 但只有**实质字段**（`MATERIAL_FIELDS`：标题/正文/标签/状态/merged）变化才让**已有结论作废**：
  未进入执行的回落 `new` 重评；修复已开工的（`fix_queued`/`fixing`/`pr_created`）只留痕
  （`stale_reasons`，导出与 API 可见），由 `fissue requeue --stale` 人工放行。
  不拿 `content_hash` 判过期是因为它含评论数——那样每条新评论都要重评，等于持续烧 token。
- 打标签是**自己的写**，成功后必须把标签并回本地快照（`merge_item_labels` 取并集），否则下次
  抓取必然判「平台 ≠ 库里」把刚做出的结论作废重评。

SQLite 是默认后端，已对 `with_for_update` 等方言差异做处理，并开 **WAL** + 30s busy 超时
（驱动默认 5s，回滚日志模式下长读事务会挡住写者提交）。PostgreSQL 服务于生产/多机，已完成真实
部署验证；MySQL 8.0+ 跑通了全链路——两者验证的是**部署与并发路径**，`BENCHMARK.md` 的跑分数字
仍全部来自 SQLite。

## 8. 扩展点

| 想加什么 | 怎么做 |
|---|---|
| 平台适配器 | 继承 `PlatformAdapter`，实现 `_headers` / `fetch_items` / `fetch_diff` / `item_url`，注册进 `platforms/registry.py:ADAPTERS`；v5 风格直接继承 `V5Adapter` |
| 通知渠道 | `service/notify.py` 实现 `Channel.send`，注册进 `CHANNELS`，在 `notify.channels` 写 `type` |
| 换 AI 供应商 | 改 `llm.base_url` + `llm.model`（OpenAI 兼容 `/chat/completions`）；不支持 `response_format` 的端点自动降级为文本解析 |
| 调整修复策略 | `fix_policy`（准入阈值）+ `auto_fix`（护栏、提 PR 方式、失败处置） |

分档由 `fix_policy.tier_strategy` 决定：`importance`（默认，绝对门槛）、`dual`（两维取严）、
`custom`（用户函数）、`percentile`（批内分位）。分位策略让**容量占比**稳定，但**不**消除近分条目
的成员翻转——夹具逐条期望因此只在 `importance` 下可比。

---

相关：[USAGE.md](USAGE.md) · [REGRESSION-GATE.md](REGRESSION-GATE.md) · [AUTH.md](AUTH.md) ·
[wiki/Architecture.md](wiki/Architecture.md) · [BENCHMARK.md](../BENCHMARK.md)