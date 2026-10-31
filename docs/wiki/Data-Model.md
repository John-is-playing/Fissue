# 数据模型

> 默认 **SQLite**（零依赖），生产 / 多机可换 PostgreSQL 或 MySQL 8.0+；可导出 JSON / Markdown。
> 键列取 640 字符：MySQL + utf8mb4 索引上限 3072 字节，真正超标的是队列表复合唯一索引
> （queue + item_key + status，767 时 3196 字节）；640 既满足索引，也覆盖 `items.key` 最坏长度
> （`platform:owner/name#number` = 584）。详见 [DESIGN.md §7](../DESIGN.md)。

## 1. 表清单

业务表 12 张：

| 表 | 作用 |
|---|---|
| `repos` | 登记的仓库 + 增量游标 |
| `items` | Issue/PR 主表（含 `content_hash`） |
| `comments` | 评论 |
| `evaluations` | 评测历史（**取最新为当前结论**） |
| `verifiers` / `verifier_runs` | 验证器定义 / 每次执行（含 base、fix、regression 各 stage） |
| `queue_entries` | 队列条目 |
| `batch_conclusions` | 批量 flush 结论 |
| `fix_attempts` | 修复尝试与 PR 产物 |
| `llm_usage` | token / 成本（**预算来源**） |
| `scan_runs` | 扫描记录 |
| `notifications` | 通知去重（`dedup_key` 唯一） |

鉴权表 3 张（`create_all` 直接建，**无需迁移脚本**）：

| 表 | 字段要点 |
|---|---|
| `auth_users` | `username`(unique) · `password_hash` · `totp_secret` · `totp_enabled` · `recovery_code_hashes`(JSON，一次性)。单管理员落地，字段按多用户设计 |
| `sessions` | `id`(32B 随机) · `user_id` · `csrf_token` · `expires_at` · `last_seen_at` · `user_agent` · `peer_ip` |
| `api_keys` | `name` · `prefix`(unique，公开 id) · `key_hash` · `last_used_at` · `revoked_at` |

口令哈希 `pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>`（`iters=600000`）；签名密钥在
`data/auth-secret.key`（0600），**不进** config.yaml / .env。

## 2. 增量的两层口径

1. **`content_hash`**（标题/正文/标签/**评论条数**/状态/merged）判「平台侧与库里不一样」，
   变了就重写字段与评论表。
2. **只有实质字段**（`MATERIAL_FIELDS`：标题/正文/标签/状态/merged）变化才让**已有结论作废**：
   未进入执行的回落 `new` 重评；修复已开工的（`fix_queued`/`fixing`/`pr_created`）只留痕
   （`stale_reasons`，导出与 API 可见），由 `fissue requeue --stale` 人工放行。

**为什么不拿 `content_hash` 判过期**：它含评论条数，那样每来一条评论都要重评，活跃 issue 上等于
持续烧 token。

## 3. 自己写的标签不能反过来作废自己的结论

打标签是**我们自己的写**，成功后必须把标签并回本地快照（`merge_item_labels`，取并集）：

> 否则下次抓取必然读到「平台 ≠ 库里」，把刚做出的结论作废重评——与「评论条数」同一类自伤开销，
> 只是触发者是自己的写操作。实测见 [BENCHMARK.md §4.32](../../BENCHMARK.md)。

## 4. 后端差异

**SQLite**（默认）：已处理 `with_for_update` 等方言差异，开 **WAL** + **30s busy 超时**
（驱动默认 5s；回滚日志模式下长读事务会挡住写者提交）。

```bash
FISSUE_DATABASE_URL=sqlite:///./data/fissue.db     # 默认值，通常什么都不用写
```

**PostgreSQL**：`1.1.0-b4` 起做过真实部署验证（`db init` / `db check` / 全量单测 / 服务与页面
实测），远端 PG 上看板切页 4.8s → 2ms。

**MySQL 8.0+**：在 Docker `mysql:8.0` 上跑通 `db init → fetch → eval → verify → flush → fix`
全链路（`tests/test_store_mysql.py` 按 `FISSUE_TEST_MYSQL_URL` 启用）。

> 两者验证的都是**部署与并发路径**，[基准与可信度](Benchmark.md) 里各夹具的跑分数字仍全部来自
> SQLite，别混读。

## 5. 导出

`fissue report` / `export` 支持 JSON 与 Markdown，输出到 `export.output_dir`（默认 `./data/exports`）。
`bench` 基线落在 `data/benchmarks/`，是**唯一进版本库的 `data/` 子目录**。

---

相关：[架构设计](Architecture.md) · [AI 层](AI-Layer.md) · [Web 与 REST API](Web-and-API.md) ·
[安全模型](Security.md) · [AUTH.md](../AUTH.md)