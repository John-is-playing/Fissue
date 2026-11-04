# 部署与服务器运维

> 能力目标：给 Fissue 一个仓库地址与一台服务器，**自己读懂 README 与代码**，
> 生成部署计划、在远端执行、用**独立证据**确认服务真的起来了；之后能
> 看状态 / 看日志 / 重启 / 回滚 / 卸载。

这份文档先定契约（数据模型、动作白名单、证据判定、回滚语义），实现按此落地。
改契约请先改本文档——否则 CLI、Web、执行器三处必然漂移。

---

## 1. 为什么不能「让模型自己上台执行」

Fissue 的核心主张是「别信模型的自述，要看独立证据」。放到部署上，这条主张
意味着两件具体的事：

1. **模型不产生任意 shell**。AI 只输出**结构化计划**：一组白名单动作
   （`upload` / `write_file` / `service` / `exec` / `probe`），每个动作有
   `id`、`check`（怎么判定它成了）、`rollback`（失败怎么退）。模型想做的
   怪事，首先得落成这几种形状之一。
2. **「部署成功」不是模型说的，也不是部署脚本自己 echo 的**。判定证据必须
   来自控制端：SSH 上 `systemctl is-active`、`ss -ltnp` 里真有那个端口、
   以及**从控制端发起的健康探针**（不是远端自己 `curl localhost`——远端
   curl 成功只证明它自己看得到自己，证明不了对外可用）。

## 2. 能力链

```
                ┌─────────────┐
  仓库(本地/克隆) → │  planner    │ AI 读 README + 目录树 + 关键文件
                └──────┬──────┘
                       │ DeployPlan（严格 schema，可校验可存库）
                ┌──────▼──────┐
                │   guard     │ 危险命令拦截 + 计划合法性校验
                └──────┬──────┘
                       │ 校验通过才允许执行
                ┌──────▼──────┐
                │   runner    │ SSH 执行：上传 → 装环境 → 起服务 → 探针
                └──────┬──────┘   每步落 deploy_steps + deploy_events
                       │
            成功 ──────┴────── 失败
              │                 │
       symlink 切 current    rollback：切回上一 release + 重启
              │                 │
           状态 success      状态 rolled_back / failed
```

CLI 与 Web API 共用同一条链（都走 `fissue/deploy/` 下的服务层），不允许
各写一遍。

## 3. 发布布局（远端）

```
/opt/fissue-deploy/<owner>-<name>/
  releases/
    20261102T101500Z/        # 每次部署一份完整产物
    20261101T090000Z/
  current -> releases/20261102T101500Z   # 原子切换，回滚就是把它指回去
  shared/                    # 跨版本保留：.env、上传目录、sqlite 数据
  deploy-state.json          # 远端自述（仅供参考，不作为成功证据）
```

**为什么必须整份 release + symlink**：就地覆盖会让「回滚」退化成「把旧文件
再传一遍」，而旧文件早被覆盖了。symlink 切换是原子的，回滚只需一次
`ln -sfn` + 重启，且新旧产物同时在盘上，出问题能人工比对。

`shared/` 单独存在的理由同理：`.env`、上传目录、SQLite 文件一旦落在
release 目录里，每次部署都会被换掉——这是「越部署数据越少」的经典事故。

## 4. 数据模型

新增四张表（只加表、不加列，因此 `create_all` 即可，无需列迁移）：

### `deploy_hosts`

| 列 | 说明 |
|---|---|
| `id` / `name`(唯一) | 主机别名，CLI/Web 都用它引用 |
| `address` / `port` / `user` | 连接目标 |
| `auth_method` | `key` \| `password` |
| `credential` | **加密后**的私钥正文或密码（`deploy/secretbox.py`） |
| `host_fingerprint` | 首次连接 TOFU 记下的主机公钥指纹 |
| `enabled` | 关掉的主机不允许部署 |
| `last_seen_at` | 最近一次成功连接 |

