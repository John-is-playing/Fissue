# 架构设计

> 本页是 [docs/DESIGN.md](../DESIGN.md) 的**导读与摘要**：分层、数据流、状态机、依赖方向、扩展点。
> 需要完整细节时请直接读 DESIGN.md（例如安全模型的逐条论证）。

---

## 1. 分层

```
┌──────────────────────────────────────────────────────────────────┐
│ 交付形态层                                                        │
│  CLI (typer)   │  REST API (FastAPI)  │  Web 仪表盘  │  常驻服务   │
├──────────────────────────────────────────────────────────────────┤
│ 编排层                                                            │
│  pipeline/  fetcher · stages · queue · flush · context            │
│  fixer/     agent（受控循环）· autofix（提 PR 编排）· reviewer     │
│  service/   daemon（扫描/评测/验证/flush/修复循环）· notify        │
├──────────────────────────────────────────────────────────────────┤
│ 能力层                                                            │
│  ai/        客户端 · 提示词 · 评估器                              │
│  verifier/  生成器 · F2P 执行器 · 回归门                           │
│  sandbox/   转发组件 · Docker 运行时 · 线协议                       │
│  platforms/ GitHub · Gitee · AtomGit · GitLab 适配器               │
│  workspace  本地 git 工作副本                                     │
├──────────────────────────────────────────────────────────────────┤
│ 基础层                                                            │
│  config · models · store（SQLite / PostgreSQL + JSON/MD 导出）· errors · 日志 │
└──────────────────────────────────────────────────────────────────┘
```

**依赖方向严格单向**：上层依赖下层，下层不知道上层的存在。
`store` 与 `platforms` 互不依赖；`ai` 不知道沙盒；`sandbox` 不知道 AI。

### 源码目录

```
src/fissue/
  config.py       配置加载（.env + config.yaml + 覆盖层）
  configio.py     配置写回（覆盖层）
  models.py       领域模型
  lockfile.py     跨进程写锁（flock）
  net.py          HTTP 客户端与 TLS / 限流处理
  workspace.py    本地 git 工作副本
  store/          SQLite / PostgreSQL 持久化 + JSON/MD 导出
  platforms/      GitHub / Gitee / AtomGit / GitLab 适配器
  ai/             OpenAI 兼容客户端、预算、提示词、评估器
  verifier/       验证器生成与 F2P / 回归门执行
  sandbox/        Docker 沙盒与转发组件
  pipeline/       流水线与队列
  fixer/          自动修复 Agent 循环与提 PR
  service/        常驻调度与通知
  auth/           会话 / 口令 / TOTP / API Key 内核（纯标准库）
  web/            REST API 与仪表盘
  cli/            命令行入口
```

---

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

---

## 3. 队列与批量触发

三套队列：`verify`（Issue-BUG 验证）、`fix_bug`、`fix_feature`（PR 合并验证，Bug/Feature 各一套，共用沙盒）。

flush 触发条件（四者取或）：

1. **数量阈值**：已完成未定论条数 ≥ `flush_size`。
2. **空闲超时**：队首完成条目静默 ≥ `idle_flush_seconds`。
3. **队列排空**：无 pending/running 条目（哪怕只有 1 条）。
4. **手动**：`fissue flush` / `POST /api/v1/queues/flush`。

**为什么需要空闲超时**：常驻服务里「队列为空」往往只在真正空闲时出现，若某个条目卡在 pending
（例如沙盒超时），靠前两条可能永远等不到 flush。

**flush 之前会先回收「入队后未收尾」的残留**（`pending` 超龄）：那种行会让第 3 条兜底永久失效，
条目也卡在 `queued` 再也无人碰。批次本身用条件 UPDATE **原子抢占**，同一批不会被两个写者各跑一次。

---

## 4. 验证：F2P 与回归门

判定需要**两个独立方向**都成立：

```
确定性验证器（F2P）  ：base 失败 + fix 通过       → f2p_satisfied
回归门（pass-to-pass）：base 既有测试绿 + fix 仍绿 → regression_ok
```

