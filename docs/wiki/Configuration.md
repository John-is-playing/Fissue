# 配置参考

> 全量键清单与关键规则。`.env` 逐项解释见 [ENV.md](../ENV.md)，业务配置的操作背景见
> [USAGE.md §4](../USAGE.md)；本页是**查阅用速查**。

## 1. `.env` 与 `config.yaml` 的分工

| | `.env` | `config.yaml` |
|---|---|---|
| 放什么 | 密钥、连接串、随环境变化的值 | 业务规则、策略、阈值 |
| 提交 git | ❌ | ✅ |
| 格式 | `KEY=value` | YAML |

判断标准：**换个环境会变的值 → `.env`**。

### 优先级（高 → 低）

```
进程环境变量 > .env > 覆盖层 config.local.yaml > config.yaml > 内置默认
```

覆盖层路径由 `FISSUE_CONFIG_LOCAL` 指定（默认 `./config.local.yaml`）。WebUI 与 `fissue onboard`
**只写覆盖层**，所以手写 `config.yaml` 的注释一字不动。

## 2. 环境变量

完整说明（含各变量填错的表现、TLS 中间人处置）见 [ENV.md](../ENV.md)。速查：

| 变量 | 作用 | 默认 |
|---|---|---|
| `FISSUE_LLM_API_KEY` | LLM 密钥（`eval/verify/fix` 必需） | — |
| `FISSUE_DATABASE_URL` | 连接串 | `sqlite:///./data/fissue.db` |
| `FISSUE_LLM_BASE_URL` / `_MODEL` / `_FALLBACK_MODEL` | 端点 / 模型 / 降级模型 | 取 config.yaml |
| `FISSUE_GITHUB_TOKEN` / `GITEE` / `ATOMGIT` / `GITLAB` | 平台 Token | 空 |
| `FISSUE_WEBHOOK_ENABLED` + `FISSUE_WEBHOOK_<平台>_SECRET` | 入站 webhook | `false` |
| `FISSUE_LABEL_CACHE_TTL` | 标签列表进程内缓存秒数（`0` = 不缓存） | `600` |
| `FISSUE_WEB_HOST` / `FISSUE_WEB_PORT` | 绑定地址 / 端口 | `127.0.0.1` / `8000` |
| `FISSUE_SANDBOX_RUNNER_SOCKET` | 转发组件 socket | `/var/run/fissue-runner.sock` |
| `FISSUE_CA_BUNDLE` | 企业/代理根证书 PEM | 空（用系统信任库） |
| `FISSUE_INSECURE_SKIP_VERIFY` | 关 TLS 校验，**仅排查** | — |
| `FISSUE_CONFIG` / `FISSUE_ENV` / `FISSUE_CONFIG_LOCAL` | 三个文件路径 | `./config.yaml` / `./.env` / `./config.local.yaml` |
| `FISSUE_LOG_LEVEL` | 日志级别（等价 `-v`） | `info` |

> 含「密钥 / Token / Secret」的键都能在 `/settings` 页改写（写进 `.env`，留空 = 不改，勾「清空」
> = 移除）。页面只回「已配置/未配置」与掩码，**不回显原值**。
>
> **已移除**：`FISSUE_API_TOKEN` / `api.token` / `api.auth_required`。鉴权改为会话 + 多 API Key
> + 可信内网，见 [AUTH.md](../AUTH.md)。

## 3. `config.yaml` 键清单

### `app` / `database`

| 键 | 默认 | 说明 |
|---|---|---|
| `app.log_level` | `info` | 日志级别 |
| `app.data_dir` | `./data` | 克隆缓存、报告、patch 产物 |
| `database.url` | `sqlite:///./data/fissue.db` | 可被 `FISSUE_DATABASE_URL` 覆盖 |
| `database.echo` / `pool_size` | `false` / `5` | 打印 SQL / 连接池 |

### `llm`

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` / `model` / `fallback_model` | `https://api.deepseek.com/v1` / `deepseek-chat` / `deepseek-chat` | 端点与模型 |
| `temperature` | `0.2` | 采样温度 |
| `max_tokens` / `max_tokens_ceiling` | `16384` / `32768` | 单次上限 / 加码重试天花板 |
| `timeout_seconds` / `max_retries` | `120` / `4` | 超时与重试 |
| `concurrency` | `4` | 并发信号量 |
| `budget.daily_tokens_per_repo` / `daily_usd_per_repo` / `hard_stop` | `2000000` / `5.0` / `true` | 预算熔断 |
| `pricing.input_per_1k` / `output_per_1k` | `0.00014` / `0.00028` | 成本估算 |

### `repos`（列表，**必改**）

