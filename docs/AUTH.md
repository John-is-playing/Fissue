# Web 鉴权设计（会话 + API Key + 2FA）

> 状态：**已落地**。目标版本：`v1.1.0` 之后的一个快照。
> 本文是「方案 B」的完整设计——替换原有单一 `FISSUE_API_TOKEN`，改为**会话登录 + 多 API Key**，
> 并保留一条「可信内网（CIDR）免登录」旁路。
>
> **落地记录（与本文的偏差，逐条写明）**
>
> | # | 偏差 | 理由 |
> |---|---|---|
> | 1 | `api.auth_required` / `api.token` 两个键**一并删除**（不只是 `token`） | 留着布尔量等于同一件事有两个真相来源——设了口令却把它写成 false、或反过来。现在唯一开关是 `web.auth.enabled` |
> | 2 | 登录页**独立渲染**（不套 `base.html`） | 登录前左侧导航每条链接都指向 401，套上去只会把用户引到死路 |
> | 3 | `web.trusted_networks` 拒绝**裸地址**（如 `192.168.1.0`） | `ip_network` 会把它当 `/32`：用户想圈网段、实际只放行一台机器，而任何地方都不会提示。要单机请写 `/32` |
> | 4 | 转发头补刀按「**有没有显式声明反代部署**」判定，而不是按来源 | `web.host=127.0.0.1` + 同机 nginx 时对端就是 127.0.0.1，与用户自己点页面在协议层无法区分。已认证、或声明了可信网段则放行；否则见到转发头就 403 |
> | 5 | ~~多了一条 CLI `fissue auth key import-legacy`~~ | **已随垫片在 v1.3.0 移除**：手动导入与自动导入一起删掉了。迁移改为「新建一把正式 Key + 删掉旧口令」，不需要任何导入通道 |
> | 6 | 2FA 二维码**已做**，但编码器落在**服务端 Python**（`web/qr.py`），不是本文设想的「精简 JS 编码器」 | 安全性等价（密钥本来就在服务端，图最终总要显示在这个浏览器里），可测性差很多：Python 版能在 pytest 里离线跑结构与往返断言，JS 版要跑就得让 CI 依赖 node。详见 §12 第 1 条 |


---

## 1. 背景与问题

触发点：`api.enabled=true` 且 `web.host=0.0.0.0`（非回环）却没有鉴权时，配置层直接拒绝启动：

```
api.enabled=true 且 web.host=0.0.0.0 对外可达，但没有鉴权：请在 .env 里设置 FISSUE_API_TOKEN…
```

现有模型只有**一个全局 Bearer 口令**（`api.token`），且：

- 没有登录页——浏览器撞 401 时弹框要口令，存 localStorage（`static/app.js`）；
- 口令即身份，无法区分使用者、无法单独吊销、无法审计；
- 想在内网用浏览器直连，**绕不开**「要么给口令、要么关 API（关了就废掉整个 WebUI）」；
- `api.auth_required` 是布尔开关，没有「谁在访问、访问了什么」。

用户诉求：**内网可直接用浏览器访问**；做一套**密码 + 2FA + 可生成/吊销 API Key** 的鉴权来
替换单一口令；**这一切都要能在 WebUI 里配置**。

---

## 2. 决策（已确认）

| # | 取舍 | 结论 |
|---|---|---|
| 1 | 密码/TOTP 实现 | **纯标准库**（`hashlib.pbkdf2_hmac` + RFC 6238 自实现），零新 Python 依赖 |
| 2 | 2FA | **可选**，默认关闭，用户自行开启 |
| 3 | 旧 `FISSUE_API_TOKEN` | **直接替换**（不再是独立机制；见 §7 的一次性迁移） |
| 4 | 可信内网 CIDR 旁路 | **要**（满足「内网直连」） |
| 5 | 落地方式 | **直出方案 B**（一次性做完整套） |

---

## 3. 总体设计

### 3.1 三种身份来源（互为「或」）