**凭据必须可逆加密而不是哈希**：SSH 私钥/密码要能还原出原文才能用。
密钥由 `data/server.secret` 经 HKDF 派生，落库的是密文；日志与 API 一律
只回显 `auth_method` + 指纹，永不回显正文。

### `deployments`

`id`、`host_id`、`repo_slug`、`commit_sha`、`status`、`plan_json`、
`app_root`、`service_name`、`url`、`health_json`、`error`、
`created_at` / `finished_at`、`release`（目录名）、`previous_release`。

### `deployment_steps`

`deployment_id`、`seq`、`step_id`、`kind`、`command`、`status`、
`exit_code`、`output_tail`（尾部若干 KB，防爆库）、`started_at` /
`finished_at`。

### `deploy_events`

审计流水：`deployment_id`、`level`、`message`、`created_at`。所有执行的
命令原文都进这里——出事后要能回答「昨天晚上到底谁在机器上干了什么」。

状态机：

```
planning → running → success
              │└──→ failed          （无可用回滚点时）
              └───→ rolled_back     （回滚完成）
```

## 5. 动作白名单

| kind | 字段 | 说明 |
|---|---|---|
| `upload` | `src`(本地路径/包)、`dst`(远端 release 内相对路径) | 传产物 |
| `write_file` | `path`、`content`、`mode` | 写 unit 文件 / nginx conf / .env |
| `exec` | `command`、`cwd?`、`timeout?` | 装依赖、跑构建；过 guard |
| `service` | `action`(enable/start/restart/stop)、`name` | systemd 操作 |
| `probe` | `url?`、`port?`、`expect_status?`、`timeout?` | **控制端**发起 |

每个动作都带 `check`（缺省按 kind 推导）与可选 `rollback`。计划整体还有
`health`（结束前必须全绿的探针集合）与 `artifacts`（要上传的本地路径）。

## 6. 危险动作拦截

用户选择了「完全通用，让 AI 自由发挥」，但通用不等于无防线。`deploy/guard.py`
对 `exec` 命令做两层检查：

- **绝对拒绝**（`--allow-dangerous` 也不放行）：`rm -rf /`、`mkfs`、
  `dd of=/dev/`、`shutdown/reboot/poweroff`、`iptables -F`、
  fork 炸弹、写 `/boot`、`chmod -R 777 /`、`userdel root`。
- **需显式开启**（`deploy.allow_dangerous=true` 或 `--allow-dangerous`）：
  `sudo`、`rm -rf`、`apt/yum remove`、`systemctl disable`、改 `/etc/sudoers`、
  `kill -9`、`docker rm -f`。

计划层面还有硬上限：`deploy.max_plan_steps`（默认 60），超了直接判计划非法，
不执行。

## 7. 失败与回滚

1. 任一步失败 → 立即停止后续步骤（不「尽力而为」，半成品服务比没部署更糟）。
2. 若存在 `previous_release`：`ln -sfn` 指回它 → 重启服务 → **重新跑健康
   探针**；探针绿才记 `rolled_back`，否则记 `failed` 并在事件里说明「回滚
   后仍不健康，需要人工介入」。
3. 首次部署失败（没有回滚点）→ 记 `failed`，保留现场目录不删，方便查。

回滚**不复用**原计划的步骤：它是一段固定动作，走同一条 SSH 通道与同一套
证据判定。这样「回滚本身失败」不会被伪装成前一次部署的问题。

## 8. 与现有模块的关系

| 现有 | 关系 |
|---|---|
| `sandbox/` | **不复用**。沙盒是禁网、只读根、一次性容器，用来跑验证器；部署要写盘、要常驻、要联网。两者安全模型相反，硬凑会同时削弱两边。 |
| `platforms/` | 只用来解析仓库与克隆；部署不通过平台 API。 |
| `ai/client.py` | 复用 `chat_json` + 预算守卫，新增 `deploy.plan` 这一用途标签。 |
| `auth/` | 复用 `require_auth` / 只读模式 / 写互斥闸门；凭据加密复用 `server.secret`。 |
| `store/` | 新表进 `tables.py`，读写进 `store/deploy_repo.py`（不塞进已有 1400 行的 `repository.py`）。 |

