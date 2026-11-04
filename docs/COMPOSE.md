# 用 Docker Compose 起服务（不需要装 Python）

面向「只想把界面跑起来看看」的人：本机只要有 **Docker + Docker Compose v2**
（`docker compose`，不是旧的 `docker-compose`），剩下的都在容器里。

想在本机装 Python 环境开发，看 [USAGE.md](USAGE.md)；两者配置口径一致（同一套
`.env` + `config.yaml` + `config.local.yaml` 优先级），区别只是文件落在卷里。

---

## 1. 三条命令

```bash
cp .env.example .env          # ① 复制环境变量模板
vi .env                       # ② 填 FISSUE_LLM_API_KEY（**可留空**，进界面再填）
docker compose up -d          # ③ 起服务（首次会构建镜像，约 2~4 分钟）
```

打开 <http://localhost:8000/login>：

* 还没有管理员账号时，服务启动会打印**一次性引导口令**，两种取法：
  ```bash
  docker compose logs fissue | grep -A3 引导口令
  docker compose exec fissue cat /data/auth-bootstrap-token
  ```
* 用它在 `/login` 页创建第一个管理员（用户名 + 口令，可再开 2FA）。
  引导口令**用后即焚**，用完文件就没了。

看状态 / 看日志：

```bash
docker compose ps                    # STATUS 里出现 (healthy) 就说明 /api/v1/health 通了
docker compose logs -f fissue
```

两条路的差别（不用你改命令，入口脚本自动选）：

| LLM key | 启动的模式 | 效果 |
|---|---|---|
| 空 | `fissue web` | 只有 Web 工作台 + REST API，不跑轮次（点页面上的按钮仍然能用） |
| 已填 | `fissue serve --with-web` | 上面那些 + 常驻流水线（定时扫描 / 评测 / 验证 / 通知） |

想固定成某一种：在 `.env` 里写 `FISSUE_MODE=web`（或 `serve`），再 `docker compose up -d`。
在界面「设置」页填 key 之后需要 `docker compose restart fissue` 才会切到流水线模式。

---

## 2. 端口与监听地址

| 变量（写在 `.env`） | 默认 | 说明 |
|---|---|---|
| `FISSUE_WEB_PORT` | `8000` | 容器内监听端口**与**宿主映射端口同时改（两处用同一个值） |
| `FISSUE_BIND_ADDR` | `0.0.0.0` | 宿主侧绑定地址；改 `127.0.0.1` 就只允许本机访问 |

容器内固定监听 `0.0.0.0`（绑回环则宿主映射不到），所以**访问控制靠会话鉴权**：
`web.auth.enabled` 默认就是 `true`，首次访问必须走 `/login`。把服务暴露到公网前请读
[AUTH.md](AUTH.md) 与 [SECURITY.md](../SECURITY.md)；反代场景还要配 `web.trust_proxy` /
`web.trusted_proxies`，否则来源判定会失真。

---

## 3. 数据卷：在哪、备份什么

唯一持久化的位置是卷 `fissue_fissue-data`，容器内挂载点 `/data`：

| 容器内路径 | 内容 |
|---|---|
| `/data/fissue.db` | SQLite 数据库（条目、评分、验证与修复记录）；换 PostgreSQL/MySQL 见 §6 |
| `/data/auth-secret.key` | 会话 cookie 的签名密钥（0600；丢了等于所有人被登出） |
| `/data/auth-bootstrap-token` | 一次性引导口令（用后即焚） |
| `/data/.env` | 界面「设置」页写下的密钥（镜像把 `FISSUE_ENV` 指到这里，容器重建不丢） |
| `/data/config.local.yaml` | 界面写下的业务配置（仓库列表、阈值…）；`config.yaml` 那份人写的不会被它改动 |
| `/data/artifacts/` `/data/patches/` `/data/manual/` `/data/exports/` | 沙盒执行日志、补丁、需人工处理的报告、导出的报表 |

卷在宿主上的实际目录：

```bash
docker volume inspect fissue_fissue-data -f '{{ .Mountpoint }}'
```

备份 / 恢复（停不停服务都可以，恢复前先停）：

```bash
# 备份
docker run --rm -v fissue_fissue-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/fissue-data-$(date +%F).tar.gz -C /data .

# 恢复
docker compose down
docker run --rm -v fissue_fissue-data:/data -v "$PWD":/backup alpine \
  sh -c 'find /data -mindepth 1 -delete && tar xzf /backup/fissue-data-2026-01-01.tar.gz -C /data'
docker compose up -d
```

