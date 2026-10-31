# Web 与 REST API

> `fissue web` 同时提供工作台与 REST API。鉴权的完整设计见 [AUTH.md](../AUTH.md)。

## 1. 启动

```bash
fissue web                    # 默认 127.0.0.1:8000
fissue web --host 0.0.0.0 --port 8080
fissue web --reload           # 开发热重载
fissue serve --with-web       # 常驻服务 + Web/API 同进程
```

工作台在 `http://127.0.0.1:8000`，交互式 API 文档在 `/docs`。

## 2. 页面

| 页面 | 作用 |
|---|---|
| **流水线**（首页） | 三种视图看同一条流水线：漏斗（吞吐）/ 看板（各阶段积压 + 批量操作）/ 泳道（单条走到哪）。卡点清单常驻顶部 |
| 条目列表 | 筛选（仓库 / 预设 / 搜索）+ 勾选批量操作 |
| 条目详情 | **垂直时间线**：六阶段各一张卡片，带该阶段证据与挂在阶段上的动作按钮 |
| 状态监测 | 运行状态 + 任务与日志 |
| 总览 / 监控仓库 / 初始化向导 / 设置 | 统计与控制面板 / 登记仓库 / 等价 `onboard` / 配置编辑（写回覆盖层） |
| API Key | 列表 / 创建（**仅此一次**回明文）/ 吊销 |
| 登录 | `/login`：用户名 + 口令 →（开 2FA 则）验证码 |

> 队列判定并入流水线看板，任务并入状态监测；旧地址 `/queues`、`/jobs` 保留 **307 重定向**
> （它们在文档与书签里都有，404 会让人以为功能没了），`/jobs/{id}` 不受影响。

流水线视图的要点：三视图共用**同一份计数**（服务端一条 `GROUP BY`）· 阶段由状态**推导**不落库
（多存一个字段就有第二个真相）· `needs_manual` / `failed` 排最前且**不猜它死在哪个阶段**（状态
不携带该信息，硬猜会画错泳道）· 干预走阶段卡片按钮（重评 / 重验 / 强制修 / 批准合并）。

**强制修复**（⚡）：条目详情页按钮（仅 Issue）或列表页批量；已在修复流程中的条目保持原状。

## 3. 鉴权

| 来源 | 适用 | 说明 |
|---|---|---|
| 回环 | 本机 | 永远免登录 |
| 可信内网（`web.trusted_networks`） | 内网 | 视同回环：免登录、可写配置 |
| 会话 / API Key | 其余（含对外） | 必须持有有效身份 |

- **会话**：`/login`（+ 2FA 同页多步）→ HttpOnly cookie。每会话带 `csrf_token`，前端对非 GET
  带 `X-CSRF-Token`，服务端**只校验会话身份**（Bearer / 回环 / 可信网豁免）。
- **API Key**：`Authorization: Bearer fissue_<prefix>_<secret>`，只存 `sha256`、常数时间比对；
  列表只回 `prefix`/名称/时间，创建时**仅此一次**回明文；吊销是软删（保留审计行）。
- **bootstrap**：首次启动无管理员时生成一次性引导口令（控制台 + `data/auth-bootstrap-token`，
  0600），`POST /api/auth/bootstrap` 用后即焚；没有它时回环可引导、非回环 403。

界面进不去时自救：`fissue auth status / set-password / reset / key …`——能改口令的接口本身要求
先登录，而 WebUI 正是你进不去的那一侧。

```yaml
web:
  host: 127.0.0.1
  read_only: false
  auth: { enabled: true, session_ttl_minutes: 480, secure_cookie: false, login_max_failures: 5 }
  trusted_networks: []        # ["192.168.1.0/24"]
```

裸地址会被**拒绝**（`ip_network` 当 `/32`，想圈网段却只放行一台）；单机请写 `/32`。

**反向代理**：启动检查只看绑定地址、看不见代理——「绑 `127.0.0.1` + 同机 nginx 转出去」同样等于
把写控制面开放。CIDR 只看真实对端，默认不信 `X-Forwarded-For`；未认证带转发头的写请求会 403，
但那只是补刀，**边界始终是令牌**。

## 4. REST API

前缀默认 `/api/v1`。

```bash
BASE=http://127.0.0.1:8000/api/v1

curl $BASE/health
curl $BASE/stats
curl "$BASE/items?category=bug&priority=tier1"
curl "$BASE/items?stage=verify"                 # 阶段（状态的分组）
curl "$BASE/items?lane=issue-bug"               # 流水线（类型 × 分类）
curl "$BASE/items?preset=stuck"                 # 预设（卡住的 / 待评测 / 待验证 / 待修）
curl "$BASE/items/github%3Apsf%2Frequests%2342" # 详情（key 要 URL 编码）
curl $BASE/queues
curl "$BASE/export?format=markdown"
```

