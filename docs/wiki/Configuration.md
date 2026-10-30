# 配置参考

> 逐项说明 Fissue 的配置。**比较深入的逐项解释**见 [docs/ENV.md](../ENV.md)（`.env`）与
> [docs/USAGE.md §4](../USAGE.md#4-配置业务规则configyaml)（`config.yaml`）；本页给的是**全量清单 + 关键规则**。

---

## 1. `.env` 与 `config.yaml` 的分工

| | `.env` | `config.yaml` |
|---|---|---|
| 放什么 | 密钥、连接串、**随环境变化**的值 | 业务规则、策略、阈值 |
| 是否提交 git | 不提交（已在 `.gitignore`） | 可提交 |
| 典型内容 | LLM key、数据库密码、平台 Token | 抓哪些仓库、修哪些 Issue、通知发给谁 |
| 格式 | `KEY=value`（无引号、无缩进） | YAML |

判断标准：**这个值换台机器 / 换个人会不会变？** 会变 → `.env`；不会 → `config.yaml`。

### 优先级（高 → 低）

```
① 进程环境变量      FISSUE_LLM_MODEL=xxx fissue eval
      ↓ 覆盖
② .env 文件         ./.env
      ↓ 覆盖
③ 覆盖层            ./config.local.yaml（WebUI 与 fissue onboard 只写这一份）
      ↓ 覆盖
④ config.yaml       ./config.yaml
      ↓ 覆盖
⑤ 内置默认值
```

覆盖层这份文件的路径由 `FISSUE_CONFIG_LOCAL` 指定（默认与 `config.yaml` 同目录、名为 `config.local.yaml`）。
**它的存在意义**：让程序**只写覆盖层**，从而人手写进 `config.yaml` 的注释一字不动。

---

## 2. `.env` 环境变量全表

| 变量 | 作用 | 默认 / 示例 | 必填 |
|---|---|---|---|
| `FISSUE_LLM_API_KEY` | LLM 密钥 | `sk-...` | **是**（`eval/verify/fix` 需要） |
| `FISSUE_DATABASE_URL` | 数据库连接串 | `postgresql+psycopg://fissue:fissue@localhost:5432/fissue`；本地可用 `sqlite:///E:/Fissue/data/fissue.db` | 否（默认同上 PG） |
| `FISSUE_LLM_BASE_URL` | LLM 端点（OpenAI 兼容） | `https://api.deepseek.com/v1` | 否 |
| `FISSUE_LLM_MODEL` | 模型名 | `deepseek-chat` | 否 |
| `FISSUE_LLM_FALLBACK_MODEL` | 主模型失败时降级的备用模型 | 留空则不降级 | 否 |
| `FISSUE_GITHUB_TOKEN` | GitHub Token | `ghp_xxx` | 否（**自动提 PR 必需**） |
| `FISSUE_GITEE_TOKEN` | Gitee 私人令牌 | — | 否 |
| `FISSUE_ATOMGIT_TOKEN` | AtomGit 访问令牌 | — | 否 |
| `FISSUE_GITLAB_TOKEN` | GitLab Access Token | `glpat-xxx` | 否 |
| `FISSUE_WEBHOOK_ENABLED` | 打开入站 webhook | `false` | 否 |
| `FISSUE_WEBHOOK_GITHUB_SECRET` | GitHub webhook Secret（HMAC-SHA256） | — | 开启 webhook 时**必需** |
| `FISSUE_WEBHOOK_GITEE_SECRET` | Gitee webhook 密码 | — | 同上 |
| `FISSUE_WEBHOOK_ATOMGIT_SECRET` | AtomGit webhook 密钥 | — | 同上 |
| `FISSUE_WEBHOOK_GITLAB_SECRET` | GitLab Secret token | — | 同上 |
| `FISSUE_LABEL_CACHE_TTL` | 仓库标签列表的进程内缓存秒数（等价 `platforms.label_cache_ttl_seconds`，`0` = 不缓存） | `600` | 否 |
| `FISSUE_WEB_HOST` | Web/API 绑定地址 | `127.0.0.1` | 否 |
| `FISSUE_WEB_PORT` | Web/API 端口 | `8000` | 否 |
| `FISSUE_SANDBOX_RUNNER_SOCKET` | 转发组件 socket 路径（等价 `sandbox.forwarder.socket`） | `/var/run/fissue-runner.sock` | 否 |
| `FISSUE_CA_BUNDLE` | 手动指定代理 / 企业根证书（PEM） | 如 `E:/certs/corp-root-ca.pem` | 否（TLS 报错时才需要） |
| `FISSUE_INSECURE_SKIP_VERIFY` | 临时关闭 TLS 校验，`1` 生效。**仅供本地排查，切勿生产** | — | 否 |
| `FISSUE_CONFIG` | `config.yaml` 的路径 | `./config.yaml` | 否 |
| `FISSUE_ENV` | 本 `.env` 自身的路径（改用别的 env 文件时） | `./.env` | 否 |
| `FISSUE_CONFIG_LOCAL` | 覆盖层路径 | `./config.local.yaml` | 否 |
| `FISSUE_LOG_LEVEL` | `debug / info / warning / error`（等价 `fissue -v`） | `info` | 否 |

> 上表里带「密钥 / Token / Secret」的键都能在 **`/settings` 页面**改写（写入 `.env`，留空 = 不改动，
> 勾「清空」= 移除本层设置）。页面只回「已配置 / 未配置」与掩码，**不回显原值**。

### 关于 TLS 中间人

`Fissue` 默认安装 `truststore`，**自动改用操作系统信任库**——企业网关 / 抓包工具重签的证书，其根证书在系统信任库里（浏览器正常）但不在 `certifi` 里。多数情况无需配置。
若仍失败，先看报错里的「当前生效的信任链」：显示 `certifi` → 说明没走系统库；显示「操作系统信任库」→ 说明根证书**连系统库也没有**，需导出并设 `FISSUE_CA_BUNDLE`。

### 已移除的键

- `FISSUE_API_TOKEN` / `api.token` / `api.auth_required` **已不再是配置项**。鉴权改为
  **会话登录 + 多 API Key + 可信内网**（见 [Web 与 REST API](Web-and-API.md#3-鉴权) 与 [docs/AUTH.md](../AUTH.md)）。
  若环境里仍留着旧口令，**启动时会自动把它导入成一条 API Key**（`prefix=legacy`），到「API Key」页吊销并改用正式 Key 即可。

---

## 3. `config.yaml` 键清单

### `app`

| 键 | 默认 | 说明 |
|---|---|---|
| `app.log_level` | `info` | `debug / info / warning / error` |
| `app.data_dir` | `./data` | 本地数据目录（克隆缓存、报告、patch 产物） |

### `database`

| 键 | 默认 | 说明 |
|---|---|---|
| `database.url` | `sqlite:///./data/fissue.db` | 可被 `FISSUE_DATABASE_URL` 覆盖；可选 PostgreSQL（`postgresql+psycopg://`）或 MySQL 8.0+（`mysql+pymysql://`） |
| `database.echo` | `false` | 打印 SQL |
| `database.pool_size` | `5` | 连接池大小（SQLite 不用） |

### `llm`

| 键 | 默认 | 说明 |
|---|---|---|
| `llm.base_url` | `https://api.deepseek.com/v1` | OpenAI 兼容端点 |
| `llm.model` | `deepseek-chat` | 模型名 |
| `llm.fallback_model` | `deepseek-chat` | 失败降级模型 |
| `llm.temperature` | `0.2` | 采样温度 |
| `llm.max_tokens` | `16384` | 单次补全上限（被截断会自动加码重试到 `max_tokens_ceiling`） |
| `llm.max_tokens_ceiling` | `32768` | 加码重试的天花板 |
| `llm.timeout_seconds` | `120` | 单次调用超时 |
| `llm.max_retries` | `4` | 调用重试次数 |
| `llm.concurrency` | `4` | 并发上限（信号量） |
| `llm.budget.daily_tokens_per_repo` | `2000000` | 单仓库每日 token 上限 |
| `llm.budget.daily_usd_per_repo` | `5.0` | 单仓库每日花费上限 |
| `llm.budget.hard_stop` | `true` | 超预算即停；`false` = 仅告警 |
| `llm.pricing.input_per_1k` | `0.00014` | 成本估算用（美元 / 1K token） |
| `llm.pricing.output_per_1k` | `0.00028` | 同上 |

### `repos`（列表，**必改**）

每个条目：`platform`（github/gitee/atomgit/gitlab）· `owner` · `name` · `base_branch`（留空自动探测）·
`api_base`（GitLab 自建实例）· `enabled` · `collect`（`[issue, pr]`）· `labels_include`（空 = 不过滤）·
`labels_exclude`（如 `[wontfix, duplicate]`）· `assignee` · `since_days`（`30`，`0` = 不限）· `test_hint`（单仓库级测试命令提示）。

### `classification`

| 键 | 默认 | 说明 |
|---|---|---|
| `classification.use_keywords_first` | `true` | 先用关键词预筛，一边倒时直接定论、不调 LLM |
| `classification.bug_keywords` | `[bug, error, crash, fail, exception, regression, 报错, 崩溃, 异常, 失败, 修复, 无法]` | BUG 关键词 |
| `classification.feature_keywords` | `[feature, enhancement, proposal, support, request, 建议, 新增, 支持, 需求, 优化]` | FEATURE 关键词 |
| `classification.fallback_to_llm` | `true` | 两者都命中或都不命中时交 AI 决定 |

> 中文关键词用子串匹配；ASCII 关键词用词边界匹配（`(?<![a-z0-9_])kw(?![a-z0-9_])`），避免 `request` 误命中 `requests.get`。

### `evaluation.thresholds`

| 键 | 默认 | 说明 |
|---|---|---|
| `evaluation.thresholds.importance_high` | `70` | 重要性 ≥ 此值视为高 |
| `evaluation.thresholds.difficulty_low` | `40` | 修复难度 ≤ 此值视为低 |
| `evaluation.thresholds.authenticity_min` | `40` | 真实性低于此值 → 疑似误报 / 刷子 |
| `evaluation.thresholds.alert_importance` | `80` | ≥ 此值触发告警推送 |

> 四个维度（真实性 / 重要性 / 可行性 / PR 质量）与 0~100 区间是**固定口径**，硬编码在
> `ai/evaluator.py`，不通过配置切换。反刷子**恒开、不可关闭**。

### `verifier`

| 键 | 默认 | 说明 |
|---|---|---|
| `verifier.mode` | `hybrid` | `executable / checklist / hybrid` |
| `verifier.require_f2p` | `true` | F2P：修复前必失败、修复后必通过 |
| `verifier.max_rounds` | `2` | 验证器不可靠时自校正的最大轮次 |
| `verifier.allow_checklist_fallback` | `true` | 无测试框架时降级为自然语言清单（LLM 按证据逐条判定；配置了 `test_hint` 的仓库**不允许**降级，强制可执行验证器） |
| `verifier.checklist_force_prompt` | `true` | `force_fix` 条目的清单判定改用强制性提示词（禁止「无法验证」回避，证据不足须点名缺哪个文件/代码）；关掉则回退普通措辞 |
| `verifier.regression_gate` | `strict` | `off / warn / strict`，见[验证器与 F2P](Verifier-and-F2P.md#2-回归门regression-gate) |
| `verifier.regression_command` | `null` | 留空 → 仓库级 `test_hint` → 探测（pytest / npm test / go test…） |
| `verifier.regression_timeout_seconds` | `900` | 回归门独立预算（单测验证器默认 600） |

### `sandbox`

| 键 | 默认 | 说明 |
|---|---|---|
| `sandbox.enabled` | `true` | 是否启用沙盒 |
| `sandbox.image` | `fissue/sandbox-base:latest` | 基础镜像 |
| `sandbox.runtime` | `docker` | **只接受** `docker / local`；拼错在加载配置时即被拒 |
| `sandbox.allow_local_fallback` | `false` | Docker 不可用时是否允许降级为**无隔离**的本地执行 |
| `sandbox.local_env_allowlist` | `PATH / HOME / LANG / LC_ALL / LC_CTYPE / TMPDIR / TMP / TEMP / SYSTEMROOT / PATHEXT / COMSPEC / PYTHONIOENCODING / PYTHONUNBUFFERED / PYTHONDONTWRITEBYTECODE` | 本地执行时放行给子进程的环境变量**白名单** |
| `sandbox.network` | `none` | 禁网（最安全） |
| `sandbox.read_only_root` | `true` | 根文件系统只读 |
| `sandbox.git_readonly` | `true` | 工作区里 `.git` 对容器**只读**（`index` 除外） |
| `sandbox.user` | `""` | 留空 = 跟随宿主 `uid:gid`（容器要能写进工作区） |
| `sandbox.mounts.type` | `volume` | `volume / loopback` |
| `sandbox.mounts.size_mb` | `2048` | 隔离卷大小 |
| `sandbox.limits.cpus` | `2.0` | CPU 上限 |
| `sandbox.limits.memory_mb` | `2048` | 内存上限 |
| `sandbox.limits.pids` | `256` | 进程数上限 |
| `sandbox.limits.timeout_seconds` | `600` | 单次验证最长时长 |
| `sandbox.forwarder.enabled` | `true` | 转发组件 |
| `sandbox.forwarder.socket` | `/var/run/fissue-runner.sock` | socket 路径 |
| `sandbox.keep_artifacts` | `true` | 保留容器日志以便出报告 |

### `queues`

| 键 | 默认 | 说明 |
|---|---|---|
| `queues.verify_queue.flush_size` | `10` | Issue-BUG 验证队列的数量阈值 |
| `queues.verify_queue.idle_flush_seconds` | `300` | 队首静默多久触发 flush |
| `queues.fix_queue.flush_size` | `10` | PR 修复/合并队列的数量阈值 |
| `queues.fix_queue.idle_flush_seconds` | `300` | 同上 |
| `queues.manual_flush_enabled` | `true` | 允许 CLI / API 手动 flush |

### `auto_fix`

| 键 | 默认 | 说明 |
|---|---|---|
| `auto_fix.enabled` | `true` | 是否启用自动修复 |
| `auto_fix.agent_max_rounds` | `12` | Agent 循环最大轮次 |
| `auto_fix.stall_rounds` | `4` | 连续多少轮「验证器输出完全一致」即收工（`0` = 关闭） |
| `auto_fix.no_write_rounds` | `6` | 连续多少轮「一个文件都没改」即收工（`0` = 关闭），只被 write 清零 |
| `auto_fix.clone_retries` | `2` | 克隆工作区重试次数（`0` = 不重试） |
| `auto_fix.regression_retries` | `1` | 回归门失败后带着失败用例重跑修复的次数（`0` = 关闭） |
| `auto_fix.review_rounds` | `1` | 修复后的评审轮次（`0` = 关闭） |
| `auto_fix.review_model` | `""` | 留空 = 用全局 `llm.model`（建议换一个模型当第二意见） |
| `auto_fix.allow_file_write` | `true` | 设 `false` 则只出补丁、不动工作区 |
| `auto_fix.syntax_check` | `true` | Python 文件落盘前 `compile()` 自检，不合法则拒绝该文件 |
| `auto_fix.protected_paths` | `[".github/**", ".gitlab/**", "LICENSE", "**/*.lock"]` | 禁止写入的路径 |
| `auto_fix.max_changed_files` | `20` | 变更文件数上限，超出即回滚 |
| `auto_fix.max_diff_lines` | `800` | diff 行数上限，超出即回滚 |
| `auto_fix.on_failure` | `report_manual` | 失败→生成需人工报告；另一取值 `discard`（只记录失败） |
| `auto_fix.pr_strategy.mode` | `fork` | `fork / direct / patch_only` |
| `auto_fix.pr_strategy.branch_prefix` | `fissue/fix-` | 分支前缀 |
| `auto_fix.pr_strategy.label` | `ai-generated` | 自动打的 AI 标识标签 |
| `auto_fix.pr_strategy.extra_labels` | `[fissue]` | 额外标签 |
| `auto_fix.pr_strategy.draft` | `false` | 是否提草稿 PR |

> 历史注记：曾有 `auto_submit` 开关，但代码从未读取它，已删除。**要「只出补丁不提 PR」请用 `pr_strategy.mode: patch_only` 或 `fissue fix --dry-run`。**

### `fix_policy`

| 键 | 默认 | 说明 |
|---|---|---|
| `fix_policy.tier1.max_difficulty` | `40` | 难度低 + 重要性高的上限 |
| `fix_policy.tier1.min_importance` | `70` | 同上 |
| `fix_policy.tier2.max_difficulty` | `40` | 难度低 + 重要性低 |
| `fix_policy.tier2.min_importance` | `0` | 同上 |
| `fix_policy.only_issues` | `true` | 仅对 Issue 自动修复；PR 交开发者手动合并 |
| `fix_policy.tier_strategy` | `importance` | `importance / dual / custom`，见下 |
| `fix_policy.custom_tier` | `null` | 仅 `custom` 需要，如 `"myproj.policy:tier_of"` |

`tier_strategy` 三档：

- `importance`（默认）：难度是硬门槛，`min_importance` 区分 tier1/tier2。
- `dual`：难度与重要性各自判档，取**更严**的一档（适合给 tier1 更紧的难度门槛）。
- `custom`：由 `custom_tier` 指定的用户函数判定；此时各 tier 的 `min_importance` **不生效**。

自定义函数签名 `func(difficulty, importance, policy) -> str`，支持 `pkg.mod:func` 与 `pkg.mod.func` 两种写法，
返回值接受 `"tier1" / "tier2" / "none"`（大小写不敏感）或 `fissue.models.Priority`。
加载或执行失败会记 warning 并**回退默认策略**，避免「配置写错 → 静默不修」。

### `schedule`

| 键 | 默认 | 说明 |
|---|---|---|
| `schedule.enabled` | `true` | 常驻服务是否跑周期循环 |
| `schedule.incremental` | `true` | 增量扫描（首次全量） |
| `schedule.scan_interval_seconds` | `3600` | 扫描间隔（每小时） |
| `schedule.queue_flush_interval_seconds` | `60` | 队列检查间隔 |
| `schedule.full_rescan_days` | `30` | 每 N 天做一次全量兜底 |
| `schedule.verify_enabled` | `true` | 常驻服务是否做验证（`false` 则交给 `fissue verify`） |
| `schedule.verify_batch_limit` | `30` | 每轮最多验证多少条 |

### `notify`

| 键 | 默认 / 取值 | 说明 |
|---|---|---|
| `notify.enabled` | `true` | 是否通知 |
| `notify.channels[].type` | `webhook / email / wecom / dingtalk / feishu / console` | 渠道类型 |
| `notify.channels[].enabled` | `true` | 是否启用 |
| `notify.channels[].url` | — | 渠道地址 |
| `notify.channels[].secret` | `""` | 可选签名密钥 |
| `notify.events` | `[scan_done, eval_done, verify_done, fix_done, pr_created, budget_exceeded, needs_manual]` | 订阅的事件 |

### `web` / `api`

| 键 | 默认 | 说明 |
|---|---|---|
| `web.host` | `127.0.0.1` | 绑定地址（默认仅本机） |
| `web.port` | `8000` | 端口 |
| `web.read_only` | `false` | `true` 则隐藏所有操作按钮、API 动作类返回 403 |
| `web.auth.enabled` | `true` | **唯一总开关**；关掉 = 只靠回环 / 可信网放行 |
| `web.auth.session_ttl_minutes` | `480` | 会话有效期（分钟） |
| `web.auth.secure_cookie` | `false` | 反向代理已提供 HTTPS 时置 `true` |
| `web.auth.login_max_failures` | `5` | 连续失败多少次后指数退避锁定 |
| `web.trusted_networks` | `[]` | 可信 CIDR（如 `["192.168.1.0/24"]`）——落在其中的来源**视为等同回环** |
| `web.trust_proxy` | `false` | 是否采信反代转发头；**必须**同时配 `web.trusted_proxies`，否则加载期报错 |
| `web.trusted_proxies` | `[]` | 反向代理自身的 CIDR（如 `["127.0.0.1/32"]`）；只有这些对端送来的转发头才算数 |
| `api.enabled` | `true` | 是否注册 REST API |
| `api.prefix` | `/api/v1` | API 前缀 |

> 口令、TOTP 密钥、API Key 哈希**都不在这些配置里**——它们在**数据库**（以及 `data/auth-secret.key`）。
> 详见 [Web 与 REST API](Web-and-API.md#3-鉴权)。
>
> `web.trusted_networks` 判定**只看真实对端地址**，默认不信任 `X-Forwarded-For`；
> 裸地址（如 `192.168.1.0`）会被**拒绝**——`ip_network` 会把它当 `/32`，想圈网段请显式写 `192.168.1.0/24`，单机写 `/32`。

### `webhooks`

| 键 | 默认 | 说明 |
|---|---|---|
| `webhooks.enabled` | `false` | 打开入站 webhook |
| `webhooks.secrets.github` | `""` | 也可用 `FISSUE_WEBHOOK_GITHUB_SECRET` |
| `webhooks.secrets.gitlab` | `""` | 同上 |
| `webhooks.secrets.gitee` | `""` | 同上 |
| `webhooks.secrets.atomgit` | `""` | 同上 |

### `export`

| 键 | 默认 | 说明 |
|---|---|---|
| `export.output_dir` | `./data/exports` | 导出目录（格式由命令行决定） |

### `platforms`

| 键 | 默认 | 说明 |
|---|---|---|
| `platforms.label_cache_ttl_seconds` | `600` | 标签列表进程内缓存时长（`0` = 不缓存） |
| `platforms.label_concurrency` | `null` | 打标签并发上限；留空跟随 `llm.concurrency` |
| `platforms.max_retry_after_seconds` | `300` | 被限流时**单次最多等多久**（封顶，避免被 GitHub 一小时的 reset 卡住） |
| `platforms.max_retries` | `4` | 平台 HTTP 重试次数（与 `llm.max_retries` **无关**） |

---

## 4. 启动期校验（fail-closed）

以下组合会在**加载配置时直接报错**，不会「静默降级」：

| 条件 | 结果 |
|---|---|
| `sandbox.runtime` 不是 `docker` / `local`（如 `Docker`、`podman`、带尾空格） | 加载配置即被拒；运行期另有一道 fail-closed，**不会静默落到无隔离的本地执行** |
| `web.host` 非回环 **且** `api.enabled`，但**既没有** `web.auth.enabled` **也**没有 `web.trusted_networks` | `ConfigError`：报错文案为「设置管理员口令登录 / 或声明可信内网」。**默认 `web.auth.enabled=true` 已满足**，所以升级后不会再看到这条 |
| `webhooks.enabled: true` 但某个**已启用平台**没配密钥 | 拒绝启动（刻意的 fail-closed——该端点不带 bearer 鉴权，密钥就是身份证明） |
| `regression_gate` 不是 `off / warn / strict` | `ConfigError` |

> 反向代理提醒：上面那道检查只能看**绑定地址**，看不见代理。「绑 `127.0.0.1` + 同机 nginx 转出去」同样等于把写控制面开放。
> 未开鉴权时，带转发头的写请求会被 403，但那只是补刀——**边界始终是令牌**。

---

## 5. WebUI 可写 vs 绝不可写

Web 设置页与 `fissue onboard` 通过**写白名单**改配置：

- **可写回覆盖层（`config.local.yaml`）**：业务配置与部分环境相关项，例如 `web.auth.enabled`、`web.auth.session_ttl_minutes`、
  `web.auth.secure_cookie`、`web.auth.login_max_failures`、`web.trusted_networks`，以及各业务段（repos / 阈值 / 策略 / 通知等）。
- **绝不进通用配置写入器**：口令哈希、TOTP 密钥、API Key 哈希、会话与签名密钥——它们走 **数据库** 与 `data/`
  （`data/auth-secret.key`、`data/auth-bootstrap-token`）。这些文件生成一次、权限 `0600`，**不写进** `config.yaml` / `.env`。

---

## 相关页

[快速开始](Getting-Started.md) · [CLI 参考](CLI-Reference.md) · [Web 与 REST API](Web-and-API.md) · [部署与运维](Deployment.md) · [故障排查](Troubleshooting.md)