| 来源 | 适用 | 说明 |
|---|---|---|
| **回环**（loopback） | 本机 | 永远免登录——保住现有本地手感 |
| **可信内网**（`web.trusted_networks` 内的 CIDR） | 内网 | 视为等同回环：免登录、可写配置 |
| **会话 / API Key** | 其余（含对外） | 必须持有有效身份 |

判定顺序：先看来源是否回环/可信网（免身份）；否则要求会话 cookie 或 `Authorization: Bearer <key>`。

### 3.2 鉴权机制

- **浏览器会话**：`/login`（用户名 + 密码）→（2FA 开则）`/login/2fa` → 下发 HttpOnly 会话 cookie。
- **API Key**：`Authorization: Bearer fissue_<prefix>_<secret>`，只存哈希，常数时间比对。
- **访客无状态**：无 cookie、无 key 的读请求在**非回环**下也拒绝（对外一律要身份）。

### 3.3 fail-closed 的放宽（改 `Settings._validate`）

启动期只看**配置**（此时数据库还没连上，拿不到「有没有管理员」）：

> 条件：`web.host` 非回环 **且** `api.enabled`
> 放行当且仅当：`web.auth.enabled` **或** `web.trusted_networks` 非空
> 否则 `ConfigError`（报错文案更新为「设置管理员口令登录 / 或声明可信内网」）

注意 `web.auth.enabled` **默认 `true`**，因此**唯一还会触发这条 `ConfigError`** 的情况是：
用户**显式把会话鉴权关掉**、又没配可信网、却把页面绑到非回环地址——也就是「既不要身份、
又不要可信网、又要对外」，这正是应当拦下的组合。你此刻遇到的报错在升级后**不会再出现**。

「**尚无管理员账号**」这件事在**请求期**兜：见 §3.4。启动期放行但会打一条警告。

### 3.4 首次引导（bootstrap）

没有管理员账号时，非回环请求只能凭**一次性引导口令**完成初始化：

- 首次启动 `fissue web`/`serve` 且无管理员账号 → 生成引导口令，打印到控制台，写入
  `data/auth-bootstrap-token`（0600）。
- `POST /api/auth/bootstrap` 用它 + 新用户名/口令创建管理员；**用后即焚**（删文件）。
- 没有它时：回环可引导（本机部署），非回环一律 403，并给出获取引导口令的指引。

> 为什么不是「非回环直接 403 到永远」：那样内网用户永远进不来，等于把「内网直连」诉求打死。

### 3.5 CSRF

cookie 化鉴权必须补 CSRF（现有 header-token 模型天然免疫，换成 cookie 后不再免疫）：

- 每个会话带一个 `csrf_token`；`base.html` 注入 `window.FISSUE.csrfToken`。
- `app.js` 对所有**非 GET** 请求带上 `X-CSRF-Token`（现有请求封装是唯一入口，改一处即可）。
- 服务端：**会话 cookie** 认证的写请求必须校验；**Bearer/API Key** 请求豁免（无 cookie 语义）。
- cookie 用 `SameSite=Lax` + `HttpOnly`；`web.auth.secure_cookie=true` 时加 `Secure`。

---

## 4. 数据模型（新表，`store/tables.py` + `create_all`，无需迁移）

新增三张表（都是**新表**，`create_all` 直接建，不动既有表）：

**`auth_users`**（先支持单管理员，字段按多用户设计）
`id` · `username`(unique) · `password_hash` · `totp_secret`(可空) · `totp_enabled` ·
`recovery_code_hashes`(JSON，一次性) · `created_at` · `updated_at`

**`sessions`**
`id`(pk，随机 32B) · `user_id` · `csrf_token` · `created_at` · `expires_at` · `last_seen_at` ·
`user_agent`(截断) · `peer_ip`(截断)

**`api_keys`**
`id` · `name` · `prefix`(unique，公开 id) · `key_hash` · `created_at` · `last_used_at`(可空) ·
`revoked_at`(可空)