`platform`（github/gitee/atomgit/gitlab）· `owner` · `name` · `base_branch`（留空自动探测）·
`api_base`（自建 GitLab）· `enabled` · `collect`（`[issue, pr]`）· `labels_include`（空 = 不过滤）·
`labels_exclude` · `assignee` · `since_days`（`30`，`0` = 不限）· `test_hint`（单仓库测试命令提示）。

### `classification`

| 键 | 默认 | 说明 |
|---|---|---|
| `use_keywords_first` | `true` | 先关键词预筛，一边倒时不调 LLM |
| `bug_keywords` | `[bug, error, …, 报错, 崩溃, 无法]` | BUG 关键词 |
| `feature_keywords` | `[feature, …, 建议, 新增, 优化]` | FEATURE 关键词 |
| `fallback_to_llm` | `true` | 两边都命中/都不命中时交 AI |

### `evaluation.thresholds`

`importance_high: 70` · `difficulty_low: 40` · `authenticity_min: 40` · `alert_importance: 80`。
四维与 0~100 区间是**固定口径**（硬编码在 `ai/evaluator.py`）；反刷子恒开不可关。

### `verifier`

| 键 | 默认 | 说明 |
|---|---|---|
| `mode` | `hybrid` | `executable / checklist / hybrid` |
| `require_f2p` | `true` | 修复前必失败、修复后必通过 |
| `max_rounds` | `3`（示例配置取 `2`） | 验证器自校正轮次 |
| `allow_checklist_fallback` | `true` | 无测试框架时降级清单；**配了 `test_hint` 不允许降级** |
| `checklist_force_prompt` | `true` | force 条目用强制性清单提示词（禁止「无法验证」回避） |
| `regression_gate` | `strict` | `off / warn / strict` |
| `regression_command` | `null` | 留空 → `test_hint` → 探测 |
| `regression_timeout_seconds` | `900` | 回归门独立预算 |

