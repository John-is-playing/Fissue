# Web 与 REST API

> `fissue web` 同时提供**Web 工作台**与 **REST API**。本页讲怎么起、有哪些页面/端点、
> 鉴权怎么配、以及入站 webhook 怎么接。鉴权的完整设计见 [docs/AUTH.md](../AUTH.md)。

---

## 1. 启动

```bash
fissue web                          # 默认 127.0.0.1:8000
fissue web --host 0.0.0.0 --port 8080
fissue web --reload                 # 开发模式热重载
fissue serve --with-web             # 常驻服务 + Web/API 同进程
```

打开 `http://127.0.0.1:8000` 即是工作台；交互式 API 文档在 `http://127.0.0.1:8000/docs`。

---

## 2. 页面

| 页面 | 作用 |
|---|---|
| 总览 | 条目统计、队列、用量、沙盒状态 |
| 条目列表 | 筛选（仓库 / 类型 / 分类 / 优先级 / 状态）+ 点击看详情 |
| 条目详情 | 评分理由、证据引用、验证器、执行记录、修复记录 |
| 队列 | 队列状态与 flush 判定 |
| 任务 | 后台作业状态 |
| 监控仓库 | 登记 / 启停要抓的仓库 |
| 状态监测 | 运行状态 |
| 初始化向导 | `onboard` 等价物（含设置管理员口令） |
| 设置 | 配置项编辑（写回覆盖层 `config.local.yaml`） |
| API Key | 列表 / 创建（**仅此一次**回明文）/ 吊销 |
| 登录 | `/login`：用户名 + 口令 →（开了 2FA 则）验证码 |

页面上还有操作按钮：扫描、评测、验证、Flush、试跑修复。

**强制修复**（⚡ 徽标）：不管评测结论如何，把指定条目推进修复流程。

- 条目详情页有「强制修复」按钮（仅 Issue，PR 无自动修复流程）；已强制的条目带 ⚡ 徽标，旁边给「取消强制」；
- 列表页可勾选多条后「强制修复选中」；
- 已在修复流程中的条目强制时保持原状（防重复开 PR）。

---

## 3. 鉴权

### 三种身份来源（互为「或」）

| 来源 | 适用 | 说明 |
|---|---|---|
| **回环**（loopback） | 本机 | 永远免登录 |
| **可信内网**（`web.trusted_networks` 内的 CIDR） | 内网 | 视为等同回环：免登录、可写配置 |
| **会话 / API Key** | 其余（含对外） | 必须持有有效身份 |

判定顺序：先看来源是否回环 / 可信网（免身份）；否则要求**会话 cookie** 或 `Authorization: Bearer <key>`。

### 会话登录 + 2FA

- `/login`（用户名 + 口令）→（2FA 开则）验证码 → 下发 **HttpOnly 会话 cookie**。
- 2FA 步骤**复用同一页**的多步状态，不做三个路由。
- 每个会话带 `csrf_token`，前端对所有**非 GET** 请求带 `X-CSRF-Token`；服务端**只对会话身份**校验（Bearer / 回环 / 可信网豁免）。
- cookie 用 `SameSite=Lax` + `HttpOnly`；`web.auth.secure_cookie=true` 时加 `Secure`。

### API Key

```
Authorization: Bearer fissue_<prefix>_<secret>
```

- 只存 `sha256`，常数时间比对；列表只回 `prefix` / 名称 / 时间，**绝不含密钥**；创建时**仅此一次**回明文。
- 吊销是软删（保留审计行）。库里没有明文，「忘了」只能吊销重建。

### 首次引导（bootstrap）

- 首次启动 `fissue web` / `serve` 且**无管理员账号**时：生成**一次性引导口令**，打印到控制台，
  并写入 `data/auth-bootstrap-token`（`0600`）。
- `POST /api/auth/bootstrap` 用它 + 新用户名/口令创建管理员；**用后即焚**（删文件）。
- 没有它时：回环可引导（本机部署），非回环一律 403，并给出「怎么拿引导口令」的指引。

