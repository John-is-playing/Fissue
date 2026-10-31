# 部署与运维

> 镜像、沙盒执行、常驻服务、反代、webhook 与数据。安全前提见[安全模型](Security.md)。

## 1. 镜像

| 镜像 | 路径 | 作用 |
|---|---|---|
| `fissue/sandbox-base:latest` | `docker/sandbox/Dockerfile` | 基于 `python:3.12-slim-bookworm`，预装 Python/Node/Go/Java/Ruby 与 pytest/tox/jest/mocha；非 root 用户 `sandbox`，工作目录 `/workspace`，**无固定 ENTRYPOINT**（运行时用 `sh -lc "<command>"`） |
| Fissue 自身镜像 | `docker/self/Dockerfile` | 多阶段，继承沙盒镜像，装运行时与 dev 依赖；`PYTHONPATH=/workspace/src` 让 `import fissue` 用挂载进来的源码 |

```bash
docker build -t fissue/sandbox-base:latest docker/sandbox
fissue sandbox check
```

> `fissue verify` 跑整批前会做**镜像存在性检查**：只查存在性、不触发拉取（拉取失败前要空等约 20s），
> 缺镜像时毫秒级拦下并给出 `docker build` 命令。

## 2. 沙盒执行

```
docker run --rm --name <容器名> --label ...
  --network none --read-only --tmpfs /tmp
  --security-opt no-new-privileges --cap-drop ALL
  --user <uid:gid>                # 留空跟随宿主，容器要能写进工作区
  --cpus <N> --memory <MB> --memory-swap <MB> --pids-limit <N>
  -v <source>:<target>:ro|rw
  sh -lc "<command>"
```

工作区挂的是**隔离卷**（`volume`，可选 `loopback`），`.git` **只读**（`index` 除外），**绝不挂宿主
普通目录**。超时后显式 `docker rm -f`；单流输出上限 **2MB**，超出丢弃并标注「已截断」。

| 模式 | 何时 | 差异 |
|---|---|---|
| `inline`（默认） | 未起转发组件、非 POSIX、socket 不存在 | Docker 句柄在调用方进程内 |
| `server` | 起了 `fissue sandbox serve` | 调用方不持有句柄，只走受控 socket 通道 |

```bash
fissue sandbox serve                                  # 监听 /var/run/fissue-runner.sock
FISSUE_SANDBOX_RUNNER_SOCKET=/run/fissue-runner.sock  # 等价 sandbox.forwarder.socket
```

**Windows 不支持 Unix socket 转发**，会自动回退 inline。

## 3. 常驻服务

```bash
fissue serve              # 扫描 + 调度 + 验证 + 通知
fissue serve --with-web   # 同进程带起 Web/API（uvicorn）
fissue serve --once       # 只跑一轮（cron / 任务计划）
```

| 循环 | 默认间隔 | 做什么 |
|---|---|---|
| 扫描 | 3600s | 增量抓取 → 评测 → **验证** → 高重要性告警 |
| flush | 60s | 按条件 flush → 打标签 → 派发修复 |
| 修复 | 120s | 修 tier1/tier2 的 Issue（受预算与策略限制） |

三循环互不阻塞（由 `schedule.enabled` 控制），收到停止信号优雅退出。

`systemd/fissue.service` 已提供：依赖 `network-online.target` 与 `docker.service`，非 root 运行，
`ExecStart=.venv/bin/fissue serve --with-web --config ... --env ...`，`Restart=on-failure`、
`RestartSec=15`、`KillSignal=SIGTERM`、`TimeoutStopSec=90`，并加固 `NoNewPrivileges` / `PrivateTmp`。

```bash
# 外部 cron
0 * * * * cd /path/to/Fissue && fissue serve --once >> /var/log/fissue.log 2>&1
```

## 4. 反向代理与鉴权

- **启动检查只能看绑定地址、看不见代理**。「绑 `127.0.0.1` + 同机 nginx 转出去」同样等于把写控制面
  （`/api/v1/queues/flush`、`/api/v1/actions/*`）开放。
- 因此：挂反代就声明 `web.trusted_networks`，或保持 `web.auth.enabled: true`（默认即开）。
- 反代提供 HTTPS 时置 `web.auth.secure_cookie: true`。
- CIDR 只看真实对端，默认不信 `X-Forwarded-For`；未认证带转发头的写请求 403，但那是补刀不是边界。
- 首次部署到 `/login` 用引导口令建管理员（或 `/setup` 向导末尾设置口令）。
- `web.read_only: true` 隐藏操作按钮，API 动作类返回 403。

## 5. 入站 webhook

```yaml
webhooks: { enabled: true }
```
```bash
FISSUE_WEBHOOK_GITHUB_SECRET=<GitHub Webhook 里填的 Secret>
```

| 平台 | Payload URL | 事件 | 密钥头 |
|---|---|---|---|
| GitHub | `…/api/v1/webhooks/github` | Issues、Pull requests | `X-Hub-Signature-256`（HMAC-SHA256） |
| GitLab | `…/api/v1/webhooks/gitlab` | Issue / MR events | `X-Gitlab-Token` |
| Gitee | `…/api/v1/webhooks/gitee` | Issue、PR | `X-Gitee-Token` |
| AtomGit | `…/api/v1/webhooks/atomgit` | 同上 | 同上 |

验签通过且仓库已登记 → 202 + 后台跑一轮（写锁内，不堆叠）；验签不过 403；仓库未登记 202 +
`accepted: false`；非条目事件只记日志。**必须能被平台服务器访问到**（只绑回环或反代不放行
`/api/v1/webhooks/*` 时永远收不到，日志里一条都没有）；开着却没配某平台密钥会**启动失败**。

## 6. 数据库与数据

- 生产用 **PostgreSQL**：先 `createdb -U fissue fissue` 再 `fissue db init`。已有 MySQL 运维栈的
  也可用 **MySQL 8.0+**（库/表 utf8mb4；索引宽度与长文本列已按 MySQL 限制落地），完整链路
  `db init → fetch → eval → verify → flush → fix` 已在 Docker `mysql:8.0` 上验证。
- 表由 `create_all` 直接建，**不需要迁移脚本**。
- 产物在 `app.data_dir`（默认 `./data`）：`artifacts/`、`manual/`、`patches/`、`exports/`、
  `benchmarks/`；其中 `data/benchmarks/` 是**唯一进版本库**的子目录。
- `data/auth-secret.key`（会话 cookie 的 HMAC 密钥）与 `data/auth-bootstrap-token` 均 `0600`，
  **不进** config.yaml / .env；轮换 `auth-secret.key` = 让所有会话失效。

## 7. 成本与限流

```yaml
llm:
  concurrency: 4
  budget: { daily_tokens_per_repo: 2000000, daily_usd_per_repo: 5.0, hard_stop: true }
platforms:
  label_cache_ttl_seconds: 600       # 标签列表缓存；常驻长跑不能永不过期（会反复 422）
  max_retry_after_seconds: 300       # 被限流时单次最多等多久（封顶 GitHub 一小时的 reset）
  max_retries: 4                     # 平台 HTTP 重试，与 llm.max_retries 无关
```

`fissue status` 查看当日用量；跑分对比见[开发与发布](Development.md)。

---

相关：[安全模型](Security.md) · [配置参考](Configuration.md) · [Web 与 REST API](Web-and-API.md) ·
[故障排查](Troubleshooting.md) · [SECURITY.md](../../SECURITY.md)