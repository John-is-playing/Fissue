# Web 鉴权设计（会话 + API Key + 2FA）

> **已落地。** 用「会话登录 + 多 API Key + 可信内网旁路」替换旧的单一 `FISSUE_API_TOKEN`。
> 本文是设计口径与取舍记录；操作步骤见 [USAGE.md §8.3](USAGE.md)，变量见 [ENV.md](ENV.md)。

## 1. 问题

旧模型只有一个全局 Bearer 口令（`api.token`）：没有登录页（浏览器撞 401 弹框、口令存
localStorage）、口令即身份、无法区分使用者与单独吊销、内网浏览器直连绕不开「给口令或关
API」。且 `api.enabled=true` + `web.host=0.0.0.0` 而无鉴权时，配置层会拒绝启动。

目标：内网可直接浏览器访问；密码 + 2FA + 可生成/吊销 API Key；全部可在 WebUI 配置。

## 2. 三种身份来源（互为「或」）

| 来源 | 适用 | 行为 |
|---|---|---|
| 回环（loopback） | 本机 | 永远免登录 |
| 可信内网（`web.trusted_networks` 内的 CIDR） | 内网 | 等同回环：免登录、可写配置 |
| 会话 / API Key | 其余（含对外） | 必须持有有效身份 |

判定顺序：先看来源是否回环/可信网；否则要求会话 cookie 或 `Authorization: Bearer <key>`。
无 cookie、无 key 的请求在非回环下**连读都拒绝**。

- 会话：`/login`（用户名+口令）→ 若开 2FA 则 `/login/2fa` → 下发 HttpOnly cookie。
- API Key：`fissue_<prefix>_<secret>`，只存哈希，常数时间比对。

## 3. fail-closed 与首次引导

启动期只看配置（此时数据库还没连上）：

> 条件 `web.host` 非回环 **且** `api.enabled` 时，放行当且仅当 `web.auth.enabled`
> **或** `web.trusted_networks` 非空，否则 `ConfigError`。

`web.auth.enabled` 默认 `true`，所以唯一还会触发报错的组合是「显式关鉴权 + 无可信网 + 对外绑定」。

**尚无管理员**在请求期兜：首次启动生成一次性引导口令，打印到控制台并写入
`data/auth-bootstrap-token`（0600）；`POST /api/auth/bootstrap` 用它 + 新账号创建管理员，
**用后即焚**。没有它时回环可引导，非回环 403（否则内网用户永远进不来）。

## 4. CSRF

cookie 化鉴权必须补 CSRF（header-token 模型天然免疫）：会话带 `csrf_token`，`base.html`
注入 `window.FISSUE.csrfToken`，`app.js` 对所有非 GET 请求带 `X-CSRF-Token`；服务端只校验
**会话 cookie** 认证的写请求，Bearer 请求豁免。cookie 用 `SameSite=Lax` + `HttpOnly`，
`web.auth.secure_cookie=true` 时加 `Secure`。

## 5. 数据模型（三张新表，`create_all` 直接建，无迁移）

| 表 | 关键字段 |
|---|---|
| `auth_users` | `username`(unique) · `password_hash` · `totp_secret` · `totp_enabled` · `recovery_code_hashes`(一次性) |
| `sessions` | `id`(32B 随机) · `user_id` · `csrf_token` · `expires_at` · `last_seen_at` · `user_agent` · `peer_ip` |
| `api_keys` | `name` · `prefix`(unique，公开 id) · `key_hash` · `last_used_at` · `revoked_at` |

- 口令哈希：`pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>`，`iters=600000`。
- 服务器签名密钥：`data/auth-secret.key`（0600），用于 cookie 签名与（可选）TOTP 密钥加密；
  **不进 config.yaml/.env**。轮换它 = 让所有会话失效。

## 6. 端点

```
GET  /login                      登录页
POST /api/auth/login             口令 → 需 2FA 回 challenge，否则建会话
POST /api/auth/2fa               校验 TOTP → 建会话
POST /api/auth/logout            销毁当前会话
GET  /api/auth/me                当前身份（用户名 / 2FA / 认证方式）
POST /api/auth/bootstrap         首次引导（引导口令 + 新账号）
POST /api/auth/password          改口令（需当前口令）
POST /api/auth/2fa/enroll        生成密钥 + otpauth URI（待验证）
POST /api/auth/2fa/activate      用有效 code 确认开启
POST /api/auth/2fa/disable       关闭（需口令 + code）
POST /api/auth/2fa/recovery      重新生成恢复码
GET  /api/auth/2fa/pending       刷新页面后取回待绑定二维码
GET|POST /api/auth/api-keys      列表（只回 prefix）/ 创建（**仅这一次**回明文）
POST /api/auth/api-keys/{id}/revoke  吊销
```

