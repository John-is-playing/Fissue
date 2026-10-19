# 部署与运维

> 生产部署涉及的镜像、服务、转发组件、反向代理与 webhook。安全前提见[安全模型](Security.md)。

---

## 1. 镜像

仓库提供两个 Dockerfile：

| 镜像 | 路径 | 作用 |
|---|---|---|
| **沙盒基础镜像** `fissue/sandbox-base:latest` | `docker/sandbox/Dockerfile` | 基于 `python:3.12-slim-bookworm`，预装 Python / Node / Go / Java / Ruby 及 `pytest` / `tox` / `jest` / `mocha` 等测试框架；创建非 root 用户 `sandbox`，工作目录 `/workspace`。**无固定 ENTRYPOINT**——由 Fissue 运行时通过 `sh -lc "<command>"` 指定 |
| **Fissue 自身镜像** | `docker/self/Dockerfile` | 分层/多阶段构建，继承 `fissue/sandbox-base`，从 `pyproject.toml` 安装运行时与 dev/test 依赖；设 `PYTHONPATH=/workspace/src`，确保容器内 `import fissue` 用**挂载进来的源码**而非镜像内旧副本 |

构建沙盒镜像（验证必需，容器默认禁网、装不了依赖）：

```bash
docker build -t fissue/sandbox-base:latest docker/sandbox
fissue sandbox check
```

> **前置检查**：`fissue verify` 现在会在跑整批前做一次**基础镜像存在性检查**——只查存在性、
> **不触发拉取**（拉取失败前要空等约 20 秒超时）；缺镜像时毫秒级拦下整批并给出 `docker build` 命令。

---

## 2. 沙盒执行

### 容器启动参数（真实使用的）

```
docker run --rm --name <容器名> --label ...
  --network none                  # 禁网
  --read-only                     # 根只读
  --tmpfs /tmp                    # 受限可写层
  --security-opt no-new-privileges
  --cap-drop ALL
  --user <uid:gid>                # 非 root；留空则跟随宿主，容器要能写进工作区
  --cpus <N>  --memory <MB>  --memory-swap <MB>  --pids-limit <N>
  -v <source>:<target>:ro|rw      # 按 MountSpec.read_only 决定
  sh -lc "<command>"
```

工作区挂载：`.git` 以**只读**方式挂（`index` 除外），其余 rw；挂的是**隔离卷**（`volume`，可选 `loopback`），
**绝不挂宿主普通目录**。超时后显式 `docker rm -f` 回收容器；单流输出上限 **2MB**，超出丢弃并标注「已截断」。

### 两种执行模式

| 模式 | 何时 | 差异 |
|---|---|---|
| `inline`（默认） | 未起转发组件、非 POSIX 平台、socket 文件不存在 | Docker 句柄就在调用方进程内 |
| `server` | 起了 `fissue sandbox serve` | 调用方**不持有 Docker 句柄**，只走受控本地 socket 通道 |

容器隔离参数两种模式**一致**；差别在「句柄在谁手里」。

### 转发组件

```bash
fissue sandbox serve                     # 监听 /var/run/fissue-runner.sock
FISSUE_SANDBOX_RUNNER_SOCKET=/run/fissue-runner.sock   # 等价 sandbox.forwarder.socket
```

- 默认 socket：`cfg.forwarder.socket`（默认 `/var/run/fissue-runner.sock`）。
- **非 Windows** 使用 Unix domain socket（`AF_UNIX`）；**Windows 不支持 Unix socket 转发**，
  会报错并提示改用 inline 模式——实际会自动回退 inline。

---

## 3. 常驻服务

```bash
fissue serve                 # 常驻：增量扫描 + 队列调度 + 阈值通知（也含验证）
fissue serve --with-web      # 同进程带起 Web/API（uvicorn）
fissue serve --once          # 只跑一轮就退出（适合系统 cron / 任务计划）
```

三个独立循环，互不阻塞（由 `schedule.enabled` 决定是否启动）：

| 循环 | 默认间隔 | 做什么 |
|---|---|---|
| 扫描 | 每小时（`scan_interval_seconds: 3600`） | 增量抓取 → 评测新条目 → **验证**（生成验证器 + F2P）→ 高重要性告警 |
| flush | 60 秒（`queue_flush_interval_seconds`） | 按触发条件 flush 队列 → 打标签 → 派发修复 |
| 修复 | 120 秒 | 修 tier1/tier2 的 Issue，受预算与策略限制 |

收到停止信号后优雅停止。`--with-web` 时另起 uvicorn 任务。

### systemd 单元

仓库提供 `systemd/fissue.service`：

- 依赖 `network-online.target` 与 `docker.service`；
- 以某个非 root 用户运行，工作目录 `~/Fissue`；
- `ExecStart=.venv/bin/fissue serve --with-web --config ... --env ...`；
- `Restart=on-failure`、`RestartSec=15`、`KillSignal=SIGTERM`、`TimeoutStopSec=90`；
- 加固：`NoNewPrivileges=true`、`PrivateTmp=true`。

