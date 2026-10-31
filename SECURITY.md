# 安全策略

Fissue 会**执行不可信代码**（Issue 里的复现步骤、外部 PR 补丁），并把 AI 生成的补丁推向
他人仓库。安全是核心约束，不是附加项。

---

## 报告漏洞

**不要用公开 Issue。** 优先 GitHub 私密渠道：
<https://github.com/John-is-playing/Fissue/security/advisories/new>（或仓库 → Security → Advisories
→ Report a vulnerability），表单见 `.github/VULNERABILITY_REPORT.yml`。

不便使用时发邮件 **b297209694@outlook.com**（标题以 `[SECURITY]` 开头）。

请给出：影响（组件与后果）、复现步骤或 PoC、版本与平台、是否已公开披露。

| 阶段 | 时限（尽力而为，非 SLA） |
|---|---|
| 确认收到 | 7 天内 |
| 初步定级 | 14 天内 |
| 修复或缓解 | 视严重度，尽量 90 天内 |

修复发布后在 `CHANGELOG.md` 的 Security 段致谢（除非要求匿名）。

---

## 威胁模型

信任边界画在**沙盒**上。以下一律不可信：Issue/PR 正文与评论（提示注入）、外部 PR 补丁、
被测仓库自身代码与测试、**AI 模型的输出**（验证器、补丁、路径）。

宿主侧永不信任上述任一项，包括模型自称「已验证通过」。

---

## 沙盒隔离

| 约束 | 默认值 | 作用 |
|---|---|---|
| `network` | `none` | 断网，杜绝外联与回连 |
| `read_only_root` | `true` | 根只读，仅 `/tmp`、`/run` 为受限可写层（`noexec,nosuid`） |
| `user` | `1000:1000` | 非 root |
| capabilities | `--cap-drop=ALL` + `no-new-privileges` | 丢弃全部能力、禁止提权 |
| `cpus` / `memory_mb` / `pids` | `2.0` / `2048` / `256` | 限 CPU / 内存 / 进程数 |
| `timeout_seconds` | `600` | 限执行时长 |
| 挂载 | 仅隔离工作区（临时目录，可选 loopback 虚拟盘） | 绝不挂宿主普通目录 |
| `.git` | 只读（`index` 除外，`sandbox.git_readonly`） | 掐掉宿主的执行入口（hooks、`filter.*`、`diff.*.textconv`、`credential.helper`）与伪造入口（`objects`/`refs`） |

**转发组件只在起了 `fissue sandbox serve` 时生效**；否则走 `inline` 模式，Docker 句柄在调用方
进程内（这是默认情况）。容器隔离参数两种模式一致，生产部署建议起转发组件。

```text
调用方 ──ExecRequest──► ForwarderClient ──本地 socket──► ForwarderServer ──► DockerRuntime ──► 隔离容器
```

⚠️ **本地执行没有任何隔离**（不禁网、非只读根、无资源上限），必须由人显式选择：

- `runtime: docker`（默认）：Docker 不可用时**直接拒绝执行**，不降级；确需降级须显式
  `allow_local_fallback: true`。
- `runtime: local`：仅限本机开发。
- 两条本地路径都只按白名单透传环境变量（`sandbox.local_env_allowlist`）。宿主环境里有 LLM key
  与各平台 token，整份透传等于直接交密钥。

另有三条与「承诺是否兑现」直接相关：

- **取值只认白名单**：`runtime` 只接受 `docker` / `local`，拼错在加载配置时被拒，运行期还有
  fail-closed，不会静默落到无隔离的本机执行。
- **超时强制回收容器**：容器一律带 `--name`，超时后 `docker rm -f`（`--rm` 在 `subprocess`
  超时下不触发）。
- **输出硬上限**：单流最多保留 2MB，超出丢弃并在 stderr 标注「已截断」。

---

## 宿主侧护栏

模型说了不算，以下由宿主强制执行：

| 护栏 | 说明 |
|---|---|
| 路径白名单 | 只允许仓库内相对路径；`..` 与绝对路径直接拒绝 |
| 保护路径 | `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` 拒绝写入 |
| 验证器锁定 | 修复期间验证器文件不可写，防「改测试骗过测试」 |
| 规模上限 | 变更文件数 ≤ `max_changed_files`、diff 行数 ≤ `max_diff_lines`，超出回滚 |
| F2P 复核 | 提 PR 前重跑完整 F2P，不接受 Agent 自述 |
| 策略闸门 | 仅 Issue、仅 BUG、仅 tier1/tier2，排除疑似刷量/重复 |
| 令牌卫生 | 推送 URL 不带令牌，令牌经子进程环境变量 + 凭据助手递给 git；输出过 `sanitize()` |
| `.git` 加固 | 宿主 git 带 `-c core.hooksPath=<devnull>`；克隆后与每次沙盒跑完重建 `.git/config`、删 `.git/hooks`；`push --no-verify` |
| 写操作互斥 | 跨进程 `flock`（`fissue-*.lock`），同进程 `WRITE_GATE`；Web 写端点拿不到锁回 `409`；flush 批次用条件 UPDATE 原子抢占 |

**人工闸门**：PR 永不自动合并，只打标签给建议；`approve-merge` 也只是标签 + 评论。

---

## 入站 webhook

`webhooks.enabled: true` 时注册 `POST {api.prefix}/webhooks/<平台>`，让平台直接推送事件。
该端点**刻意不带身份鉴权**（平台只带自己的签名），边界换成三条，缺一不可：

| 边界 | 说明 |
|---|---|
| 签名 / 密钥 | GitHub 用 HMAC-SHA256（`X-Hub-Signature-256`），其余用共享密钥头（`X-Gitlab-Token` / `X-Gitee-Token`）；`hmac.compare_digest` 常数时间比对 |
| fail-closed | 平台没配密钥：配置层拒绝启动，请求期再返 403。密钥与 API token 不是一回事，**别复用** |
| 仓库白名单 | 只接受 `repos` 里登记且启用的仓库，其余 202 + 原因 |

事件体只被解析、不被信任：解析出的仓库与编号仍走正常抓取口径，伪造事件最多多拉一次；
后台那一轮同样在写锁内，只回 202，不为平台同步等待。

---

## 部署者清单

- [ ] 开启会话鉴权（默认 `web.auth.enabled: true`），在 `/login` 建管理员（一次性引导口令见控制台
      与 `data/auth-bootstrap-token`，用后即焚）
- [ ] 挂反向代理就声明 `web.trusted_networks`——启动检查只看绑定地址，看不见代理
- [ ] 脚本/CI 用 API Key（`Authorization: Bearer fissue_<prefix>_<secret>`），别把登录口令贴命令行
- [ ] 对外暴露保持 `web.auth.enabled: true`；关掉不等于谁都能进，而是只认回环与可信网段
- [ ] 不要用 `sandbox.runtime: local`，也不要在处理不可信代码时开 `allow_local_fallback`
- [ ] 平台 Token 用最小权限：抓取只读，只有自动修复需要写
- [ ] 用 webhook 时给每个已启用平台配 `FISSUE_WEBHOOK_<平台>_SECRET`，且必须能被平台服务器访问到
- [ ] 定期轮换平台 Token 与 LLM Key

---

## 相关文档

[docs/DESIGN.md §5 安全模型](docs/DESIGN.md) · [docs/AUTH.md](docs/AUTH.md) ·
[docs/wiki/Security.md](docs/wiki/Security.md) · [docs/wiki/Deployment.md](docs/wiki/Deployment.md)

安全修复只针对**最新主分支 / 最新发布版本**。