想把数据直接放在宿主的某个目录（方便 rsync / 快照）：把 `compose.yaml` 里
`fissue-data:/data` 改成 `./data:/data` 即可。目录若不存在，Docker 会建成 root 所有，
**不用手工 chown**——入口脚本启动时会修正属主再降权运行。

---

## 4. 升级

镜像是**从本仓库源码构建**的（`docker/runtime/Dockerfile`，目前没有官方预构建镜像），
所以升级是「拉代码 + 重建」而不是 `docker compose pull`：

```bash
git pull
docker compose up -d --build
```

数据库表由 `create_all` 增量创建（不跑迁移脚本），升大版本前建议先按 §3 备份一次。
回滚：`git checkout <tag> && docker compose up -d --build`（数据卷不动）。

改了 `docker/runtime/container.yaml`（镜像里的 `/app/config.yaml`）或 `pyproject.toml`，
也要带 `--build` 才会生效。

---

## 5. 沙盒 profile（默认关闭，可选）

### 为什么默认不开

验证器 / 回归门 / 自动修复都要在隔离容器里跑代码，而那需要**宿主 Docker**。
默认 profile 里的服务镜像既没有 `docker` 命令，也不挂 `/var/run/docker.sock`——于是沙盒
以「未找到 docker 命令」明确失败（不会静默降级成本机执行，那等于把隔离承诺架空）。

要真正用起来，就多起一个**只干这件事**的容器 `fissue-sandbox-runner`（转发组件），
它是整个方案里唯一接触 Docker 的容器。

> ⚠️ 挂进 `docker.sock` 等于把宿主 Docker 的控制权交给那个容器：能起特权容器、能挂任何
> 宿主目录，实质等于宿主 root。**只在你自己的可信机器上开**，不要用在别人也能访问的部署里。

### 启用步骤

```bash
# ① 宿主上构建沙盒基础镜像（一次性，3~8 分钟；容器默认禁网，所以依赖必须在构建期装好）
docker build -t fissue/sandbox-base:latest docker/sandbox

# ② 找到宿主 docker 组的 gid（容器里的普通用户要靠它才能访问 docker.sock）
stat -c '%g' /var/run/docker.sock        # 例如 999；macOS：ls -ln /var/run/docker.sock

# ③ 写进 .env 并带 profile 起
echo 'DOCKER_GID=999' >> .env
docker compose --profile sandbox up -d

# ④ 确认转发组件就绪
docker compose ps
docker compose logs fissue-sandbox-runner | tail -20
docker compose exec fissue-sandbox-runner fissue sandbox check
```

`fissue sandbox check` 在**转发组件容器里**跑才有意义（那里才有 docker 命令）；在服务容器
里跑会如实告诉你「未找到 docker 命令」。

关闭：`docker compose --profile sandbox down` 之后再用不带 profile 的 `docker compose up -d`。

### 为什么两边都要挂「同一个路径」

容器里的 docker 命令只是把参数转给**宿主**的守护进程，而守护进程只认**宿主**路径。
验证用的是「服务端克隆出来的工作副本」，所以那个目录必须以**逐字相同的绝对路径**同时出现
在服务容器、转发组件容器和宿主上。`compose.yaml` 用同一个变量做了这件事：

```yaml
FISSUE_SANDBOX_WORKSPACE: "${FISSUE_SANDBOX_WORKSPACE:-/tmp/fissue-sandbox}"
# volumes: - ${FISSUE_SANDBOX_WORKSPACE}:/${FISSUE_SANDBOX_WORKSPACE}
```

`/tmp` 在 Linux 与 Docker Desktop 的默认共享目录里都有，一般不用改。若你的环境不允许
（比如桌面版只共享了家目录），改成 `FISSUE_SANDBOX_WORKSPACE=/home/me/fissue-ws`
（**必须绝对路径**）。这个目录不可写时服务不会崩，只是沙盒不可用，日志里会说明原因。

---

## 6. 常见问题

**端口被占用 / 打不开页面**
`Error ... bind: address already in use` → 在 `.env` 里改 `FISSUE_WEB_PORT=8080`，
然后 `docker compose up -d`（映射和容器内监听会一起变）。只本机访问：
`FISSUE_BIND_ADDR=127.0.0.1`。容器起来了但页面打不开，看 `docker compose ps` 是否健康、
`docker compose logs fissue` 里有没有 `Uvicorn running on`。