### 外部 cron（不常驻）

```bash
# Linux crontab：每小时扫一轮
0 * * * * cd /path/to/Fissue && fissue serve --once >> /var/log/fissue.log 2>&1
```

```powershell
# Windows 任务计划程序：actions 填
powershell -c "cd E:\Fissue; fissue serve --once"
```

---

## 4. 反向代理与鉴权

鉴权模型是**会话登录 + 多 API Key + 可信内网**（见 [Web 与 REST API](Web-and-API.md#3-鉴权)）。部署要点：

- **启动期检查只能看绑定地址、看不见代理**。「绑 `127.0.0.1` + 同机 nginx 转出去」同样等于把无鉴权的
  写控制面（`/api/v1/queues/flush`、`/api/v1/actions/*`）开放出去。
- 因此：**挂了反代就声明可信网段**（`web.trusted_networks`），或保持 `web.auth.enabled: true`（默认即开）。
- 反代提供 HTTPS 时置 `web.auth.secure_cookie: true`（给会话 cookie 加 `Secure`）。
- CIDR 判定**只看真实对端地址**（`request.client.host`），**默认不信任** `X-Forwarded-For`。
  未认证、带转发头、又不在可信网的**写请求**会被 403——那是补刀，不是边界。
- 首次部署：启动后到 `/login` 用**引导口令**创建管理员；或用 `/setup` 向导末尾的「设置管理员口令」步骤。
- `web.read_only: true` 可隐藏所有操作按钮，API 动作类返回 403。

---

## 5. 接入入站 webhook

```yaml
# config.yaml
webhooks:
  enabled: true
```

```bash
# .env：每个已启用平台都必须配（密钥与平台 Token 是两回事）
FISSUE_WEBHOOK_GITHUB_SECRET=<GitHub Webhook 里填的 Secret>
```

| 平台 | Payload URL | 事件 | 密钥头 / 算法 |
|---|---|---|---|
| GitHub | `https://你的域名/api/v1/webhooks/github` | Issues、Pull requests | `X-Hub-Signature-256`，HMAC-SHA256 |
| GitLab | `…/api/v1/webhooks/gitlab` | Issue events、MR events | `X-Gitlab-Token`，逐字节比对 |
| Gitee | `…/api/v1/webhooks/gitee` | Issue、PR | `X-Gitee-Token` |
| AtomGit | `…/api/v1/webhooks/atomgit` | 同上 | 同上（v5 同形状） |

- 验签通过且仓库**已登记** → `202`，后台跑一轮（在写锁内，不堆叠）。
- 验签不过 → `403`；仓库没登记 → `202` 但 `accepted: false`；非条目事件 → 只记日志。
- **必须能被平台服务器访问到**：只绑回环、或反代不放行 `/api/v1/webhooks/*` 都会让推送永远到不了
  （判断方法：日志里一条 webhook 都没有）。
- 开着 `webhooks.enabled` 却没给某个已启用平台配密钥，**启动就失败**（fail-closed）。

---

## 6. 数据库与数据

- 生产用 **PostgreSQL**（`FISSUE_DATABASE_URL=postgresql+psycopg://...`），先 `createdb -U fissue fissue` 再 `fissue db init`。
- 表由 `create_all` 直接建，**不需要迁移脚本**（新增表同理）。
- 产物件落在 `app.data_dir`（默认 `./data`）：`artifacts/`、`manual/`、`patches/`、`exports/`、`benchmarks/`。
  其中 `data/benchmarks/` 是**唯一进版本库**的子目录。
- `data/auth-secret.key`（会话 cookie 的 HMAC 密钥）与 `data/auth-bootstrap-token` 均为 `0600`，
  **不进** `config.yaml` / `.env`。轮换 `auth-secret.key` = 让所有会话失效。

---

## 7. 成本与限流（运维面）

```yaml
llm:
  concurrency: 4
  budget: { daily_tokens_per_repo: 2000000, daily_usd_per_repo: 5.0, hard_stop: true }
platforms:
  label_cache_ttl_seconds: 600       # 标签列表缓存；常驻长跑不能永不过期（会反复 422）
  max_retry_after_seconds: 300       # 被限流时单次最多等多久（封顶 GitHub 一小时的 reset）
  max_retries: 4                     # 平台 HTTP 重试，与 llm.max_retries 无关
```

查看当日用量：`fissue status`。跑分基线对比：见[开发与发布 §跑分基线](Development.md#7-跑分基线fissue-bench)。

---

## 相关页

[安全模型](Security.md) · [配置参考](Configuration.md) · [Web 与 REST API](Web-and-API.md) · [故障排查](Troubleshooting.md) · [SECURITY.md](../../SECURITY.md)