**口令哈希格式**：`pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>`，`iters=600000`。

**服务器签名密钥**：生成一次存 `data/auth-secret.key`（0600），**不进 config.yaml/.env**，用于
会话 cookie 签名（HMAC）与（可选）TOTP 密钥加密。轮换它 = 让所有会话失效。

---

## 5. API 与页面

### 5.1 端点

```
GET  /login                     登录页
POST /api/auth/login            用户名+口令 → 需 2FA 则回 challenge，否则建会话
POST /api/auth/2fa              校验 TOTP（challenge + code）→ 建会话
POST /api/auth/logout           销毁当前会话
GET  /api/auth/me               当前身份（用户名 / 是否开 2FA / 认证方式）
POST /api/auth/bootstrap        首次引导（引导口令 + 新账号）
POST /api/auth/password         改口令（需当前口令）
POST /api/auth/2fa/enroll       生成 TOTP 密钥 + otpauth URI（待验证）
POST /api/auth/2fa/activate     用一次有效 code 确认开启
POST /api/auth/2fa/disable      关闭 2FA（需口令 + code）
POST /api/auth/2fa/recovery     重新生成恢复码（需口令）
GET  /api/auth/api-keys         列出（只回 prefix/名称/时间，绝不含密钥）
POST /api/auth/api-keys         创建（**仅此一次**回明文）
POST /api/auth/api-keys/{id}/revoke  吊销
```

### 5.2 页面

- `/login`（新页，扩展 `base.html`；2FA 步骤复用同一页的多步状态）。
- 设置页新增「**访问与鉴权**」区：鉴权开关、会话时长、可信内网 CIDR、安全 cookie、
  改口令、2FA 开关/绑定/重置恢复码。绑定 2FA 时给**二维码**（服务端现算的内联 SVG）+
  手抄密钥两条路，刷新页面二维码不丢（`GET /api/auth/2fa/pending`）。
- 新页「**API Key**」：列表 + 创建（弹窗显示一次）+ 吊销；并入左侧导航。
- 现有 `/setup` 向导末尾新增「设置管理员口令」步骤（首次部署时无缝）。

---

## 6. 配置项（全部经 WebUI 可写）

`web.auth` 段（`config.yaml` / 覆盖层）：

| 键 | 默认 | 说明 |
|---|---|---|
| `web.auth.enabled` | `true` | 是否启用会话鉴权（关掉=仅靠可信内网/回环） |
| `web.auth.session_ttl_minutes` | `480` | 会话有效期 |
| `web.auth.secure_cookie` | `false` | 反向代理有 TLS 时置 `true` |
| `web.auth.login_max_failures` | `5` | 触发退避前允许的失败次数 |
| `web.trusted_networks` | `[]` | 可信 CIDR（如 `["192.168.1.0/24"]`） |

- `OVERLAY_WRITABLE` **新增**：`web.auth.enabled`、`web.auth.session_ttl_minutes`、
  `web.auth.secure_cookie`、`web.auth.login_max_failures`、`web.trusted_networks`。
- **移除** `FISSUE_API_TOKEN`（`ENV_WRITABLE` / `SECRET_KEYS` / `APIConfig.token` / auth 依赖）。
- 口令哈希、TOTP 密钥、API Key 哈希、会话与签名密钥**走 DB 与 `data/`**，绝不进通用配置写入器。

> 反向代理说明：CIDR 判定**只看真实对端地址**（`request.client.host`），**默认不信任**
> `X-Forwarded-For`（信任转发头本身可被伪造）。确需按真实客户端判定时，打开
> `web.trust_proxy=true` **并**把代理网段写进 `web.trusted_proxies`——两条**缺一不可**：
> 只填开关不填网段会在加载期直接报错，因为「谁的转发头都信」等于让任何人加一行
> `X-Forwarded-For: 127.0.0.1` 就跳过全部鉴权（回环免登录）。同时满足时才采信，且只取
> `X-Forwarded-For` 的**最右一跳**（代理自己追加的那个地址；左侧各段是客户端随便写的）。