## 9. 命令与端点

```
fissue host add <name> --address ... --user ... --port 22 \
      --key-file ~/.ssh/id_ed25519 | --password-stdin
fissue host list | show <name> | remove <name> | ping <name>
fissue deploy plan <owner/name> --host <name> [--ref main]   # 只出计划，存库并打印
fissue deploy apply <owner/name> --host <name> [--plan-file p.yaml] [--dry-run]
fissue deploy status [<deployment-id>]
fissue deploy logs <deployment-id> [--lines 200]
fissue deploy rollback <deployment-id>
fissue deploy destroy <deployment-id>        # 停服务 + 删 release（保留 shared）
```

REST：

```
GET    /api/v1/hosts            POST /api/v1/hosts
GET    /api/v1/hosts/{name}     DELETE /api/v1/hosts/{name}
POST   /api/v1/hosts/{name}/ping
GET    /api/v1/deployments      POST /api/v1/deployments          # 触发一次部署
GET    /api/v1/deployments/{id} POST /api/v1/deployments/{id}/rollback
GET    /api/v1/deployments/{id}/logs
```

所有写端点与现有写路径一样：`_require_writable` + 认证 + `_exclusive_write`。

## 10. 配置

```yaml
deploy:
  enabled: false                 # 默认关：这是能改远端机器的能力，不该开箱可用
  app_root: /opt/fissue-deploy
  keep_releases: 5               # 超过的旧 release 自动清理（保留 previous 一份）
  connect_timeout: 15
  command_timeout: 900
  health_timeout: 60
  max_plan_steps: 60
  allow_dangerous: false
  workflow: auto                 # auto=按代码自动推断；留空也给 auto
```

默认 `enabled: false` 的理由：Fissue 的默认姿态是「只读地看世界」，一旦默认
开启，任何拿到 Web 写权限的人就能对清单里的服务器动刀。

## 11. 验证策略

- 单测：SSH 层用假连接（`tests/test_deploy_ssh.py`），计划生成用假 LLM，
  执行器的每步落地、失败中止、回滚路径全部覆盖；`guard` 的正反例逐条覆盖。
- 真机：需要一台 Linux（systemd + sshd）。跑 §12 的冒烟清单：全新部署 → 改一版
  再部署 → 注入失败步骤验证回滚 → destroy。
- **不做**：自动在真机上跑随机仓库。全自动那条路只对明确登记的主机开放，
  且每次都有审计流水。

## 12. 真机冒烟清单

按顺序做一遍，每步记下判定依据（别只看"没报错"）：

| # | 动作 | 判定依据 |
|---|---|---|
| 1 | `fissue host add <名> --address ... --key-file ...` | `fissue host list` 里 `auth=key`、`fingerprint` 显示"未建立" |
| 2 | `fissue host ping <名>` | 打印 uptime/disk/systemd；再 `host show` 应看到指纹已钉住 |
| 3 | `fissue deploy plan <owner/repo> --host <名>` | 计划落库、步骤数合理、`health` 至少一条且**不是** `127.0.0.1` |
| 4 | `fissue deploy apply ...` | 远端 `systemctl is-active` = active、`ss -ltn` 有端口、控制端 `curl` 通；DB 里 status=success |
| 5 | 改一行代码再 `apply` | `releases/` 下多一个目录，`current` 指向新的，旧目录仍在 |
| 6 | `fissue deploy rollback <id>` | `current` 指回旧的、服务仍是 active、探针仍通 |
| 7 | 故意把计划里某条命令改错再 `apply` | 状态 `rolled_back`（有上一版）或 `failed`（首次），`deploy logs` 能看到失败步 + 审计流水 |
| 8 | `fissue deploy destroy <id>` | 服务已停、`releases/` 已删、**`shared/` 还在** |
| 9 | 手工改远端指纹（`ssh-keygen -R` 后重建主机密钥）再 `ping` | 报 `HostKeyMismatch`，**不**自动接受新指纹 |