`stage` / `preset` / `lane` 三个维度共用同一份口径：阶段是状态的分组（「待验证」含
`evaluated` / `verifier_ready` / `verifying`），预设是阶段的友好别名，流水线是「类型 × 分类」。

操作类（需 `web.read_only=false` **且**具备写身份）：

```bash
curl -X POST $BASE/actions/scan     -H 'Content-Type: application/json' -d '{"repo":"psf/requests"}'
curl -X POST $BASE/actions/evaluate -H 'Content-Type: application/json' -d '{"limit":20}'
curl -X POST $BASE/actions/verify   -H 'Content-Type: application/json' -d '{"limit":10}'
curl -X POST $BASE/queues/flush     -H 'Content-Type: application/json' -d '{"force":true}'
curl -X POST $BASE/actions/fix      -H 'Content-Type: application/json' -d '{"dry_run":true,"limit":5}'

# 强制修复走作业接口（可轮询进度；验证阶段要生成验证器，分钟级）
curl -X POST $BASE/jobs/force    -H 'Content-Type: application/json' -d '{"keys":["github:psf/requests#42"]}'
curl -X POST $BASE/items/unforce -H 'Content-Type: application/json' -d '{"keys":["github:psf/requests#42"]}'

# 人工闸门
curl -X POST "$BASE/actions/approve-fix?key=github%3Apsf%2Frequests%2342" -H 'Content-Type: application/json' -d '{}'
curl -X POST "$BASE/actions/approve-merge?key=github%3Apsf%2Frequests%2399" -H 'Content-Type: application/json' -d '{"note":"已人工确认"}'
```

> `approve-merge` 只打 `merge-approved` 标签 + 留评论，**不会真的合并**。

鉴权后调用：`curl $BASE/stats -H "Authorization: Bearer <API Key>"`。

### 鉴权端点

```
GET  /login                       登录页
POST /api/auth/login              口令 → 需 2FA 回 challenge，否则建会话
POST /api/auth/2fa                校验 TOTP → 建会话
POST /api/auth/logout             销毁当前会话
GET  /api/auth/me                 当前身份
POST /api/auth/bootstrap          首次引导
POST /api/auth/password           改口令（需当前口令）
POST /api/auth/2fa/{enroll,activate,disable,recovery}
GET  /api/auth/2fa/pending        取回待绑定密钥（刷新页面二维码不丢）
GET|POST /api/auth/api-keys       列表 / 创建
POST /api/auth/api-keys/{id}/revoke
GET  /api-keys                    管理页
```

## 5. 入站 webhook

默认是轮询（`schedule.scan_interval_seconds`，1 小时）。接上 webhook 变**事件驱动**：平台一推，
后台立刻跑一轮（抓取 → 评测 → 验证 → flush → 修复）。

```yaml
webhooks: { enabled: true }
```
```bash
FISSUE_WEBHOOK_GITHUB_SECRET=<仓库 Webhook 里填的那个 Secret>
```

| 平台 | Payload URL | 订阅 | 密钥 |
|---|---|---|---|
| GitHub | `…/api/v1/webhooks/github` | Issues、Pull requests | `X-Hub-Signature-256`（HMAC-SHA256） |
| GitLab | `…/api/v1/webhooks/gitlab` | Issue / MR events | `X-Gitlab-Token`（逐字节比对） |
| Gitee | `…/api/v1/webhooks/gitee` | Issue、PR | `X-Gitee-Token` |
| AtomGit | `…/api/v1/webhooks/atomgit` | 同上 | 同上（v5 同形状） |

反应：验签通过且仓库已登记 → `202`，后台跑一轮（同样在写锁内，已有写进程则跳过，连推不会堆叠）；
验签不过 → `403`；仓库未登记 → `202` + `accepted: false` 并写明原因；非条目类事件（如 ping）只记日志。

三个坑：① 端点不带 bearer 鉴权，**密钥就是身份**，别把 API token 当密钥；② 必须能被**平台服务器**
访问到（只绑回环或反代不放行 `/api/v1/webhooks/*` 时永远收不到）；③ 开着 `webhooks.enabled` 却
没给某个已启用平台配密钥会**启动失败**（fail-closed）。

---

相关：[配置参考](Configuration.md) · [部署与运维](Deployment.md) · [安全模型](Security.md) ·
[CLI 参考](CLI-Reference.md) · [AUTH.md](../AUTH.md)