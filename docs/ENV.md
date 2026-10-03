# `.env` 配置详解

> 本文逐项说明 Fissue 的每个环境变量：**是什么、填什么、不填会怎样、填错了什么表现**。
> 想直接抄模板跳到 [§6 配置模板](#6-配置模板)。业务规则（仓库列表、阈值、策略）
> 在 `config.yaml`，见 [USAGE.md §4](USAGE.md#4-配置业务规则configyaml)。

---

## 1. 先搞清 `.env` 和 `config.yaml` 的分工

| | `.env` | `config.yaml` |
|---|---|---|
| 放什么 | 密钥、连接串、**随环境变化**的值 | 业务规则、策略、阈值 |
| 是否提交 git | ❌ 不提交（已在 `.gitignore`） | ✅ 可提交 |
| 典型内容 | LLM key、数据库密码、平台 Token | 抓哪些仓库、修哪些 Issue、通知发给谁 |
| 格式 | `KEY=value`（无引号，无缩进） | YAML |

判断标准：**这个值换台机器/换个人会不会变？** 会变 → `.env`；不会 → `config.yaml`。

---

## 2. 优先级（最容易踩坑的地方）

```
① 进程环境变量         FISSUE_LLM_MODEL=xxx fissue eval
        ↓ 覆盖
② .env 文件            ./​.env
        ↓ 覆盖
③ config.yaml          ./config.yaml 的 llm.model
        ↓ 覆盖
④ 内置默认值           deepseek-chat 等
```

实测验证（已写成测试 `tests/test_env.py::test_process_env_beats_dotenv_file`）：

```bash
# .env 里写 FISSUE_LLM_MODEL=from-dotenv
$ FISSUE_LLM_MODEL=from-shell fissue eval
# → 实际使用 from-shell（进程环境赢）
```

**为什么这样设计？** 容器 / CI / 临时调试时用一行前缀覆盖最符合直觉；
`.env` 是「长期默认值」，不该压住你当场显式指定的值。

### 2.1 一个常见误解

`.env` 里的**空值等于没写**，不会把 `config.yaml` 的值清空：

```bash
# .env
FISSUE_LLM_MODEL=          # ← 空值，将被忽略
```
```yaml
# config.yaml
llm:
  model: deepseek-chat     # ← 仍生效
```

所以「临时清掉某个配置」要删掉那一行，或者显式设成有效值。

---

## 3. 逐项说明

### 3.1 元配置（一般不用动）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FISSUE_CONFIG` | `./config.yaml` | 指定 `config.yaml` 路径 |
| `FISSUE_ENV` | `./.env` | 指定 env 文件路径（多环境切换用，如 `.env.prod`） |
| `FISSUE_LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error`，等价于全局 `-v` |

```bash
# 多环境示例：生产配置单独放
FISSUE_CONFIG=/etc/fissue/config.prod.yaml
FISSUE_ENV=/etc/fissue/.env.prod
```

> `FISSUE_LOG_LEVEL=debug` 会打印每次 HTTP 请求与 LLM 调用的 token 数，
> 排查问题时很有用，但输出量大。

---

### 3.2 `FISSUE_DATABASE_URL` —— 数据库（**必填**）

**格式**：SQLAlchemy 连接串。

| 场景 | 值 |
|---|---|
| PostgreSQL（推荐） | `postgresql+psycopg://用户:密码@主机:5432/库名` |
| 本地试跑（零依赖） | `sqlite:///E:/Fissue/data/fissue.db`（Windows） |
| 本地试跑（Linux/Mac） | `sqlite:////home/me/fissue.db`（注意 4 个斜杠 = 绝对路径） |
| 密码含特殊字符 | 需 URL 编码，如 `@` → `%40`，`:` → `%3A` |

```bash
# PostgreSQL 示例
FISSUE_DATABASE_URL=postgresql+psycopg://fissue:my%40pass@localhost:5432/fissue
```

**不填会怎样**：用内置默认 `postgresql+psycopg://fissue:fissue@localhost:5432/fissue`。

**PostgreSQL 首次准备**：

```bash
createdb -U fissue fissue          # 建库
fissue db init                     # 建表
fissue db check                    # 验证连通
```

**填错了什么表现**：

| 现象 | 原因 | 解决 |
|---|---|---|
| `无法连接数据库：...` | 服务没起 / 连接串错 | `pg_isready`；`fissue db check` |
| `database "fissue" does not exist` | 没建库 | `createdb -U fissue fissue` |
| `password authentication failed` | 密码错或含特殊字符未编码 | 编码特殊字符 |
| SQLite `unable to open database file` | 目录不存在 / 路径格式错 | 先 `mkdir -p data`；Windows 用 `sqlite:///E:/...` |

> **SQLite vs PostgreSQL**：SQLite 适合单机试用（零运维）；
> 常驻服务 + Web 并发建议 PostgreSQL（SQLite 并发写会锁表）。

---

### 3.3 LLM 相关

#### `FISSUE_LLM_API_KEY` —— **必填**

唯一的必填项。命令会校验它（`fissue eval` / `verify` / `fix` 等需要 AI 的命令
在不填时直接报错退出）。

```
缺少 LLM API Key：请在 .env 中设置 FISSUE_LLM_API_KEY
```

> 只做 `fetch` / `status` / `report` 不需要 key。

#### `FISSUE_LLM_BASE_URL` —— 端点地址

**不填**：用 `config.yaml` 的 `llm.base_url`（默认 `https://api.deepseek.com/v1`）。

各供应商对照（**只改这两项即可换供应商**）：

| 供应商 | `BASE_URL` | 推荐 `MODEL` | 备注 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | 便宜、中文好，默认 |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | 通用能力强 |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | 国内直连 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | 便宜 |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | 长上下文 |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:14b` | 完全离线，`API_KEY` 随便填 |

**注意末尾**：填 `https://api.deepseek.com/v1`（程序自动补 `/chat/completions`），
不要自己带 `/chat/completions`（带了也能识别，但容易写错）。

#### `FISSUE_LLM_MODEL` —— 模型名

**不填**：用 `config.yaml` 的 `llm.model`（默认 `deepseek-chat`）。

#### `FISSUE_LLM_FALLBACK_MODEL` —— 备用模型

主模型调用失败（限流、服务故障、模型名写错）时自动降级到它，避免整批任务中断。

**不填**：不降级，主模型失败即报错。

```bash
# 示例：主用便宜的，故障时降级到备用
FISSUE_LLM_MODEL=deepseek-chat
FISSUE_LLM_FALLBACK_MODEL=deepseek-chat
```

**填错了什么表现**：

| 现象 | 原因 |
|---|---|
| `LLM 请求失败 401` | key 错 / 与 base_url 不匹配（如用 OpenAI 的 key 调 DeepSeek） |
| `LLM 请求失败 404` | base_url 少了 `/v1` 或多了 `/chat/completions` |
| `模型调用失败（xxx）: ...` | model 名不存在 |
| `LLM 服务错误 429` | 限流；调小 `config.yaml` 的 `llm.concurrency` |
| 中文输出变成英文 | 换成了不擅长中文的模型（不影响功能） |

> 不支持的 `response_format`（结构化输出）会被**自动识别并降级**为文本解析，
> 所以本地小模型也能跑，只是偶尔需要重试。

---

### 3.4 平台 Token（可选，但建议配）

四个变量独立，**配哪个平台就填哪个**，用不到的留空。

```bash
FISSUE_GITHUB_TOKEN=
FISSUE_GITEE_TOKEN=
FISSUE_ATOMGIT_TOKEN=
FISSUE_GITLAB_TOKEN=
```

**不填会怎样**：

| | 有 Token | 无 Token |
|---|---|---|
| 读公开仓库 | ✅ | ✅ |
| 限流（GitHub） | 5000 次/小时 | **60 次/小时** |
| 读私有仓库 | ✅ | ❌ |
| 打标签 / 提 PR | ✅ | ❌（自动修复不可用） |

**申请入口与最小权限**：

| 平台 | 入口 | 最小权限 |
|---|---|---|
| GitHub | [settings/tokens](https://github.com/settings/tokens) | classic 勾 `repo`；或 fine-grained 勾 Contents + Issues + Pull requests 读写 |
| Gitee | [个人访问令牌](https://gitee.com/profile/personal_access_tokens) | `projects` `pull_requests` `issues` |
| AtomGit | [访问令牌](https://atomgit.com/setting/token-classic) | `read_repository` `write_repository` `issues` `pull_requests` |
| GitLab | [Access Tokens](https://gitlab.com/-/user_settings/personal_access_tokens) | `api`（或 `read_api` + `write_repository`） |

> **只试用不修复**：可以不填任何 Token，用公开仓库跑 `fetch`/`eval`/`verify`，
> 只是会很快触到 60 次/小时的限流。

**注意**：Token 等同于密码，**永远不要提交进 git**（`.env` 已在 `.gitignore`）。
日志里的 Token 会被自动脱敏成 `***`。

**填错了什么表现**：

| 现象 | 原因 |
|---|---|
| `401 Bad credentials` | Token 错 / 已过期 / 撤销了 |
| `404` 访问私有仓库 | Token 权限不含该仓库 |
| `403` 打标签失败 | Token 缺写权限（只勾了只读） |
| `触发限流` | 未配 Token 或调用过密 |

---

### 3.5 Web / API

#### `FISSUE_API_TOKEN` —— 访问令牌

**填了 = 自动开启鉴权**（无需再改 `config.yaml`）：所有 `/api/*` 请求必须带

```
Authorization: Bearer <你的token>
```

**留空 = 不鉴权**，仅适合本机。

```bash
# 生成一个强随机串
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
FISSUE_API_TOKEN=Kx7vQ2mN8pL4wR9tY6uZ3sD5fG1hJ0aB
```

> 对外暴露（`--host 0.0.0.0`）时**务必**填上，否则任何人都能触发修复与提 PR。
>
> 注意：`/api/v1/health` 与网页本身不需要鉴权；网页上的操作按钮走 API，
> 浏览器不会自动带 Token，所以对外开启鉴权后请用 API 而非网页按钮。

#### `FISSUE_WEB_HOST` / `FISSUE_WEB_PORT`

| 值 | 含义 |
|---|---|
| `127.0.0.1`（默认） | 仅本机可访问，最安全 |
| `0.0.0.0` | 所有网卡可访问，**需配 `FISSUE_API_TOKEN`** |

```bash
FISSUE_WEB_HOST=127.0.0.1
FISSUE_WEB_PORT=8000
```

**填错了什么表现**：

| 现象 | 原因 |
|---|---|
| `环境变量 FISSUE_WEB_PORT 必须是整数` | 端口填了非数字（已友好报错，不会抛 traceback） |
| `Address already in use` | 端口被占，换一个 |
| 局域网访问不到 | `WEB_HOST` 仍是 `127.0.0.1` |

---

### 3.6 `FISSUE_SANDBOX_RUNNER_SOCKET` —— 沙盒转发组件（可选）

**默认不填**：走 `inline` 模式——进程内直调 Docker，简单够用。

**填了**：走转发组件模式——宿主机常驻一个受控进程，容器执行只经这条通道，
调用方不持有 Docker 句柄。生产环境（Linux）推荐。

```bash
# 1) 起转发组件
fissue sandbox serve

# 2) 告诉客户端走这条通道
FISSUE_SANDBOX_RUNNER_SOCKET=/var/run/fissue-runner.sock
```

等价于 `config.yaml` 的 `sandbox.forwarder.socket`。

> **Windows 不支持 Unix domain socket**，会自动回退 `inline` 模式。
> 想让转发组件在 Windows 上工作，把值写成 `\\.\pipe\fissue-runner`。

**填错了什么表现**：

| 现象 | 原因 |
|---|---|
| `无法连接转发组件 /var/run/...` | 没起 `fissue sandbox serve`，或路径不一致 |
| `Windows 不支持 Unix socket` | 平台限制，改用 inline 或命名管道 |

---

### 3.7 TLS 证书（`FISSUE_CA_BUNDLE` / `FISSUE_INSECURE_SKIP_VERIFY`）

**默认可不填。** 只有遇到下面这个报错时才需要：

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate (_ssl.c:1082)
```

#### 为什么会这样

你的环境里存在 **HTTPS 中间人**，常见两类：

| 类型 | 例子 |
|---|---|
| 企业安全网关 | Zscaler、Netskope、深信服、Palo Alto… |
| 本机代理 / 抓包工具 | Fiddler、Charles、**SteamTools**、Clash(部分模式) |

它们会用自己的根证书**重签**所有 HTTPS 证书。因为该根证书装进了
**操作系统信任库**，所以浏览器访问正常；但 httpx 默认只信 **certifi** 的
CA 列表，没有这张证书 → 校验失败。

#### 解决方案（按推荐顺序）

**方案 1（默认已启用）：用系统信任库**

Fissue 依赖里已包含 [`truststore`](https://pypi.org/project/truststore/)，
它会自动把 TLS 后端切到操作系统信任库。装好依赖即可，无需配置。

> 如果你的旧环境没装：`pip install truststore`

**方案 2：显式指定根证书**

把代理/企业的根证书导出成 PEM，然后：

```bash
FISSUE_CA_BUNDLE=E:/certs/corp-root-ca.pem
```

**方案 3：临时关闭校验（仅排查）**

```bash
FISSUE_INSECURE_SKIP_VERIFY=1
```

> ⚠️ 这会关闭全部证书校验，**存在被中间人攻击的风险**。
> 只在本地确认「是不是证书问题」时短暂使用，绝不要写进生产配置。

#### 优先级

```
FISSUE_INSECURE_SKIP_VERIFY  >  FISSUE_CA_BUNDLE  >  系统信任库(truststore)  >  certifi
```

#### 验证是否修好

```bash
# 看当前用的是哪套 CA
fissue status --verbose          # 日志会说明 CA 来源

# 或直接测一次真实请求
fissue fetch --repo psf/requests --limit 1
```

#### 顺带说明：SSL 错误不会重试

证书错误是**确定性**的（重试不会让证书变好），所以 Fissue 遇到它会
**立即失败**并给出上面三个方案，而不是傻等 4 轮退避（约 8.5 秒）。
其它网络抖动（超时、连接重置）仍会正常重试。

---

## 4. 完整变量清单

| 变量 | 必需 | 默认 | 作用 |
|---|---|---|---|
| `FISSUE_DATABASE_URL` | ✅ | PostgreSQL 本地串 | 存储连接串 |
| `FISSUE_LLM_API_KEY` | ✅ | 无 | 大模型密钥 |
| `FISSUE_LLM_BASE_URL` | | `config.yaml` 值 | 模型端点 |
| `FISSUE_LLM_MODEL` | | `config.yaml` 值 | 模型名 |
| `FISSUE_LLM_FALLBACK_MODEL` | | 无 | 降级模型 |
| `FISSUE_GITHUB_TOKEN` | | 空 | GitHub 访问 |
| `FISSUE_GITEE_TOKEN` | | 空 | Gitee 访问 |
| `FISSUE_ATOMGIT_TOKEN` | | 空 | AtomGit 访问 |
| `FISSUE_GITLAB_TOKEN` | | 空 | GitLab 访问 |
| `FISSUE_API_TOKEN` | | 空（不鉴权） | Web/API 访问令牌 |
| `FISSUE_WEB_HOST` | | `127.0.0.1` | 监听地址 |
| `FISSUE_WEB_PORT` | | `8000` | 监听端口 |
| `FISSUE_SANDBOX_RUNNER_SOCKET` | | 空（inline） | 沙盒转发组件 socket |
| `FISSUE_CA_BUNDLE` | | 空（用系统信任库） | 指定 CA 证书 PEM 路径 |
| `FISSUE_INSECURE_SKIP_VERIFY` | | 空（开启校验） | 关闭 TLS 校验（**仅排查**） |
| `FISSUE_LOG_LEVEL` | | `info` | 日志级别 |
| `FISSUE_CONFIG` | | `./config.yaml` | 配置文件位置 |
| `FISSUE_ENV` | | `./.env` | env 文件位置 |

> 这份清单与 `.env.example` 由测试 `tests/test_env.py` 自动校验一致性：
> 代码会读的变量必须在示例里有说明，示例里列出的必须真的会被读取。

---

## 5. 验证你的 `.env` 有没有写对

```bash
# 1) 语法与取值能否成功加载
fissue status

# 2) 数据库通不通
fissue db check

# 3) LLM key 是否有效、模型名对不对
fissue eval --limit 1              # 会真的调一次模型
fissue eval --limit 1 --verbose    # 看请求细节

# 4) 平台 Token 是否有效
fissue fetch --repo <owner/name> --limit 1
fissue fetch --repo <owner/name> --limit 1 --verbose

# 5) 沙盒是否就绪
fissue sandbox check
```

`fissue status` 会把关键项一次性打出来：

```
配置仓库：psf/requests
沙盒：✅ Docker 26.0.0
条目状态 / 队列状态表格…
今日用量：0 tokens（prompt 0 / completion 0），约 $0.0000，调用 0 次
```

---

## 6. 配置模板

### 6.1 最小可用（本地试用，不碰平台写操作）

```bash
# .env —— 只填这一项就能评测
FISSUE_LLM_API_KEY=sk-你的真实key

# 可选：用 SQLite 免装 PostgreSQL
FISSUE_DATABASE_URL=sqlite:///E:/Fissue/data/fissue.db
```

> 但要做自动修复（提 PR）必须配对应平台的 Token。

### 6.2 开发环境（DeepSeek + SQLite + 本机 Web）

```bash
FISSUE_DATABASE_URL=sqlite:///E:/Fissue/data/fissue.db

FISSUE_LLM_API_KEY=sk-xxx
FISSUE_LLM_BASE_URL=https://api.deepseek.com/v1
FISSUE_LLM_MODEL=deepseek-chat

FISSUE_GITHUB_TOKEN=ghp_xxx

FISSUE_WEB_HOST=127.0.0.1
FISSUE_WEB_PORT=8000
FISSUE_API_TOKEN=

FISSUE_LOG_LEVEL=debug
```

### 6.3 生产（PostgreSQL + 四平台 + 鉴权 + 转发组件）

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

FISSUE_API_TOKEN=生成一个32字节随机串
FISSUE_WEB_HOST=0.0.0.0
FISSUE_WEB_PORT=8000

FISSUE_SANDBOX_RUNNER_SOCKET=/var/run/fissue-runner.sock

FISSUE_LOG_LEVEL=info
```

配套：生产环境建议在 `.env` 同级放一份 `config.yaml` 收紧自动修复策略
（见 [USAGE.md §9.2](USAGE.md#92-观察自动修复质量推荐的上线姿势)）。

### 6.4 完全离线（本地 Ollama）

```bash
FISSUE_LLM_API_KEY=ollama          # 任意非空值
FISSUE_LLM_BASE_URL=http://localhost:11434/v1
FISSUE_LLM_MODEL=qwen2.5:14b

FISSUE_DATABASE_URL=sqlite:///E:/Fissue/data/fissue.db
```

```bash
ollama serve                       # 另开终端
ollama pull qwen2.5:14b
```

> 小模型在「生成验证器」这类任务上成功率低于大模型，可能出现更多
> `needs_manual`，属正常现象。

### 6.5 CI（GitHub Actions Secrets）

不要提交 `.env`，用平台 Secrets 注入：

```yaml
- run: fissue serve --once
  env:
    FISSUE_DATABASE_URL: ${{ secrets.FISSUE_DATABASE_URL }}
    FISSUE_LLM_API_KEY: ${{ secrets.FISSUE_LLM_API_KEY }}
    FISSUE_GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}   # Actions 内置
    FISSUE_LOG_LEVEL: info
```

---

## 7. `.env` 写法注意事项

```bash
# ✅ 正确
FISSUE_LLM_API_KEY=sk-abc123
FISSUE_LLM_API_KEY="sk-abc 123"      # 含空格用引号
FISSUE_DATABASE_URL=postgresql+psycopg://u:p%40word@h:5432/db   # 密码特殊字符要 URL 编码

# ❌ 常见错误
FISSUE_LLM_API_KEY = sk-abc123       # = 两边不要加空格
export FISSUE_LLM_API_KEY=sk-abc123  # 不需要 export
FISSUE_LLM_API_KEY='sk-abc123'       # 单引号在某些 shell 下行为不同，用双引号或不用
FISSUE_WEB_PORT=8000   # 注释必须独占一行或用 #（行内 # 后面会被当注释）
```

行内注释可用，但要注意 `#` 会开始注释：

```bash
FISSUE_WEB_PORT=8000    # 这样写是可以的
```

**Token 脱敏**：Fissue 在日志与报告中会把 Token 替换为 `***`，
但 `.env` 文件本身请确保权限收紧：

```bash
chmod 600 .env
```

---

## 8. 相关文件

| 文件 | 作用 |
|---|---|
| `.env.example` | 变量清单与注释（提交进 git） |
| `.env` | 你的实际密钥（**不提交**） |
| `config.yaml` | 业务规则 |
| `docs/USAGE.md` | 完整使用手册 |
| `docs/DESIGN.md` | 架构与设计取舍 |
| `tests/test_env.py` | 变量接线与优先级的自动化校验 |