第 9 步是唯一无法用"部署成功"替代的安全验证：它能证明中间人防线真的在。

## 13. 真机验证记录（2026-11-03，Ubuntu 24.04 aarch64）

目标机：`john@192.168.2.10`（树莓派，systemd 255、Python 3.12、**免密 sudo**），
被部署物：`John-is-playing/school_bbs`（Flask + SQLite，`python -m app` 绑 `0.0.0.0:5000`）。

结果：ping / TOFU / 部署 / 三路证据 / 回滚 / 下线 全部通过。过程中的**六个真实缺陷**
已全部修掉并补了用例——这一节存在，是为了下次换机器时不重新踩：

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 计划被自己的校验拒绝：`write_file.path 必须是远端绝对路径` | `schema`/执行器支持「release 内相对路径」，`guard` 却一律要求绝对路径 | `guard` 接受相对路径（= release 内），`..` 仍拒；相对路径 + `allow_outside_release` 判矛盾直接拒 |
| 2 | 上传解包失败：`tar: <release 目录>: Cannot open` | `_prepare` 把 `mkdir` 与探测用 `&&`/`||` 串成一句，mkdir 的权限失败被 `|| echo WARN` 短路吃掉了，整体退出码仍是 0 | 一条命令一件事，按该命令自身退出码判定 |
| 3 | 同上（非 root 目标） | 默认 `app_root=/opt`，普通用户建不了 | `_prepare` 失败时回退 `sudo -n mkdir -p` + `chown` 给连接用户 |
| 4 | `venv` 步骤退出码 1 | Ubuntu 无 `ensurepip`（缺 `python3-venv`） | 属计划环境前置：`sudo apt-get install python3.12-venv`（AI 规划时也应从 README 推断出来） |
| 5 | 11 步全绿但判失败：`activating` / 端口未监听 / 探针连不上 | **健康检查没有等待**：`Type=simple` 一 exec 就报 active，应用 bind 端口还要 1~2 秒；`deploy.health_timeout` 定义了却没用 | 按 `health_timeout` 轮询证据（2 秒一次），全绿或超时才结束；超时后抓 `journalctl` 尾部作为失败原因 |
| 6 | `destroy` 报 `Interactive authentication required` | `systemctl stop` 没带提权（`restart`/rollback 带了，销毁路径漏了） | 统一从计划里的 `service_sudo` 取提权；`sudo -n` 非交互，失败会立刻报错而不是卡在密码提示 |
| 7 | 「下线」后 `systemctl is-enabled` 仍是 `enabled`，unit 文件还在 | `destroy` 只 stop 不清理 unit：机器重启后 systemd 会去启动一个指向**已删除目录**的服务，而列表里没有痕迹 | `destroy` 依次 stop → disable → 删 unit 文件 → `daemon-reload`，`shared/` 仍保留 |

另外两个**部署到非 root 账号**时必须知道的事实（已写进动作白名单）：

- `service` 动作需要 `sudo: true` 才能操作系统级 unit；它受 `deploy.allow_dangerous`
  约束（不走 `guard` 的命令检查，所以检查放在执行器里）。
- 写 unit 文件的正确姿势：模板放 release 内（相对路径），再用
  `sudo -n install -m 644 deploy/x.service /etc/systemd/system/x.service` 装出去——
  SFTP 无法以 root 写盘，所以「写系统目录」这件事必须走 `exec`。

### 控制端环境坑（Windows + Git Bash）

远端路径（`/opt/...`）会被 MSYS 转成 `C:/Program Files/Git/opt/...`，于是
`--app-root /opt/fissue-deploy` 直接报「必须是绝对路径」。两种解法：
`MSYS_NO_PATHCONV=1`（此时配置文件与密钥路径要写 `E:/...` 形式），
或用 `//opt/...` 的双斜杠写法。**这不是 Fissue 的缺陷**，但会让人以为命令写错了。

