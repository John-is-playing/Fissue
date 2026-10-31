# 环境变量（`.env`）详解

> 逐项说明每个变量的作用、默认值与填错的表现。业务规则（仓库列表、阈值、策略）在
> `config.yaml` 里，见 [USAGE.md §4](USAGE.md)。模板可直接抄 [§6](#6-配置模板)。

## 1. `.env` 与 `config.yaml` 的分工

| | `.env` | `config.yaml` |
|---|---|---|
| 放什么 | 密钥、连接串、随环境变化的值 | 业务规则、策略、阈值 |
| 提交 git | ❌（已在 `.gitignore`） | ✅ |
| 格式 | `KEY=value`，无引号无缩进 | YAML |

判断标准：**换台机器/换个人会变的值 → `.env`**。

## 2. 优先级

```
进程环境变量 > .env 文件 > config.yaml > 内置默认值
```

```bash
# .env 写 FISSUE_LLM_MODEL=from-dotenv
FISSUE_LLM_MODEL=from-shell fissue eval      # → 用 from-shell
```

理由：容器 / CI / 临时调试用一行前缀覆盖最符合直觉，`.env` 是长期默认值，不该压住当场显式指定。

⚠️ `.env` 里的**空值等于没写**，不会清空 `config.yaml` 的值。要「临时清掉某个配置」请删掉那行。

## 3. 逐项说明

### 3.1 元配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `FISSUE_CONFIG` | `./config.yaml` | 配置文件路径 |
| `FISSUE_ENV` | `./.env` | env 文件路径（多环境用，如 `.env.prod`） |
| `FISSUE_LOG_LEVEL` | `info` | `debug`/`info`/`warning`/`error`，等价全局 `-v` |

`debug` 会打印每次 HTTP 请求与 token 数，排错有用但输出量大。

### 3.2 `FISSUE_DATABASE_URL` —— 数据库

| 场景 | 值 |
|---|---|
| 默认（零依赖） | `sqlite:///./data/fissue.db`（父目录自动创建） |
| SQLite 绝对路径 | `sqlite:////home/me/fissue.db`（**4 个斜杠**） |
| PostgreSQL | `postgresql+psycopg://用户:密码@主机:5432/库名` |
| MySQL 8.0+ | `mysql+pymysql://用户:密码@主机:3306/库名?charset=utf8mb4` |
| 密码含特殊字符 | 需 URL 编码：`@`→`%40`、`:`→`%3A` |

```bash
createdb -U fissue fissue && fissue db init && fissue db check     # PostgreSQL 首次
```

```sql
CREATE DATABASE fissue CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;   -- MySQL
```

**MySQL 三条硬约束**：① 库/表字符集必须 utf8mb4；② utf8mb4 下索引上限 3072 字节，键列用
640 字符（旧的 767 会让队列表复合唯一索引建不上）；③ TEXT 只有 64KB，正文/沙盒日志/补丁列在
MySQL 上落 `MEDIUMTEXT`。另外 8.0 以下不支持 `FOR UPDATE SKIP LOCKED`，队列并发回收会失效。

**填错表现**：`无法连接数据库` → `fissue db check` / `pg_isready`；`database does not exist` →
先 `createdb`；`password authentication failed` → 转义特殊字符；SQLite `unable to open database
file` → 确认父目录存在。

后端选择：SQLite 是默认（单机、零运维，开发与单测全程在用）；常驻服务 + Web 并发或多机部署
建议 PostgreSQL（SQLite 并发写会锁表），MySQL 8.0+ 是同类选择。

> 验证深度不同：**PostgreSQL** 在 `1.1.0-b4` 起做过真实部署验证（`db init` / `db check` /
> 全量单测 / 服务与页面实测）；**MySQL** 在 Docker `mysql:8.0` 上跑通了
> `db init → fetch → eval → verify → flush → fix` 全链路（`tests/test_store_mysql.py` 可复跑，
> 按 `FISSUE_TEST_MYSQL_URL` 启用）。两者验证的都是**部署与并发路径**，各夹具跑分数字仍全部
> 来自 SQLite，**别混读**。

### 3.3 LLM 相关

| 变量 | 必需 | 说明 |
|---|---|---|
| `FISSUE_LLM_API_KEY` | ✅ | 唯一必填项；`fetch` / `status` / `report` 不需要 |
| `FISSUE_LLM_BASE_URL` | | 端点，默认取 `config.yaml` 的 `llm.base_url` |
| `FISSUE_LLM_MODEL` | | 模型名，默认取 `config.yaml` |
| `FISSUE_LLM_FALLBACK_MODEL` | | 主模型失败时的降级模型；不填则直接报错 |

| 供应商 | `BASE_URL` | 推荐 `MODEL` |
|---|---|---|
| DeepSeek（默认） | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:14b` |

`BASE_URL` 只写到 `/v1`（程序自动补 `/chat/completions`）。

**填错表现**：`401` → key 错或与 base_url 不匹配；`404` → base_url 少 `/v1` 或多 `/chat/completions`；
`模型调用失败（xxx）` → 模型名不存在；`429` → 限流，调小 `llm.concurrency`。

> 不支持 `response_format` 的端点会被自动识别并降级为文本解析，本地小模型也能跑，只是偶尔重试。

### 3.4 平台 Token

四个变量独立，用哪个填哪个。

| | 有 Token | 无 Token |
|---|---|---|
| 读公开仓库 | ✅ | ✅ |
| 限流（GitHub） | 5000 次/小时 | **60 次/小时** |
| 读私有仓库 | ✅ | ❌ |
| 打标签 / 提 PR | ✅ | ❌（自动修复不可用） |

| 平台 | 入口 | 最小权限 |
|---|---|---|
| GitHub | settings/tokens | classic `repo`；或 fine-grained 的 Contents + Issues + Pull requests 读写 |
| Gitee | 个人访问令牌 | `projects` `pull_requests` `issues` |
| AtomGit | 访问令牌（classic） | `read_repository` `write_repository` `issues` `pull_requests` |
| GitLab | Access Tokens | `api`（或 `read_api` + `write_repository`） |

**填错表现**：

| 现象 | 原因 |
|---|---|
| `401 Bad credentials` | Token 错 / 过期 / 已撤销 |
| `404` 访问私有仓库 | 权限不含该仓库 |
| `404 project or enterprise`（**建/改** Issue） | **Gitee 特有，原因未定位**：读、推送、打标签、建评论、仓库级写都正常，只有创建/更新 Issue 对象被拒（换令牌、换公开/私有库均复现）。见 `BENCHMARK.md` §4.16 |
| `400 Problems parsing JSON`（写标签） | 请求体形状：Gitee 只认**裸 JSON 数组** `["a"]`，发 `{"labels":[…]}` 就报这句。适配器已按平台分派，再见到先查体形状而不是猜权限 |
| `403` 打标签失败 | Token 缺写权限 |
| `403` 克隆私有仓库（Gitee） | 令牌需带**项目（projects）**权限，`issue` 权限不够。用 `fissue platforms doctor -r gitee:owner/repo` 区分「缺项目权限」与「凭据传递问题」 |

Token 等同密码：不要提交 git，日志与报告里会自动脱敏成 `***`。

### 3.5 Web / API

鉴权**不是环境变量**：口令、TOTP 密钥、API Key 哈希都存在数据库，策略在 `config.yaml` 的
`web.auth` 段（可在「设置」页改）。完整设计见 [AUTH.md](AUTH.md)。

| 来源 | 适用 | 怎么用 |
|---|---|---|
| 回环 | 本机 | 永远免登录 |
| 可信网段 | 内网 | 写进 `web.trusted_networks`（如 `["192.168.1.0/24"]`），视同回环 |
| 会话 / API Key | 其余 | `/login` 登录；脚本用 `Authorization: Bearer fissue_<prefix>_<secret>` |

首次启动且无管理员时，控制台打印一次性引导口令（也写在 `data/auth-bootstrap-token`，0600），
用后即焚。

```bash
fissue auth status                  # 有没有账号 / 引导口令
fissue auth set-password            # 设或改口令（会注销该账号全部会话）
fissue auth reset                   # 清 2FA 与恢复码
fissue auth key create --name ci    # 建 API Key（明文只显示一次）
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `FISSUE_WEB_HOST` | `127.0.0.1` | 仅本机可访问，最安全；`0.0.0.0` 时需有身份机制（默认 `web.auth.enabled=true` 已满足） |
| `FISSUE_WEB_PORT` | `8000` | 非整数会友好报错 |

> 反向代理：CIDR 判定只看真实对端，默认不信 `X-Forwarded-For`。「绑 127.0.0.1 + 同机 nginx
> 转出去」时请把代理/内网网段写进 `web.trusted_networks` 显式声明。

**填错表现**：`FISSUE_WEB_PORT 必须是整数` → 填了非数字；`Address already in use` → 换端口；
局域网访问不到 → `WEB_HOST` 仍是 `127.0.0.1`。

### 3.6 `FISSUE_SANDBOX_RUNNER_SOCKET` —— 沙盒转发组件

不填：走 `inline`（进程内直调 Docker，简单够用）。填了：走转发组件，调用方不持有 Docker 句柄，
生产（Linux）推荐。

```bash
fissue sandbox serve
FISSUE_SANDBOX_RUNNER_SOCKET=/var/run/fissue-runner.sock     # 等价 sandbox.forwarder.socket
```

Windows 不支持 Unix socket，会自动回退 inline；要让它工作请写 `\\.\pipe\fissue-runner`。

### 3.7 TLS 证书

只有遇到下面这个报错才需要配：

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```

说明环境里有 **HTTPS 中间人**（企业安全网关，或 Fiddler / Charles / SteamTools / 部分 Clash
模式）。**先分清是哪种情况再动手**，无脑装 truststore 是最常见的白折腾：

```bash
python -c "from fissue import net; print(net.ca_bundle_hint())"
```

| 实际链路 | 说明 | 怎么办 |
|---|---|---|
| `certifi 默认 CA 列表` | truststore 没装上 | 方案 1 |
| `操作系统信任库（truststore）` | 装了，但这张根证书连系统信任库也没有 | 方案 2 |
| `自定义 CA：…` | 已指定仍失败 | 根证书不止一张（代理套代理），合并成同一个 PEM |

1. **用系统信任库**（默认）：依赖里已含 `truststore`，装好依赖即可，无需配置。仅在「根证书
   确实进了 OS 信任库」时有效。
2. **显式指定根证书**：把根证书导出为 PEM，`FISSUE_CA_BUNDLE=/path/to/root-ca.pem`（支持 `~`）。
   该变量是**严格的**：路径不存在或内容不是合法 PEM 会**直接报错**，不静默回退——静默回退等于
   你以为只信这张证书、实际用的是 OS 信任库。多层中间人需合并：`cat a.pem b.pem > combined.pem`。
3. **临时关闭校验**（仅排查）：`FISSUE_INSECURE_SKIP_VERIFY=1`。存在中间人攻击风险，绝不写进生产。

优先级：`INSECURE_SKIP_VERIFY > CA_BUNDLE > 系统信任库 > certifi`。

验证：`fissue status --verbose`（日志说明 CA 来源），或 `fissue fetch --repo psf/requests --limit 1`。

> 证书错误是**确定性**的，Fissue 会立即失败并给出上述方案，而不是白等 4 轮退避；其它网络抖动
> （超时、连接重置）仍正常重试。

## 4. 完整变量清单

| 变量 | 必需 | 默认 | 作用 |
|---|---|---|---|
| `FISSUE_DATABASE_URL` | | `sqlite:///./data/fissue.db` | 存储连接串 |
| `FISSUE_LLM_API_KEY` | ✅ | 无 | 大模型密钥 |
| `FISSUE_LLM_BASE_URL` | | config.yaml 值 | 模型端点 |
| `FISSUE_LLM_MODEL` | | config.yaml 值 | 模型名 |
| `FISSUE_LLM_FALLBACK_MODEL` | | 无 | 降级模型 |
| `FISSUE_GITHUB_TOKEN` / `GITEE` / `ATOMGIT` / `GITLAB` | | 空 | 平台访问 |
| ~~`FISSUE_API_TOKEN`~~ | — | — | **已移除**：被会话 + API Key 替换（垫片于 v1.3.0 移除）。遗留的 `prefix=legacy` Key 到「API Key」页吊销 |
| `FISSUE_WEB_HOST` | | `127.0.0.1` | 监听地址 |
| `FISSUE_WEB_PORT` | | `8000` | 监听端口 |
| `FISSUE_SANDBOX_RUNNER_SOCKET` | | 空（inline） | 转发组件 socket |
| `FISSUE_CA_BUNDLE` | | 空（系统信任库） | CA PEM 路径（不可用直接报错） |
| `FISSUE_INSECURE_SKIP_VERIFY` | | 空 | 关闭 TLS 校验（仅排查） |
| `FISSUE_LOG_LEVEL` | | `info` | 日志级别 |
| `FISSUE_CONFIG` / `FISSUE_ENV` | | `./config.yaml` / `./.env` | 文件位置 |

> 这份清单与 `.env.example` 由 `tests/test_env.py` 自动校准：代码会读的必须在示例里有说明，
> 示例列出的必须真的会被读取。

## 5. 验证 `.env`

```bash
fissue status                      # 语法与取值能否加载
fissue db check                    # 数据库
fissue eval --limit 1              # 真的调一次模型
fissue eval --limit 1 --verbose    # 看请求细节
fissue fetch --repo <owner/name> --limit 1   # 平台 Token
fissue sandbox check               # 沙盒
```

## 6. 配置模板

**最小可用**（本地试用，不写平台）：

```bash
FISSUE_LLM_API_KEY=sk-你的真实key
```

**开发**（DeepSeek + SQLite + 本机 Web）：

```bash
FISSUE_DATABASE_URL=sqlite:///E:/Fissue/data/fissue.db
FISSUE_LLM_API_KEY=sk-xxx
FISSUE_LLM_BASE_URL=https://api.deepseek.com/v1
FISSUE_LLM_MODEL=deepseek-chat
FISSUE_GITHUB_TOKEN=ghp_xxx
FISSUE_WEB_HOST=127.0.0.1
FISSUE_WEB_PORT=8000
FISSUE_LOG_LEVEL=debug
```

**生产**（PostgreSQL + 四平台 + 鉴权 + 转发组件）：

```bash
FISSUE_DATABASE_URL=postgresql+psycopg://fissue:强密码@db.internal:5432/fissue
FISSUE_LLM_API_KEY=sk-xxx
FISSUE_LLM_BASE_URL=https://api.deepseek.com/v1
FISSUE_LLM_MODEL=deepseek-chat
FISSUE_LLM_FALLBACK_MODEL=deepseek-chat
FISSUE_GITHUB_TOKEN=ghp_xxx
FISSUE_GITEE_TOKEN=xxx
FISSUE_ATOMGIT_TOKEN=xxx
FISSUE_GITLAB_TOKEN=glpat-xxx
FISSUE_WEB_HOST=0.0.0.0
FISSUE_WEB_PORT=8000
FISSUE_SANDBOX_RUNNER_SOCKET=/var/run/fissue-runner.sock
FISSUE_LOG_LEVEL=info
```

对外绑定时必须开 `web.auth.enabled`（默认已是 true）或声明 `web.trusted_networks`，否则拒绝启动。
API Key 用 `fissue auth key create`，口令用 `fissue auth set-password`。

**完全离线**（Ollama）：

```bash
FISSUE_LLM_API_KEY=ollama          # 任意非空值
FISSUE_LLM_BASE_URL=http://localhost:11434/v1
FISSUE_LLM_MODEL=qwen2.5:14b
```

小模型在「生成验证器」上成功率低于大模型，`needs_manual` 会更多，属正常现象。

**CI**：不要提交 `.env`，用平台 Secrets 注入（见 [USAGE.md §9.5](USAGE.md)）。

## 7. 写法注意事项

```bash
# ✅
FISSUE_LLM_API_KEY=sk-abc123
FISSUE_LLM_API_KEY="sk-abc 123"                                # 含空格用引号
FISSUE_DATABASE_URL=postgresql+psycopg://u:p%40word@h/db       # 特殊字符 URL 编码（双引号）

# ❌
FISSUE_LLM_API_KEY = sk-abc123        # = 两边不要空格
export FISSUE_LLM_API_KEY=sk-abc123   # 不需要 export
FISSUE_LLM_API_KEY='sk-abc123'        # 单引号行为因 shell 而异
```

行内注释可用但 `#` 会开始注释。Token 在日志与报告中被替换为 `***`，但文件权限请收紧：
`chmod 600 .env`。

## 8. 相关文件

| 文件 | 作用 |
|---|---|
| `.env.example` | 变量清单与注释（提交进 git） |
| `.env` | 你的实际密钥（**不提交**） |
| `config.yaml` | 业务规则（见 [USAGE.md §4](USAGE.md)） |
| [AUTH.md](AUTH.md) | Web 鉴权设计 |
| `tests/test_env.py` | 变量接线与优先级的自动化校验 |

---

相关：[USAGE.md](USAGE.md) · [AUTH.md](AUTH.md) · [DESIGN.md](DESIGN.md) ·
[wiki/Configuration.md](wiki/Configuration.md)