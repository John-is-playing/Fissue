# 数据模型

> 存储层：默认 **SQLite**（零依赖），生产 / 多机可换 PostgreSQL 或 MySQL 8.0+；支持导出 JSON / Markdown。
> 键列长度取 511 字符（不是更大的 767）：MySQL + utf8mb4 的索引上限是 3072 字节，
> `uq_item_identity` 是四列复合唯一索引，767 字符单列就 3068 字节、复合必然建不上。
> 完整细节见 [docs/DESIGN.md §7](../DESIGN.md#7-存储)。

---

## 1. 表清单

业务表 12 张：

| 表 | 作用 |
|---|---|
| `repos` | 登记的仓库 + 增量游标 |
| `items` | Issue/PR 主表（含内容指纹 `content_hash`） |
| `comments` | 评论 |
| `evaluations` | 评测历史（**取最新为当前结论**） |
| `verifiers` | 验证器定义 |
| `verifier_runs` | 每次验证执行结果（含 base / fix / regression 各 stage） |
| `queue_entries` | 队列条目 |
| `batch_conclusions` | 批量 flush 的结论 |
| `fix_attempts` | 自动修复尝试与 PR 产物 |
| `llm_usage` | token / 成本用量（**预算来源**） |
| `scan_runs` | 扫描记录 |
| `notifications` | 通知去重（`dedup_key` 唯一） |

鉴权表 3 张（会话鉴权落地时新增，均由 `create_all` 直接建，**无需迁移脚本**）：

| 表 | 字段要点 |
|---|---|
| `auth_users` | `id` · `username`(unique) · `password_hash` · `totp_secret`(可空) · `totp_enabled` · `recovery_code_hashes`(JSON，一次性) · `created_at` · `updated_at`。先支持单管理员，字段按多用户设计 |
| `sessions` | `id`(pk，随机 32B) · `user_id` · `csrf_token` · `created_at` · `expires_at` · `last_seen_at` · `user_agent`(截断) · `peer_ip`(截断) |
| `api_keys` | `id` · `name` · `prefix`(unique，公开 id) · `key_hash` · `created_at` · `last_used_at`(可空) · `revoked_at`(可空) |

口令哈希格式：`pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>`，`iters=600000`。
服务器签名密钥存在 `data/auth-secret.key`（`0600`），**不进** `config.yaml` / `.env`，用于会话 cookie 签名（HMAC）。

> 表由 `create_all` 建：**新表直接建，不需要迁移脚本**（这也是项目不依赖 alembic 的原因）。

---

## 2. 增量的两层口径（最容易误解）

增量分两层，**必须分开读**：

1. **`items.content_hash`**（标题 / 正文 / 标签 / **评论条数** / 状态 / merged）判的是
   「**平台侧这条与库里不一样**」——变了就重写字段与评论表。
2. **但只有实质字段**（`MATERIAL_FIELDS`：标题 / 正文 / 标签 / 状态 / merged）变化才让
   **已有结论作废**：
   - 还没进入执行的条目 → 回落 `new` 重评；
   - **修复已开工的**（`fix_queued` / `fixing` / `pr_created`）→ 只把「结论过期」**留痕**
     （`stale_reasons`，导出与 API 可见），由 `fissue requeue --stale` **人工放行**。

**为什么不直接拿 `content_hash` 判过期**：它含**评论条数**，那样每来一条评论都要重评测一次，
活跃 issue 上等于持续烧 token。

---

## 3. 自己写的标签不能反过来作废自己的结论

打标签是**我们自己的写**。所以写成功后必须把标签**并回本地快照**
（`Repository.merge_item_labels`，取**并集**）：

> 否则下一次抓取必然读到「平台 ≠ 库里」，把刚做出的结论作废重评——与「评论条数」同一类自伤开销，
> 只是触发者是自己的写操作。实测见 [BENCHMARK.md §4.32](../../BENCHMARK.md)。

---

## 4. SQLite 兼容

**SQLite 是默认后端**（零依赖、开箱即用，也是开发与测试全程在用的那条路）：

- 已对 `with_for_update` 等**方言差异**做处理。
- 开启 **WAL** + **30s busy 超时**（驱动默认只有 5s；回滚日志模式下长**读**事务会把写者的提交挡住）。

```bash
# 默认即此值，通常什么都不用写
FISSUE_DATABASE_URL=sqlite:///./data/fissue.db
```

> ✅ **PostgreSQL 后端已完成真实部署验证**（`1.1.0-b4`）：`db init` / `db check` / 全量单测 /
> 服务启动与页面实测均在该后端上跑过，远端 PG 上看板页面切换亦已实测提速。
> 它验证的是**部署与并发路径**；[基准与可信度](Benchmark.md#5-尚未测量) 里各夹具的跑分
> 数字仍全部来自 SQLite。

---

## 5. 导出

`fissue report` / `fissue export` 支持 JSON 与 Markdown 两种格式，输出到 `export.output_dir`（默认 `./data/exports`）。
`bench` 的基线落在 `data/benchmarks/`，是**唯一进版本库的 `data/` 子目录**（其余在 `.gitignore` 内）。

---

## 相关页

[架构设计](Architecture.md) · [AI 层](AI-Layer.md) · [Web 与 REST API](Web-and-API.md#3-鉴权) · [安全模型](Security.md) · [docs/AUTH.md](../AUTH.md)