### 验证时用的隔离配置

真机验证**不要**用生产库：`.env` 里的 `FISSUE_DATABASE_URL` 优先级高于
`config.yaml`，会把你以为在「临时库」里跑的操作写进生产 PostgreSQL。做法是
显式覆盖环境变量：

```bash
export FISSUE_DATABASE_URL="sqlite:///E:/Fissue/data/deploy-verify/verify.db"
fissue deploy apply ... --config E:/Fissue/data/deploy-verify/config.yaml --env .env
```

## 14. AI 计划真机验证（2026-11-03，第二轮）

同一台树莓派、同一个仓库，这一轮让**模型自己**读 README 与代码出计划，再真机执行。
结论：AI 计划的**结构质量不差**，但它会漏掉「模型看不出来的环境事实」，必须靠
确定性归一化与执行器兜底。

### AI 计划比手写版更好的三处

| 点 | AI 计划 | 手写计划 |
|---|---|---|
| 虚拟环境 | 放 `shared/venv`，跨版本复用，升级只重装依赖 | 每版本在 release 里重建（慢且浪费） |
| 运行方式 | `gunicorn`（多 worker，生产可用） | Flask 开发服务器（`debug=True`，不适合生产） |
| 前置校验 | 多一步 `python -c "import run"`，起服务前就暴露 import 错误 | 没有，错了要等 systemd 反复重启才发现 |

### 但它在真机上连续踩了五处（第 8~12 个缺陷）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 8 | 计划生成成功、落库时报 `Unable to serialize unknown type: <class 'method'>` | `getattr(workspace, "commit", None)` 取到的是 `RepoWorkspace.commit()` **方法**，赋给 `commit_sha` 后 pydantic 默认不校验赋值，直到序列化才炸 | 只接受字符串的 `_commit_sha_of()`；`DeployPlan` 开 `validate_assignment=True`，让这类错误在赋值那一刻就暴露 |
| 9 | 计划自相矛盾：`sudo -n install … /etc/systemd/system/…`，但 `service` 步骤 `sudo=false`；探针写成 `http://rpi:5000/`（别名） | 提示词是**请求**不是**保证**，模型仍会漏 | 新增 `deploy/normalize.py`：从计划本身**确定性**推断（装系统 unit ⇒ service 要提权；探针主机名等于别名 ⇒ 换成真实地址），每处改动写进 `notes` 供审计 |
| 10 | 执行到 `probe tcp:5000` 报「TCP 探针缺少目标主机」 | 内联 `probe` 步骤只有 `port`，目标主机来自连接上下文，只有 `health` 里的探针被补过 | 执行器处理 `ProbeSpec` 步骤时从 `channel.target.address` 补主机 |
| 11 | 部署失败自动回滚后，服务以 `200/CHDIR` 反复重启，反而更糟 | 回滚点（上次成功部署的 release）已被此前的 `destroy` 删除，`ln -sfn` 造出**悬空软链**，unit 的工作目录不存在 | 回滚前 `test -d` 校验回滚点；`destroy` 把同仓库历史「成功」记录一并标成 `destroyed`，不让它们再被当成回滚点 |
| 12 | 服务以 **root** 运行（`ps` 显示 root），`__pycache__` 属主 root；`destroy` 的 `rm -rf` 权限错误但**静默当成功**，`releases/` 还留在盘上 | 模型写的 unit 没有 `User=`；`destroy` 没检查 `rm` 的退出码 | 归一化给 unit 补 `User=<部署用户>`；`destroy` 删除失败即报错并 `sudo -n` 重试，绝不假成功 |

第 12 条是最值得记住的：**一个没有 `User=` 的 unit 会让应用以 root 跑在公网上**，
而它的第一现场往往只是「删不掉几个 pyc」。

### 提示词因此改了四条