---

## 7. 旧口令的收场（兼容垫片已于 v1.3.0 移除）

`FISSUE_API_TOKEN` 不再是独立机制。它在 `1.1.0` 里曾被**一次性导入**成一条
`prefix="legacy"` 的 API Key 作为过渡；这条垫片已在 **v1.3.0 整体移除**：

- **不再自动导入**：启动时不再读 `.env` / 进程环境里的 `FISSUE_API_TOKEN`，
  `migrate_legacy_token` 这个符号本身已删除。
- **不再接受旧口令作 Bearer**：校验只认 `fissue_<prefix>_<secret>` 形状的 Key。
  表里遗留的 `prefix="legacy"` 行勾不住任何请求（它的哈希算的是明文口令，与
  Key 的校验路径不同），请到「API Key」页吊销它——**留着它不会失效，只会留在列表里误导人**。
- **端点已删**：`POST /api/auth/legacy-token` 与 CLI `fissue auth key import-legacy` 都不存在了。

> 为什么必须删干净而不是「留着也无害」：一条垫片等于一条**永不过期、绕过吊销与审计**的
> 明文口令。留着它的成本是永久性的，收益只到升级那一刻。
>
> 迁移做法：`fissue auth key create --name 我的机器` 生成一把正式 Key，
> 用它替换脚本/CI 里的旧口令，最后把 `FISSUE_API_TOKEN` 从 `.env` 删掉。

---

## 8. 安全设计要点

| 面 | 措施 |
|---|---|
| 口令存储 | PBKDF2-HMAC-SHA256，60 万次迭代，随机 16B salt |
| TOTP | RFC 6238 / SHA1 / 6 位 / 30s，±1 步窗口；密钥 base32（20B） |
| 恢复码 | 10 个一次性码，只存哈希，用掉即失效 |
| 比对 | 一律 `hmac.compare_digest`（口令、TOTP、Token、API Key 哈希） |
| 爆破 | 按 IP + 按用户名计数，指数退避锁定（次数/时长可配） |
| 会话 | 服务端可撤销；签名 cookie；过期与轮换即失效 |
| CSRF | `SameSite=Lax` + 双提交 `X-CSRF-Token`（写请求） |
| 锁定自救 | CLI `fissue auth reset`（清 2FA / 重置口令）、`fissue auth set-password` |
| 审计 | `sessions.last_seen_at` + `api_keys.last_used_at`，可看出「谁在用」 |

**已知边界（明确写下，不美化）**：单进程内存态限速在多进程部署下不共享；CIDR 判定不信任
转发头，因此「反代 + 可信网」组合需显式配置。

---

## 9. 与现有代码的接入点

| 位置 | 改动 |
|---|---|
| `config.py` `WebConfig` / `APIConfig` | 加 `web.auth` 与 `web.trusted_networks`；去掉 `api.token` |
| `config.py:696` `_validate` | fail-closed 判据改为「机制已声明或网可信」 |
| `config.py:842` `_apply_env_overrides` | 去掉「设 token 自动开 auth_required」 |
| `web/app.py:_auth_dependency` | 改为「回环/可信网放行 → 否则校验会话或 API Key」 |
| `web/app.py:_reject_proxied_write` / `_reject_remote_write` | 语义更新为「**未认证**的非回环/非可信网写请求一律拒」；已认证（会话或 Key）或可信网来源**直接放行** |
| `web/app.py:_register_api` / `register_setup` | auth 依赖替换；新增 `/api/auth/*` 路由 |
| `web/setup.py` `ENV_WRITABLE` / `OVERLAY_WRITABLE` / `setup_guard` | 白名单增删；已鉴权来源直接放行 |
| `web/templates/base.html` | 注入 `csrfToken` / 当前身份 / 登录态 |
| `web/static/app.js` | 非 GET 请求带 `X-CSRF-Token`；401 → 跳 `/login`（取代弹框输口令） |
| `web/status.py` | 鉴权状态展示（谁在登录、Key 数量） |
| `cli/*` | 新增 `fissue auth {set-password,reset,key,status}` |
| `store/tables.py` | 三张新表 |