### 界面进不去时的自救

```bash
fissue auth status
fissue auth set-password
fissue auth reset                  # 清 2FA / 重置口令
fissue auth key list|create|revoke
fissue auth key import-legacy
```

理由很简单：能改口令的接口本身要求先登录，而 WebUI 正是你进不去的那一侧。

### 关键配置

```yaml
web:
  host: 127.0.0.1              # 默认仅本机
  read_only: false             # true 则隐藏所有操作按钮，API 动作类返回 403
  auth:
    enabled: true              # 唯一总开关
    session_ttl_minutes: 480
    secure_cookie: false       # 反代已提供 HTTPS 时置 true
    login_max_failures: 5
  trusted_networks: []         # 例如 ["192.168.1.0/24", "10.0.0.0/8"]
```

> 裸地址会被**拒绝**：`ip_network` 把 `192.168.1.0` 当 `/32`，用户想圈网段却只放行一台机器，
> 而任何地方都不会提示。要单机请显式写 `/32`。

### 反向代理

启动期那道 fail-closed 检查**只能看绑定地址、看不见代理**。「绑 `127.0.0.1` + 同机 nginx 转出去」
同样等于把无鉴权的写控制面（`/api/v1/queues/flush`、`/api/v1/actions/*`）开放出去。
所以：**挂了反代就声明可信网段（`web.trusted_networks`）或保持 `web.auth.enabled: true`**。

CIDR 判定**只看真实对端地址**（`request.client.host`），**默认不信任** `X-Forwarded-For`。
未认证、带转发头、又不在可信网的**写请求**会被 403——但那只是补刀，**边界始终是令牌**。

---

## 4. REST API

前缀默认 `/api/v1`（`api.prefix`）。

```bash
BASE=http://127.0.0.1:8000/api/v1

curl $BASE/health                                    # 健康检查
curl $BASE/stats                                     # 概览统计
curl "$BASE/items?category=bug&priority=tier1"        # 列表 + 筛选
curl "$BASE/items/github%3Apsf%2Frequests%2342"      # 详情（key 要 URL 编码）
curl $BASE/queues                                    # 队列状态与 flush 判定
curl "$BASE/export?format=markdown"                  # 导出
```

操作类（需要 `web.read_only=false` **且**具备写身份）：

```bash
curl -X POST $BASE/actions/scan    -H 'Content-Type: application/json' -d '{"repo":"psf/requests"}'
curl -X POST $BASE/actions/evaluate -H 'Content-Type: application/json' -d '{"limit":20}'
curl -X POST $BASE/actions/verify   -H 'Content-Type: application/json' -d '{"limit":10}'
curl -X POST $BASE/queues/flush     -H 'Content-Type: application/json' -d '{"force":true}'
curl -X POST $BASE/actions/fix      -H 'Content-Type: application/json' -d '{"dry_run":true,"limit":5}'

# 强制修复：走作业接口（可轮询进度；验证阶段要生成验证器，分钟级）
curl -X POST $BASE/jobs/force      -H 'Content-Type: application/json' \
     -d '{"keys":["github:psf/requests#42"]}'
curl -X POST $BASE/items/unforce   -H 'Content-Type: application/json' \
     -d '{"keys":["github:psf/requests#42"]}'      # 取消强制

# 人工闸门
curl -X POST "$BASE/actions/approve-fix?key=github%3Apsf%2Frequests%2342" -d '{}' -H 'Content-Type: application/json'
curl -X POST "$BASE/actions/approve-merge?key=github%3Apsf%2Frequests%2399" \
     -H 'Content-Type: application/json' -d '{"note":"已人工确认"}'
```

> `approve-merge` 只是打 `merge-approved` 标签 + 留评论，**不会真的合并**——合并始终由你在平台上手动操作。

鉴权后调用：