`SYSTEM_DEPLOY_PLANNER` 从「不要用 sudo」改成「sudo 只在必要时用且必须显式标注」，
并补上三条真机事实：unit 必须由计划自己 `write_file` + `install` 出去；
Debian/Ubuntu 默认没有 `ensurepip`，`python3 -m venv` 前要先装 `python3-venv`；
健康探针优先给 HTTP `url`，且服务必须监听 `0.0.0.0`。

**但不要指望提示词解决一切**：上面第 9、12 条在提示词里写了之后，模型仍会漏。
所以真正的保障是幂等的 `normalize_plan()` 与执行器的硬校验，而不是措辞。

### 这一轮的最终状态

- AI 计划 20 步：**全部通过**，三路证据齐全（systemd active / `ss` 有监听 / 控制端 HTTP 200）。
- 第二版部署 → `rollback` 回上一版：`rolled_back`，证据复检通过。
- `destroy`：unit 已移除（`not-found`）、`releases/` 已删、`shared/`（venv / SQLite /
  uploads / instance）保留。
- 服务运行身份：`User=john`，`ps` 确认非 root。
## 15. 新手引导（`/ops/guide`）

目标：让**没部署过服务的人**也能把这个仓库部署起来，而不是先读完这份 300 行的文档。

### 三个阶段，一套内容

| 阶段 | 解决什么 | 内容来源 |
|---|---|---|
| 1. 认识 Fissue | 这台工具是什么、凭据存在哪、为什么默认关闭 | 固定文案 |
| 2. 准备服务器 | 主机怎么登记、指纹是什么、为什么第一次要确认 | 固定文案 |
| 3. 部署这个仓库 | 它需要什么运行时/依赖/端口、怎么起、怎么验证 | **AI 读 README + 代码生成** |

第 3 阶段是唯一需要模型的部分；前两阶段是固定文案——它们的正确性不依赖模型，
也不该因为模型抽风就消失。

### 三档自动程度（可随时切换）

| 档位 | 行为 | 适合 |
|---|---|---|
| `readonly` | 只展示命令与解释，自己复制执行 | 第一次看、生产谨慎操作 |
| `half` | 每步一个按钮，执行前把将跑的命令显示出来 | 已经看懂在做什么的人 |
| `auto` | 一个按钮按固定顺序跑完，失败自动降级 | 熟手、临时环境 |

**一条硬约束**：AI 只写**叙述与命令文本**，半自动/自动档执行的是页面里
**写死的五个动作**（`add-host` / `generate-plan` / `apply` / `check` / `rollback`）。
模型不能指定 URL——否则一句「顺手 DELETE 一下」就能借引导页之手改服务器。
引导里每个步骤只允许声明「这一步对应哪个固定动作」，对不上就只能手动。

### 全自动失败时降级，而不是卡死

这是这套引导存在的理由：一键部署失败时，页面**自动切回只读档**并展开该阶段
的手动命令，把 `deploy logs` 里的失败步骤一起展示。新手最常见的死法是
"点了一键、红了、然后不知道该干什么"，降级路径必须是一等公民。

### 数据与接口

引导只存库（`deploy_guides`，每个 `repo_slug` + `host` 一条，重新生成即覆盖）：

```
GET  /api/v1/guides?repo_slug=&host=     读最新一份
POST /api/v1/guides                      生成（AI 读仓库；无 LLM 时返回固定兜底模板）
GET  /ops/guide?repo_slug=&host=         页面
```

`POST /api/v1/guides` 需要认证与写闸门（它要克隆仓库并调用模型，会花钱）；
读接口与页面与其余 `/ops` 页面同级受保护。

### 为什么不做「AI 直接生成一个可运行脚本」

脚本看着方便，但它把「执行什么」和「谁写的」缝在一起：新手无法判断一段
看不懂的 shell 是否安全，出错时也没有逐步证据。分步骤 + 固定动作至少保证
每一步都能停下来、能看懂、能回滚。

## 16. 真机验证记录（2026-11-04，部署 Fissue 自身）

目标：让 AI 读 Fissue 自己的仓库、生成计划，把它部署到树莓派（Ubuntu 24.04 aarch64），
再用三类独立证据判定。**结论：成功**（delivery #18，24/24 步，证据全绿）。

