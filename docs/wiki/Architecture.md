# 架构设计

> [docs/DESIGN.md](../DESIGN.md) 的导读与摘要。需要逐条论证（尤其安全模型）时读原文。

## 1. 分层

```
交付形态   CLI(typer) · REST API(FastAPI) · Web 仪表盘 · 常驻服务
编排层     pipeline/(fetcher·stages·queue·flush·context) ·
           fixer/(agent·autofix·reviewer) · service/(daemon·notify)
能力层     ai/ · verifier/(生成器·F2P执行器·回归门) · sandbox/(转发组件·Docker运行时·线协议) ·
           platforms/(四平台适配器) · workspace
基础层     config · configio · models · store · errors · logging · lockfile · net
鉴权内核   auth/（会话 / 口令 / TOTP / API Key，纯标准库）
```

**依赖方向严格单向**：上层依赖下层，下层不知道上层。`store` 与 `platforms` 互不依赖；
`ai` 不知道沙盒，`sandbox` 不知道 AI。

```
src/fissue/  config.py configio.py models.py lockfile.py net.py workspace.py
             store/ platforms/ ai/ verifier/ sandbox/ pipeline/ fixer/ service/ auth/ web/ cli/
```

## 2. 四条流水线

| 入口 | 分类 | 链路 | 终点 |
|---|---|---|---|
| Issue | BUG | 评难度 → 写验证器 → 沙盒 base 验证 → verify 队列 → 批量定论 | tier1/tier2 → 自动修复提 PR；其余打标签 |
| Issue | FEATURE | 评可行性/难度/必要性 → 打标签 | 等开发者 |
| PR | BUG | 判提交者 → 找关联 Issue → 写验证器 → 沙盒合并验证 → fix_bug 队列 → 定论 | 打标签建议合并，人工合并 |
| PR | FEATURE | 同上，入 fix_feature 队列（共用沙盒） | 同上 |

```
new ──评测──► evaluated ──验证可复现──► queued ──flush 定论──► labeled
  │                                        │            ├─► fix_queued ─► fixing ─► pr_created
  │                                        │            └─► skipped / needs_manual
  └── 无法自动验证 ──► needs_manual
```

## 3. 队列与批量

三套队列：`verify` · `fix_bug` · `fix_feature`。flush 四触发（取或）：`flush_size` 阈值 ·
`idle_flush_seconds` 空闲超时 · 队列排空 · 手动 `fissue flush` / `POST /api/v1/queues/flush`。

**为什么需要空闲超时**：常驻服务里「队列为空」往往只在真正空闲时出现，若某条目卡在 pending
（如沙盒超时），只靠前两条可能永远等不到 flush。

flush 前先回收「入队后未收尾」的 `pending` 超龄残留（它会让排空兜底永久失效，条目卡在
`queued` 无人碰）；批次用条件 UPDATE **原子抢占**，不会被两个写者各跑一次。

## 4. 验证：两个独立方向

```
F2P（确定性验证器）：base 失败 + fix 通过      → f2p_satisfied
回归门（pass-to-pass）：base 既有测试绿 + fix 仍绿 → regression_ok
```

两者**都**成立才允许自动修复 / 建议合并。矩阵与三档模式见[验证器与 F2P](Verifier-and-F2P.md)。

## 5. 安全模型（摘要）

信任边界画在**沙盒**上：Issue/PR 正文与评论、外部 PR 补丁、被测仓库自身代码、**AI 模型的输出**
一律不可信；宿主侧永不信任模型的自述。

沙盒默认 `--network none`、`--read-only`、`--cap-drop=ALL`、`no-new-privileges`、非 root、
限 CPU/内存/PID/时长；只挂隔离工作区，其中 **`.git` 只读**（`index` 除外）；Docker 不可用时
**默认拒绝执行**。

宿主侧护栏：路径白名单 · 保护路径 · 验证器锁定 · 规模上限 · 语法自检 · 提 PR 前重跑 F2P ·
策略闸门 · `.git` 加固 · 提交内容完整性 · 令牌卫生 · 写操作互斥。PR **永不自动合并**。

细节见[安全模型](Security.md) 与 [DESIGN.md §5](../DESIGN.md)。

## 6. 存储与扩展

默认 SQLite（WAL + 30s busy 超时），生产 / 多机可换 PostgreSQL 或 MySQL 8.0+。表清单与增量
口径见[数据模型](Data-Model.md)。

| 要做什么 | 入口 |
|---|---|
| 加平台 | 继承 `PlatformAdapter`（`_headers / fetch_items / fetch_diff / item_url`），注册进 `platforms/registry.py:ADAPTERS`；v5 风格继承 `V5Adapter` |
| 加通知渠道 | `service/notify.py` 实现 `Channel.send`，注册进 `CHANNELS`，`notify.channels` 写 `type` |
| 换 AI 供应商 | 改 `llm.base_url` + `llm.model`（OpenAI 兼容 `/chat/completions`） |
| 调修复策略 | `fix_policy`（准入）+ `auto_fix`（护栏、提 PR 方式、失败处置） |
| 自定义分档 | `fix_policy.tier_strategy: custom` + `custom_tier: "pkg.mod:func"` |

---

相关：[项目概览](Overview.md) · [验证器与 F2P](Verifier-and-F2P.md) · [AI 层](AI-Layer.md) ·
[数据模型](Data-Model.md) · [扩展指南](Extending.md) · [安全模型](Security.md)