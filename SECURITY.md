# 安全策略

Fissue 会**执行来自不可信来源的代码**（Issue 正文里的复现步骤、外部提交的 PR 补丁），
并把 AI 生成的补丁推送到他人仓库。因此安全不是附加项，而是产品的核心约束。

---

## 报告漏洞

**请不要用公开 Issue 报告安全问题。**

优先使用 GitHub 的私密渠道（**推荐**）：

> <https://github.com/John-is-playing/Fissue/security/advisories/new>
>
> 或：仓库页面 → **Security** 标签 → **Advisories** → **Report a vulnerability**

提交时会看到一份自定义表单（`.github/VULNERABILITY_REPORT.yml`），
它会引导你给出我们最需要的信息——**是否涉及沙盒逃逸**、复现步骤与 PoC。

若上述入口不可用，或你不便使用 GitHub，可发邮件至：

> **b297209694@outlook.com**（标题以 `[SECURITY]` 开头）

报告中请包含：

| 项 | 说明 |
|---|---|
| 影响 | 哪个组件、什么后果（逃逸 / 提权 / 数据外泄 / 令牌泄露…） |
| 复现 | 最小可复现步骤或 PoC |
| 版本 | commit hash 或版本号，以及运行平台 |
| 已知性 | 你是否已公开披露过 |

**响应时限（尽力而为，非 SLA）**：

| 阶段 | 时限 |
|---|---|
| 确认收到 | 7 天内 |
| 初步定级与初判 | 14 天内 |
| 修复或缓解方案 | 视严重度，尽量 90 天内 |

修复发布后，我们会在 `CHANGELOG.md` 的 Security 段致谢（除非你要求匿名）。

---

## 威胁模型

Fissue 的信任边界画在**沙盒**上。以下内容一律视为**不可信**：

1. **Issue / PR 的正文与评论**——可能包含提示注入（prompt injection），试图
   操纵评测、验证器生成或修复 Agent。
2. **外部提交的 PR 补丁**——即任意代码。它会在沙盒里被 `git apply` 并运行。
3. **被测仓库自身的代码**——包括其测试代码与构建脚本。
4. **AI 模型的输出**——验证器、补丁、路径都可能是恶意或错误的。

**宿主侧永不信任上述任一项**，包括模型自己的"我已经验证通过了"。

---

## 隔离设计（沙盒）

沙盒是防止不可信代码污染宿主的**唯一**屏障，默认参数如下：

| 约束 | 默认值 | 作用 |
|---|---|---|
| `network` | `none` | 断网，杜绝外联与回连 |
| `read_only_root` | `true` | 根文件系统只读，仅 `/tmp` 与 `/run` 为受限可写层（`noexec,nosuid`） |
| `user` | `1000:1000` | 非 root |
| capabilities | `--cap-drop=ALL` | 丢弃全部 Linux capabilities |
| | `no-new-privileges` | 禁止提权 |
| `cpus` / `memory_mb` / `pids` | `2.0` / `2048` / `256` | 限 CPU、内存、进程数 |
| `timeout_seconds` | `600` | 限执行时长 |
| 挂载 | 仅隔离工作区（临时目录，可选 loopback 虚拟盘） | **绝不挂载宿主普通目录** |
| `.git` 挂载 | 工作区以 rw 挂进容器，但 **`.git` 只读**（`index` 除外，默认 `sandbox.git_readonly: true`） | 掐掉**宿主的执行入口**（hooks、`filter.*.clean`、`diff.*.textconv`、`credential.helper`）与**伪造入口**（`objects`/`refs` 能伪造提交与引用、把内容混进面向上游的补丁）。留 `index` 可写只是为了让「顺手跑 `git status`」的测试套件别整片失败，而宿主提交前会把索引拉回 HEAD 再按白名单重新 stage |

「调用方不持有 Docker 句柄、只走受控的本地 socket 通道」**只在起了转发组件时成立**：
转发组件关闭、非 POSIX 平台、或没起 `fissue sandbox serve`（socket 文件不存在）时，
代码会走 `inline` 模式，Docker 句柄就在调用方进程内——**而这是默认情况**。
容器的隔离参数两种模式一致（下面这张表照常生效），但生产部署建议把转发组件起起来
（`fissue sandbox serve`），让执行也走受控通道。

> ⚠️ **本地执行没有任何隔离**（不禁网、非只读根、无资源上限），所以它必须由人
> 显式选择，绝不静默发生：
>
> - `runtime: docker`（默认）：Docker 不可用时**直接拒绝执行**并报错，不会降级。
>   确需降级须显式设 `sandbox.allow_local_fallback: true`。
> - `runtime: local`：明确要求在本机执行，仅限本机开发。
>
> 两条本地路径都**只按白名单透传环境变量**（`sandbox.local_env_allowlist`，
> 默认只含 `PATH`/`HOME`/`TMPDIR` 等运行必需项）。没有容器挡着时，被执行的
> 验证器与不可信 PR 代码可以任意组包外发，把宿主的 LLM key 与各平台 token
> 一并交给它等于直接交出密钥。
>
> 在跑不可信代码的场景下，本地执行都是**不安全配置**，请勿在生产使用。

另外三条与「承诺是否真的兑现」直接相关：

- **沙盒取值只认白名单**：`runtime` 只接受 `docker` / `local`。拼错（`Docker`、`podman`、
> 带尾空格）会在加载配置时被拒；即使绕过配置校验，运行期也 fail-closed 拒绝执行——
> **不会**静默落到无隔离的本机执行。
- **超时后强制回收容器**：`subprocess` 超时只 kill 得掉宿主上的 `docker` CLI，容器会留在
  后台继续吃资源（`--rm` 不触发）。现在容器一律带 `--name`，超时后显式 `docker rm -f`。