**权限 / Permission denied（写 /data 失败）**
数据卷首次创建时会继承镜像里 `/data` 的属主（uid 1000），入口脚本启动时也会再修一次；
正常路径**不需要**你手工 chown。仍然失败时按顺序查：

```bash
docker compose exec fissue ls -ln /data             # 属主应当是 1000
docker compose exec -u 1000 fissue sh -c 'touch /data/.probe && rm /data/.probe'
docker volume inspect fissue_fissue-data -f '{{ .Mountpoint }}'
```

> `docker compose exec` 默认以**镜像用户**（root）进入容器，而服务本体是入口脚本降权后的
> uid 1000；所以要测「服务写得进去吗」必须带 `-u 1000`，否则连 root 也算通过。

宿主的 SELinux（Fedora / RHEL / CentOS）会给 bind 目录打标签，需要给挂载加 `:z`
（把 `compose.yaml` 里 `./data:/data` 改成 `./data:/data:z`）。也**不要**把 `/data` 放到
NFS / SMB 上：SQLite 在那种文件系统上会锁表失败。

**`attempt to write a readonly database` / 数据库只读**
九成是 `/data` 不可写（上一条）。另一处常见原因是连接串写成了**相对路径**：
`sqlite:///./data/fissue.db`（三斜杠）在容器里等于 `/app/data/...`，会落进镜像层、
容器重建即丢；容器内的正确写法是四斜杠的绝对路径 `sqlite:////data/fissue.db`
（`compose.yaml` 已经这么设了，且环境变量优先于 `.env`，不必再改 `.env`）。
两者都正常还报只读，就看 `/data` 是不是落在了 NFS / SMB / 只读 bind 上
（SQLite 需要文件锁；`docker inspect` 里看 `RW` 是否为 `true`）。

**LLM key 留空会怎样**
服务照常起来（模式 `web`：界面 + API 都在，只是不跑轮次）。之后两种填法：
① 界面「设置」页填 → 落在 `/data/.env`；② 改宿主 `.env`。改完 `docker compose restart fissue`。
⚠️ 注意优先级：宿主的 `.env` 通过 `env_file` 变成容器的**进程环境**，它压过 `/data/.env`。
所以如果宿主 `.env` 里还留着 `.env.example` 的占位 `FISSUE_LLM_API_KEY=sk-your-key-here`，
界面里填的 key 不会生效——把那行清空成 `FISSUE_LLM_API_KEY=`，只在一处填。
另外别在没 key 时硬设 `FISSUE_MODE=serve`：`fissue serve` 加载配置就要求 key，缺了直接退出，
容器会一直重启（日志里是「缺少 LLM API Key」）。

**想换 PostgreSQL / MySQL**
改 `compose.yaml` 里的 `FISSUE_DATABASE_URL`（或把它注释掉、在 `.env` 里写
`FISSUE_DATABASE_URL=postgresql+psycopg://user:pw@host:5432/db`；容器里的 `host` 不是
`localhost`，用宿主 IP 或 compose 网络里的服务名）。字符集等细节见 [ENV.md](ENV.md)。

**构建慢 / 拉不到依赖**
`docker/runtime/Dockerfile` 默认走国内镜像源（`APT_MIRROR` / `PIP_INDEX`），可以覆盖：
`docker compose build --build-arg PIP_INDEX=https://pypi.org/simple/`。构建上下文已经用
`docker/runtime/Dockerfile.dockerignore` 挡掉了 `.git` / `data` / `demo`，不会把几百 MB
塞进去（该文件依赖 BuildKit，Docker 23+ 默认就是）。

**沙盒 profile 怎么开 / 开了还是「未找到 docker 命令」**
见 §5。三个最常见的漏项：沙盒基础镜像没在**宿主**上构建、`DOCKER_GID` 与宿主
`/var/run/docker.sock` 的组 id 不一致、两边的 `FISSUE_SANDBOX_WORKSPACE` 不是同一个绝对路径。

---

## 7. 已知限制

* 默认 profile **不含沙盒**：验证与自动修复需要按 §5 额外启用（牺牲的是一部分隔离边界）。
* 镜像从源码构建，尚未提供官方预构建镜像；`docker compose pull` 目前不会拉到东西。
* 容器内一切按 Linux 口径（unix socket、uid 1000、`/data`）；Windows / macOS 上通过
  Docker Desktop 使用，宿主的路径共享规则会影响 §5 的工作区目录。