```bash
curl $BASE/stats -H "Authorization: Bearer <你的 API Key>"
```

### 鉴权端点

```
GET  /login                             登录页
POST /api/auth/login                    用户名+口令 → 需 2FA 则回 challenge，否则建会话
POST /api/auth/2fa                      校验 TOTP（challenge + code）→ 建会话
POST /api/auth/logout                   销毁当前会话
GET  /api/auth/me                       当前身份（用户名 / 是否开 2FA / 认证方式）
POST /api/auth/bootstrap                首次引导（引导口令 + 新账号）
POST /api/auth/password                 改口令（需当前口令）
POST /api/auth/2fa/enroll               生成 TOTP 密钥 + otpauth URI（待验证）
POST /api/auth/2fa/activate             用一次有效 code 确认开启
POST /api/auth/2fa/disable              关闭 2FA（需口令 + code）
POST /api/auth/2fa/recovery             重新生成恢复码（需口令）
GET  /api/auth/2fa/pending              取回「已生成、还没确认」的密钥（刷新页面二维码不丢）
GET  /api/auth/api-keys                 列出（只回 prefix/名称/时间）
POST /api/auth/api-keys                创建（仅此一次回明文）
POST /api/auth/api-keys/{id}/revoke    吊销
GET  /api-keys                          管理页（列表 / 创建 / 吊销；已并入左侧导航）
POST /api/auth/legacy-token            把旧口令手工导入成一条 API Key（兼容垫片，后续版本移除）
```

---

## 5. 入站 webhook

常驻服务默认是**轮询**（`schedule.scan_interval_seconds`，默认 1 小时）。接上 webhook 变成**事件驱动**：
平台一推，后台立刻跑一轮（抓取 → 评测 → 验证 → flush → 修复）。

```yaml
# config.yaml
webhooks:
  enabled: true
```

```bash
# .env：每个已启用平台都必须配（密钥与平台 Token 是两回事，别复用）
FISSUE_WEBHOOK_GITHUB_SECRET=<仓库 Webhook 里填的那个 Secret>
```

| 平台 | Payload URL | 订阅事件 | 密钥 |
|---|---|---|---|
| GitHub | `https://你的域名/api/v1/webhooks/github` | Issues、Pull requests | Secret（`X-Hub-Signature-256`，HMAC-SHA256） |
| GitLab | `…/api/v1/webhooks/gitlab` | Issue events、Merge request events | Secret token（`X-Gitlab-Token`，逐字节比对） |
| Gitee | `…/api/v1/webhooks/gitee` | Issue、Pull Request | Webhook 密码（`X-Gitee-Token`） |
| AtomGit | `…/api/v1/webhooks/atomgit` | 同上 | 同上（v5 同形状） |

**它会怎么反应**：

- 验签通过且仓库**已登记** → 回 `202`，并在**后台**跑一轮；这一轮**同样在写锁内**，
  已有写进程在跑时直接跳过（下一轮扫描会把它带上），所以连着推几次不会堆叠。
- 验签不过 → `403`。
- 仓库没登记 → `202` 但 `accepted: false` 并写明原因。
- 不是条目类事件（如 GitHub 的 ping）→ 只记一条日志。

> **三件最容易踩的**：
> 1. 这个端点**不带 bearer 鉴权**（平台不会带令牌），**密钥就是它的身份证明**——也别把 API token 当 webhook 密钥。
> 2. 它必须能被**平台服务器**访问到：只绑回环、或反代不放行 `/api/v1/webhooks/*`，推送永远到不了（判断方法很直接：日志里一条 webhook 都没有）。
> 3. 开着 `webhooks.enabled` 却没给某个已启用平台配密钥，**启动就会失败**（fail-closed），免得端点对外裸奔。

---

## 相关页

[配置参考](Configuration.md) · [部署与运维](Deployment.md) · [安全模型](Security.md) · [CLI 参考](CLI-Reference.md) · [docs/AUTH.md](../AUTH.md)