- **输出有硬上限**：单次执行每个流最多保留 2MB，超出部分丢弃并在 stderr 标注「已截断」。
  否则一个打印循环就能吃光宿主内存，或在转发组件那边撑爆一帧、让整条验证 stage 抛异常。

> `.git` 也是执行入口，见下节「宿主侧护栏」里的 `.git` 加固。

详细的架构与安全模型见 [`docs/DESIGN.md`](docs/DESIGN.md) §5。

---

## 自动修复的宿主侧护栏

模型说了不算，以下约束由宿主强制执行：

| 护栏 | 说明 |
|---|---|
| 路径白名单 | 只允许仓库内相对路径；`..` 与绝对路径直接拒绝 |
| 保护路径 | `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` 等拒绝写入 |
| 验证器锁定 | 修复期间验证器文件不可写，防「改测试骗过测试」 |
| 规模上限 | 变更文件数 ≤ `max_changed_files`、diff 行数 ≤ `max_diff_lines`，超出即回滚 |
| F2P 复核 | 提 PR 前重跑一次完整 F2P，不接受 Agent 自说自话 |
| 策略闸门 | 仅 Issue、仅 BUG、仅 tier1/tier2，排除疑似刷量 / 重复 |
| 令牌卫生 | 推送用**不带令牌**的 URL，令牌经子进程环境变量 + 凭据助手递给 git——不进命令行、不写入 git 配置；所有输出过 `sanitize()` |
| **`.git` 加固** | 容器对工作区可写，而容器里跑的是不可信 PR 代码与模型生成的验证器——`.git/hooks` 与 `.git/config`（`filter.*.clean` 在 `git add` 时执行、`diff.*.textconv` 在 `git diff` 时执行、`credential.helper` 更直接）都是**宿主的执行入口**。现在：宿主 git 一律带 `-c core.hooksPath=<devnull>`；克隆后与**每次沙盒跑完**都重建 `.git/config`、删掉 `.git/hooks`、`push --no-verify` |
| **写操作互斥** | 同一时刻只允许一处写外部仓库：跨进程用 `flock`（`fissue-*.lock`），同进程（`fissue serve --with-web`）另有 `WRITE_GATE`；Web 写端点拿不到回 `409`。flush 的批次用条件 UPDATE 原子抢占，同一批不会被两个写者各跑一次 |

**人工闸门**：PR **永不自动合并**，只打标签给建议；`approve-merge` 也只是
打标签 + 评论，合并始终由人在平台上手动完成。

---

## 入站 webhook（平台推送）

`webhooks.enabled: true` 时会注册 `POST {api.prefix}/webhooks/<平台>`，让平台把「新 Issue /
新 PR」直接推过来，省掉一个轮询间隔。

**这个端点刻意不带 bearer 鉴权**：平台既不会带 `Authorization`，也不会带我们的转发头，它就带
自己的签名——把 `FISSUE_API_TOKEN` 那套（以及「未鉴权写请求」的两道补刀）挂上去，在真实部署里
（尤其挂了反向代理时）这个端点会**永远收不到事件**。所以它的边界换成了下面三条，缺一不可：

| 边界 | 说明 |
|---|---|
| **签名 / 密钥** | GitHub 用 HMAC-SHA256（`X-Hub-Signature-256: sha256=<hex>`）；GitLab / Gitee / AtomGit 用共享密钥头（`X-Gitlab-Token` / `X-Gitee-Token`）。一律 `hmac.compare_digest` **常数时间**比对 |
| **fail-closed** | 某平台**没配密钥**时：配置层直接拒绝启动；请求期再拦一道返回 403。密钥与 API token 是两回事，**别复用** |
| **仓库白名单** | 只有 `repos` 里**登记且启用**的仓库会被接受，其余事件一律忽略（202 + 原因） |

事件体**只被解析、不被信任**：解析出的仓库与编号会再走一遍正常的抓取口径（游标、标签过滤、
早停都在），所以伪造一条「新 Issue」最多让我们多拉一次；而 webhook 触发的后台那一轮**同样在写锁
内**（同一时刻只有一个写进程），并且只回 202——它不为平台做任何同步等待。

**人工闸门不变**：webhook 触发的也只是「跑一轮」，PR 依旧不会自动合并。

---

## 部署者须知

如果你要对外提供服务或处理他人仓库：

- [ ] 打开 `.env` 里的 `FISSUE_API_TOKEN` 鉴权（留空 = 不鉴权，仅限本地）
- [ ] **挂了反向代理就必须设 `FISSUE_API_TOKEN`**：启动检查只能看绑定地址，看不见代理——
      「绑 127.0.0.1 + 同机 nginx 转出去」同样是无鉴权的写控制面。带转发头的写请求会 403，
      但那只是补刀，不是边界
- [ ] 不要用 `sandbox.runtime: local`、也不要打开 `sandbox.allow_local_fallback`
      处理不可信代码——本地执行没有任何隔离
- [ ] 用最小权限的 Token：抓取只读即可；仅自动修复需要写权限
- [ ] 限制 Web 监听地址（默认 `127.0.0.1`；对外暴露必须开鉴权）
- [ ] 用 webhook 时，给**每个已启用平台**配 `FISSUE_WEBHOOK_<平台>_SECRET`（默认关闭；开着却
      没配会被拒绝启动），并且**不要复用 API token 当 webhook 密钥**
- [ ] webhook 必须能被**平台服务器**访问到：只绑回环、或反代不放行 `/api/v1/webhooks/*` 都会
      让推送永远到不了（这一点看日志就知道：一条 webhook 日志都没有）
- [ ] 定期轮换平台 Token 与 LLM Key

---

## 支持的版本

安全修复只针对**最新主分支 / 最新发布版本**提供。旧版本请先升级再验证。