---

## 10. 分阶段落地

| 阶段 | 内容 | 产出可验证点 |
|---|---|---|
| **P0** | 配置模型 + fail-closed 放宽 + `trusted_networks` | 内网可直连；报错消失；旧 token 暂仍可用 |
| **P1** | 鉴权存储 + 口令 + 会话 + 登录页 + bootstrap + CSRF | 密码登录可用、CSRF 拦截生效 |
| **P2** | API Key CRUD + 旧 token 迁移 + Key 页 | 多 Key 生成/吊销；旧 token 平滑过渡 |
| **P3** | 2FA（TOTP + 恢复码）+ 设置项 + CLI | 可选 2FA 全程可开可关 |
| **P4** | 加固（限速、CLI 自救、文档、CHANGELOG、`SECURITY.md`） | 文档与安全说明对齐 |

---

## 11. 测试计划（离线，纳入 CI）

- **向量级**：PBKDF2 已知向量、TOTP RFC 6238 官方向量、API Key 哈希/校验、常数时间比对存在性。
- **会话**：签发/校验/过期/吊销/登出；CSRF 缺失或错误 → 403。
- **授权矩阵**：{回环, 可信 CIDR, 非回环} × {无身份, 会话, API Key, 引导口令} × {读, 写}。
- **fail-closed**：`host=0.0.0.0` + 无机制 → `ConfigError`；有 `web.auth.enabled` 或可信网 → 放行。
- **bootstrap**：无账号时非回环被挡；引导口令可建号；用后即焚。
- **迁移**：旧 `FISSUE_API_TOKEN` 导入后仍能作 Bearer 用；吊销后失效。
- **爆破**：连续失败触发退避锁定。

---

## 12. 未决 / 风险

1. **TOTP 二维码**：**已做**（`web/qr.py`，纯标准库）。绑定页面同时给「扫码」与「手抄密钥 +
   `otpauth://` URI」两条路，谁也不取代谁。
   * 编码器支持 byte 模式、ECC L/M、版本 1~10（内容上限约 271 字节，otpauth URI 实测 ~120）；
   * 实现对着独立实现 `segno` 做过**逐位比对**：版本 1~10 × ECC L/M × 8 掩码 × 16 组输入 = 232 张
     矩阵全部逐位相同（比对前需修掉 segno 的一个填充怪癖：它在数据流已字节对齐时仍多写一个
     `0x00` 字节，与 ISO/IEC 18004 §7.4.10 不符）；
   * **已知差异**：自动挑掩码的口径可能与其它库不同（各库对规则 3「重叠图案」计数不一致）。
     掩码号写在格式信息里，读码器据此解掩码，所以**不影响可解码性**；
   * 二维码**不经任何外部服务**（把 TOTP 密钥交给第三方生成图片 = 把二次验证的密钥交出去），
     且 SVG 里只有黑白模块坐标，没有明文密钥；
   * 顺带加了 `GET /api/auth/2fa/pending`：刷新页面后二维码还在原处，不必重新生成一把。
2. **旧 token 迁移方式**（§7）：自动导入 vs 只警告（要求手动重建）。本文取前者，更平滑。
3. **多用户**：本设计按单管理员落地，表结构已留多用户余地；是否要 RBAC 待定。
4. **反代场景**：`web.trust_proxy` **已实现**（连同 `web.trusted_proxies`）——采信转发头
   需要「开关打开 + 直接对端在声明的代理网段里」两条同时成立，且只取最右一跳。
   默认仍关闭：单机 / 内网直连这两种常见形态都没有反代，打开它只是多一个可被伪造的入口。