### 16.1 执行过程

```bash
# 1. 关掉上一版手工部署（只 stop + disable，代码目录保留）
ssh john@192.168.2.10 'sudo -n systemctl stop fissue-web && sudo -n systemctl disable fissue-web'

# 2. 让 AI 生成计划（克隆走 clone_mirror，因为控制端直连 github.com 不通）
fissue deploy plan John-is-playing/Fissue --host rpi --allow-dangerous --json

# 3. 复用计划执行（--plan-file 必须同时给 --source，防止误传当前目录）
fissue deploy apply John-is-playing/Fissue --host rpi \
  --plan-file data/deploy-verify/fissue_plan.patched.json --source . --allow-dangerous
```

### 16.2 这次暴露的四个问题

| # | 现象 | 根因 | 现在的处理 |
|---|---|---|---|
| 1 | `install-app` 失败，报错埋在 pip 构建日志里 | `pyproject.toml` 有 `readme = "README.md"`，AI 只上传了 pyproject / src / config.example | `normalize` 读 pyproject 的 `readme` / `license.file` / `hatch.version.path`，自动补上传步骤 |
| 2 | 服务起来即退出（`ConfigError: 缺少 LLM API Key`），失败点报在第 20 步探针 | AI 写了假占位符 `FISSUE_LLM_API_KEY=sk-REPLACE-ME` | 新增 `required_inputs` 契约；`app.env` 出现占位符时**连机器之前**就失败并点名 |
| 3 | 控制端探针 `ConnectError` | 服务若监听 `127.0.0.1`，控制端永远连不上 | `normalize` 在存在**非回环探针**时把 `*HOST/*ADDR/*BIND` 的回环值改成 `0.0.0.0` |
| 4 | 从局域网打开只看到登录页 | 首次部署没有管理员账号，且未声明可信网段 | 计划里加 `config.local.yaml` 覆盖层写 `web.trusted_networks`（示例，按实际网段填） |

问题 1/2 已做成确定性规则与执行前检查，问题 3 已进归一化；问题 4 取决于网络环境，
写在计划的覆盖层步骤里更合适（不改产品默认值）。

### 16.3 计划质量评价

AI 产物里**本来就有**的正确部分：release + `current` 软链 + `shared/` 布局、unit 由计划
自己安装、`User=<部署用户>`、`sudo -n` 提权、venv 放 `shared/`、SQLite 放 `shared/data/`、
健康探针指向真实地址。缺的是**打包元数据文件**与**启动前置项**这两类"仓库外部知识"。

### 16.4 复跑清单（回归时逐条核对）

1. `fissue host list` 里 `rpi` 指纹已钉住（`SHA256:` 开头，不是"未建立"）；
2. `fissue deploy plan ...` 的 JSON 里：`steps` 含打包元数据上传、`app.env` 无占位符、
   `health` 的 url 是 `http://<真实地址>:<端口>/`（不是回环、不是别名）；
3. `fissue deploy apply ...` 末行 `success` 且"证据 全部证据通过"；
4. 远端三类证据：`systemctl is-active` = `active`、`ss -ltnp | grep :端口` 有监听、
   控制端 `curl http://<地址>:<端口>/` 返回 200；
5. `ps -o user= -p <MainPID>` = 部署用户（**不是 root**）；
6. `readlink -f .../current` 指向最新 release，`releases/` 下历史版本保留；
7. 失败时状态必须是 `failed` 且**不**留下半成品：`current` 仍指向上一个可用版本。

### 16.5 已知限制

- `--plan-file` 不会克隆仓库，所以 `normalize` 的打包文件规则依赖显式 `--source`；
  不给源码目录时这条规则整条跳过（宁可不补，也不猜文件是否存在）。
- `required_inputs` 里声明了但 `app.env` 没给的键只**警告**不拦截：该值很可能已长期
  保存在远端 `shared/.env`，当错误会误伤增量部署。