两者**都**满足才允许自动修复 / 建议合并。完整判定矩阵与三档模式见[验证器与 F2P](Verifier-and-F2P.md)。

---

## 5. 安全模型（摘要）

信任边界画在**沙盒**上。以下一律视为不可信：Issue/PR 正文与评论、外部 PR 补丁、被测仓库自身代码、
AI 模型的输出。宿主侧**永不信任模型的自述**，包括「我已经验证通过了」。

沙盒默认：`--network none`、`--read-only`、`--cap-drop=ALL`、`no-new-privileges`、非 root、
限 CPU/内存/PID/时长；只挂隔离工作区，其中 **`.git` 只读**（`index` 除外）。
Docker 不可用时**默认拒绝执行**（不再静默降级）。

**自动修复的宿主侧护栏**（模型说了不算）：

| 护栏 | 说明 |
|---|---|
| 路径白名单 | 只允许仓库内相对路径；`..`、绝对路径直接拒绝 |
| 保护路径 | `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` 等拒绝写入 |
| 验证器锁定 | 验证器文件在修复期间不可写（防「改测试骗过测试」） |
| 规模上限 | 变更文件数 ≤ `max_changed_files`，diff 行数 ≤ `max_diff_lines`，超出即回滚 |
| 语法自检 | Python 文件落盘前 `compile()`，不合法就拒绝该文件 |
| F2P 复核 | 提 PR 前再跑一次完整 F2P，不接受 Agent 自说自话 |
| 策略闸门 | 仅 Issue、仅 BUG、仅 tier1/tier2、排除疑似刷量 / 重复 |
| `.git` 加固 | 宿主 git 一律带 `-c core.hooksPath=<devnull>`；克隆后与每次沙盒跑完都重建 `.git/config`、删掉 `.git/hooks`；`push --no-verify` |
| 提交内容完整性 | 提交前把索引拉回 HEAD、确认仍在自己的分支、核对「待提交文件正好是 Agent 改过的那些」，多出来的拒绝提交 |
| 令牌卫生 | 推送用**不带令牌**的 URL，令牌经子进程环境变量 + 凭据助手递给 git——不进命令行、不写进 git 配置；输出过 `sanitize()` |
| 写操作互斥 | 跨进程 `flock`、同进程 `WRITE_GATE`；Web 写端点拿不到回 `409` |

**人工闸门**：PR **永不自动合并**，只打标签给建议；修复失败生成 `data/manual/<key>/report.md`。

细节与逐条论证见[安全模型](Security.md) 与 [DESIGN.md §5](../DESIGN.md#5-安全模型)。

---

## 6. 存储

存储默认 SQLite（零依赖），生产 / 多机可换 PostgreSQL。表的清单与增量口径见[数据模型](Data-Model.md)。

---

## 7. 扩展点速查

| 要做什么 | 入口 |
|---|---|
| 加一个平台 | 继承 `PlatformAdapter`，实现 `_headers / fetch_items / fetch_diff / item_url`，注册进 `platforms/registry.py:ADAPTERS`；v5 风格平台可直接继承 `V5Adapter` |
| 加一个通知渠道 | 在 `service/notify.py` 实现 `Channel.send`，注册进 `CHANNELS`，再在 `config.yaml` 的 `notify.channels` 里写 `type` |
| 换 AI 供应商 | 改 `llm.base_url` + `llm.model`（协议为 OpenAI 兼容的 `/chat/completions`） |
| 调整自动修复策略 | `config.yaml` 的 `fix_policy`（准入阈值）与 `auto_fix`（护栏、提 PR 方式、失败处置） |
| 自定义分档函数 | `fix_policy.tier_strategy: custom` + `custom_tier: "pkg.mod:func"` |

完整步骤见[扩展指南](Extending.md)。

---

## 相关页

[项目概览](Overview.md) · [验证器与 F2P](Verifier-and-F2P.md) · [AI 层](AI-Layer.md) · [数据模型](Data-Model.md) · [扩展指南](Extending.md) · [安全模型](Security.md)