### `sandbox`

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` / `image` | `true` / `fissue/sandbox-base:latest` | 开关与镜像 |
| `runtime` | `docker` | **只接受** `docker / local`，拼错加载即拒 |
| `allow_local_fallback` | `false` | Docker 不可用时是否允许**无隔离**本地执行 |
| `local_env_allowlist` | `PATH HOME LANG TMPDIR SYSTEMROOT …` | 本地执行的环境变量白名单 |
| `network` / `read_only_root` / `git_readonly` | `none` / `true` / `true` | 隔离参数（`.git` 只读，`index` 除外） |
| `user` | `""` | 留空 = 跟随宿主 `uid:gid` |
| `mounts.type` / `size_mb` | `volume` / `2048` | 隔离卷 |
| `limits.cpus` / `memory_mb` / `pids` / `timeout_seconds` | `2.0` / `2048` / `256` / `600` | 资源上限 |
| `forwarder.enabled` / `socket` | `true` / `/var/run/fissue-runner.sock` | 转发组件 |
| `keep_artifacts` | `true` | 保留容器日志以便出报告 |

### `queues`

`verify_queue.flush_size: 10` · `verify_queue.idle_flush_seconds: 300` · `fix_queue.*` 同上 ·
`manual_flush_enabled: true`。

### `auto_fix`

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否自动修复 |
| `agent_max_rounds` | `12` | Agent 循环上限 |
| `stall_rounds` / `no_write_rounds` | `4` / `6` | 两道空转闸门（`0` = 关闭） |
| `clone_retries` | `2` | 克隆重试 |
| `regression_retries` | `1` | 回归门失败后带失败用例重跑修复 |
| `review_rounds` / `review_model` | `1` / `""` | 修复后评审轮次与模型（留空用全局） |
| `allow_file_write` | `true` | `false` 则只出补丁不动工作区 |
| `syntax_check` | `true` | Python 落盘前 `compile()` |
| `protected_paths` | `.github/** .gitlab/** LICENSE **/*.lock` | 禁止写入 |
| `max_changed_files` / `max_diff_lines` | `20` / `800` | 规模护栏，超出回滚 |
| `on_failure` | `report_manual` | 失败生成人工报告（或 `discard`） |
| `pr_strategy.mode` | `fork` | `fork / direct / patch_only` |
| `pr_strategy.branch_prefix` / `label` / `extra_labels` / `draft` | `fissue/fix-` / `ai-generated` / `[fissue]` / `false` | 提 PR 方式 |

> 曾有 `auto_submit` 开关但代码从未读取，已删除。要「只出补丁不提 PR」用
> `pr_strategy.mode: patch_only` 或 `fissue fix --dry-run`。

### `fix_policy`

| 键 | 默认 | 说明 |
|---|---|---|
| `tier1.max_difficulty` / `min_importance` | `40` / `70` | 难度低 + 重要性高 |
| `tier2.max_difficulty` / `min_importance` | `40` / `0` | 难度低 + 重要性低 |
| `only_issues` | `true` | 只自动修 Issue |
| `tier_strategy` | `importance` | `importance / dual / custom / percentile` |
| `custom_tier` | `null` | 仅 `custom`，如 `"myproj.policy:tier_of"` |
| `tier1_share` / `percentile_min_batch` | `0.5` / `3` | 仅 `percentile` |

`tier_strategy`：`importance`（难度硬门槛，`min_importance` 分档）· `dual`（两维取严）·
`custom`（用户函数，`min_importance` 不生效）· `percentile`（难度仍是硬门槛，重要性看批内排名）。
分位让**容量占比**稳定，但近分条目仍会翻，且夹具逐条期望表不再可比（见 [BENCHMARK.md §4.7.2](../../BENCHMARK.md)）。

自定义签名 `func(difficulty, importance, policy) -> str`，支持 `pkg.mod:func` 与 `pkg.mod.func`，
返回 `"tier1"/"tier2"/"none"`（大小写不敏感）或 `Priority`；加载/执行失败记 warning 并回退默认策略。

### `schedule`

`enabled: true` · `incremental: true` · `scan_interval_seconds: 3600` ·
`queue_flush_interval_seconds: 60` · `full_rescan_days: 30` · `verify_enabled: true` ·
`verify_batch_limit: 30`。

### `notify`

`enabled: true` · `channels[].type`（`webhook / email / wecom / dingtalk / feishu / console`）·
`channels[].url` / `secret` · `events`（`scan_done, eval_done, verify_done, fix_done, pr_created,
budget_exceeded, needs_manual`）。

### `web` / `api`

| 键 | 默认 | 说明 |
|---|---|---|
| `web.host` / `web.port` | `127.0.0.1` / `8000` | 绑定 |
| `web.read_only` | `false` | `true` 隐藏操作按钮，API 动作类 403 |
| `web.auth.enabled` | `true` | **唯一总开关** |
| `web.auth.session_ttl_minutes` / `secure_cookie` / `login_max_failures` | `480` / `false` / `5` | 会话与爆破防护 |
| `web.trusted_networks` | `[]` | 可信 CIDR，视为等同回环 |
| `web.trust_proxy` / `web.trusted_proxies` | `false` / `[]` | 采信转发头（两条**缺一不可**） |
| `api.enabled` / `api.prefix` | `true` / `/api/v1` | REST API |

> 口令、TOTP 密钥、API Key 哈希**不在配置里**——走数据库与 `data/auth-secret.key`。
> 裸地址（`192.168.1.0`）会被拒绝：`ip_network` 当 `/32`，网段请写 `/24`，单机写 `/32`。

### `webhooks` / `export` / `platforms`

`webhooks.enabled: false` + `webhooks.secrets.{github,gitlab,gitee,atomgit}`；
`export.output_dir: ./data/exports`；
`platforms.label_cache_ttl_seconds: 600` · `label_concurrency: null` ·
`max_retry_after_seconds: 300` · `max_retries: 4`。

## 4. 启动期校验（fail-closed）

| 条件 | 结果 |
|---|---|
| `sandbox.runtime` 不是 `docker` / `local` | 加载即拒，运行期另有 fail-closed |
| `web.host` 非回环 + `api.enabled`，但既无 `web.auth.enabled` 也无 `web.trusted_networks` | `ConfigError`（默认 `true` 已满足） |
| `webhooks.enabled: true` 但某已启用平台没配密钥 | 拒绝启动（端点不带 bearer，密钥即身份） |
| `regression_gate` 不是 `off / warn / strict` | `ConfigError` |

> 该检查只能看**绑定地址**，看不见代理。「绑 `127.0.0.1` + 同机 nginx 转出去」同样等于把写
> 控制面开放。

## 5. WebUI 可写 vs 绝不可写

- **可写回覆盖层**：业务配置与部分环境项（`web.auth.*`、`web.trusted_networks`、repos / 阈值 /
  策略 / 通知等）。
- **绝不进通用配置写入器**：口令哈希、TOTP 密钥、API Key 哈希、会话与签名密钥——走数据库与
  `data/`（`auth-secret.key`、`auth-bootstrap-token`，`0600`）。

---

相关：[快速开始](Getting-Started.md) · [CLI 参考](CLI-Reference.md) · [Web 与 REST API](Web-and-API.md) ·
[ENV.md](../ENV.md) · [部署与运维](Deployment.md)