页面：`/login`（独立渲染，不套 `base.html`，否则登录前每条导航都指向 401）；设置页新增
「访问与鉴权」区（开关、会话时长、可信 CIDR、安全 cookie、改口令、2FA 绑定/恢复码）；
新页「API Key」；`/setup` 向导末尾新增「设置管理员口令」步骤。绑定 2FA 同时给**扫码**与
**手抄密钥 + URI** 两条路。

## 7. 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `web.auth.enabled` | `true` | 关掉 = 只靠可信内网/回环 |
| `web.auth.session_ttl_minutes` | `480` | 会话有效期 |
| `web.auth.secure_cookie` | `false` | 反代有 TLS 时置 `true` |
| `web.auth.login_max_failures` | `5` | 触发退避前允许的失败次数 |
| `web.trusted_networks` | `[]` | 可信 CIDR，如 `["192.168.1.0/24"]`；**裸地址会被拒**（想单机请写 `/32`，否则 `ip_network` 当 `/32` 用而用户以为是网段） |

- `api.auth_required` / `api.token` **已删除**，唯一开关是 `web.auth.enabled`——两个真相来源
  早晚互相矛盾。
- 口令哈希、TOTP 密钥、Key 哈希、会话与签名密钥走 DB 与 `data/`，**绝不进通用配置写入器**。

**反向代理**：CIDR 只看真实对端（`request.client.host`），**默认不信** `X-Forwarded-For`。
确需按真实客户端判定时，打开 `web.trust_proxy` **并且**把代理网段写进 `web.trusted_proxies`
——缺一不可：只填开关等于让任何人加一行 `X-Forwarded-For: 127.0.0.1` 就跳过全部鉴权。
同时满足时只取 `X-Forwarded-For` 的**最右一跳**。

## 8. 旧口令的收场（垫片已于 v1.3.0 移除）

`FISSUE_API_TOKEN` 不再是机制：启动时不再读它、`migrate_legacy_token` 已删、校验只认
`fissue_<prefix>_<secret>` 形状的 Key；`POST /api/auth/legacy-token` 与
`fissue auth key import-legacy` 都不存在。

> 一条垫片等于一条永不过期、绕过吊销与审计的明文口令：留着它的成本是永久性的，收益只到
> 升级那一刻。

迁移做法：`fissue auth key create --name 我的机器`，替换脚本/CI 里的旧口令，再把
`FISSUE_API_TOKEN` 从 `.env` 删掉（表里遗留的 `prefix="legacy"` 行请到「API Key」页吊销）。

## 9. 安全要点与已知边界

PBKDF2-HMAC-SHA256（60 万次迭代 + 随机 16B salt）· TOTP 按 RFC 6238（SHA1 / 6 位 / 30s，
±1 步窗口）· 恢复码 10 个一次性 · 一律 `hmac.compare_digest` · 按 IP + 用户名计数并指数退避 ·
会话服务端可撤销 · CSRF 走 `SameSite=Lax` + 双提交 · 锁定自救用 `fissue auth reset` /
`set-password` · 审计靠 `last_seen_at` / `last_used_at`。

**已知边界**：单进程内存态限速在多进程部署下不共享；CIDR 判定默认不信转发头，「反代 + 可信网」
需显式配两条。多用户按单管理员落地，表结构已留余地，RBAC 待定。

TOTP 二维码由服务端纯标准库生成（`web/qr.py`），不经任何外部服务；对着 `segno` 做过逐位比对
（版本 1~10 × ECC L/M × 8 掩码 × 16 组输入共 232 张矩阵全同）——自动挑掩码的口径可能与其它库
不同，但掩码号写在格式信息里，不影响可解码性。

## 10. 落地偏差与测试

| 偏差 | 理由 |
|---|---|
| 删除 `api.auth_required` 与 `api.token` | 避免同一件事两个真相来源 |
| 登录页独立渲染 | 套 `base.html` 会把用户引到全是 401 的死路 |
| 可信网拒绝裸地址 | `ip_network` 会当 `/32`，用户以为放行整个网段却只放行一台 |
| 转发头补刀按「是否显式声明反代」判定 | `127.0.0.1` + 同机 nginx 时对端就是 `127.0.0.1`，协议层无法区分 |
| 2FA 二维码编码器在服务端 Python | 密钥本就在服务端，Python 版能在 pytest 里离线跑结构与往返断言 |

离线测试覆盖：PBKDF2/TOTP 已知向量、Key 哈希与常数时间比对、会话签发/过期/吊销、CSRF 缺失
返回 403、授权矩阵（{回环, 可信 CIDR, 非回环} × {无身份, 会话, Key, 引导口令} × {读, 写}）、
fail-closed 组合、bootstrap 用后即焚、旧口令残留不被导入。

---

相关：[USAGE.md](USAGE.md) · [ENV.md](ENV.md) · [DESIGN.md §5](DESIGN.md) ·
[wiki/Security.md](wiki/Security.md) · [SECURITY.md](../SECURITY.md)