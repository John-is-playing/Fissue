# 安全模型

> Fissue 会执行不可信代码并把 AI 补丁推向他人仓库——安全是核心约束，不是附加项。
> 全文见 [SECURITY.md](../../SECURITY.md)，架构视角见 [DESIGN.md §5](../DESIGN.md)。

## 1. 威胁模型

信任边界画在**沙盒**上。以下一律不可信，宿主侧永不信任（包括模型自称「已验证通过」）：

1. **Issue / PR 正文与评论**——可能含提示注入，操纵评测、验证器生成或修复 Agent；
2. **外部 PR 补丁**——即任意代码，会在沙盒里 `git apply` 并运行；
3. **被测仓库自身代码**——含其测试与构建脚本；
4. **AI 模型输出**——验证器、补丁、路径都可能恶意或错误。

## 2. 沙盒隔离

| 约束 | 默认 | 作用 |
|---|---|---|
| `network` | `none` | 断网，杜绝外联与回连 |
| `read_only_root` | `true` | 根只读，仅 `/tmp`、`/run` 受限可写（`noexec,nosuid`） |
| `user` | `1000:1000` | 非 root |
| capabilities | `--cap-drop=ALL` + `no-new-privileges` | 丢弃全部能力、禁止提权 |
| `cpus` / `memory_mb` / `pids` | `2.0` / `2048` / `256` | 资源上限 |
| `timeout_seconds` | `600` | 执行时长 |
| 挂载 | 仅隔离工作区 | 绝不挂宿主普通目录 |
| `.git` | 只读（`index` 除外） | 掐掉宿主的执行入口（hooks、`filter.*.clean`、`diff.*.textconv`、`credential.helper`）与伪造入口（`objects`/`refs`）。留 `index` 可写只为让顺手跑 `git status` 的测试套件别整片失败 |

**「调用方不持有 Docker 句柄」是有条件的**：只在起了转发组件时成立。组件关闭、非 POSIX、或没起
`fissue sandbox serve` 时走 `inline` 模式，句柄就在调用方进程内——**这是默认情况**。两种模式容器
隔离参数一致，但生产建议起转发组件。

**本地执行没有任何隔离**：`runtime: docker` 在 Docker 不可用时**直接拒绝执行**（要降级须显式
`allow_local_fallback`）；`runtime: local` 仅限本机开发。两条本地路径都只按白名单透传环境变量
（`sandbox.local_env_allowlist`）——没有容器挡着，被执行的验证器能任意组包外发，把宿主 key 与
token 一并交给它等于直接交出密钥。

三条与「承诺是否兑现」相关：**取值只认白名单**（`runtime` 拼错加载即拒，运行期还有 fail-closed）·
**超时强制回收容器**（`--rm` 在 subprocess 超时下不触发，故带 `--name` 后 `docker rm -f`）·
**输出硬上限 2MB**（超出丢弃并标注「已截断」）。

## 3. 宿主侧护栏（模型说了不算）

| 护栏 | 说明 |
|---|---|
| 路径白名单 | 只允许仓库内相对路径；`..` 与绝对路径拒绝 |
| 保护路径 | `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` 拒绝写入 |
| 验证器锁定 | 修复期间验证器不可写（防改测试骗过测试） |
| 规模上限 | `max_changed_files` / `max_diff_lines`，超出回滚 |
| 语法自检 | Python 落盘前 `compile()`，不合法拒绝该文件 |
| F2P 复核 | 提 PR 前重跑完整 F2P |
| 策略闸门 | 仅 Issue、仅 BUG、仅 tier1/tier2，排除疑似刷量/重复 |
| 令牌卫生 | 推送 URL 不带令牌，令牌经子进程环境变量 + 凭据助手递给 git；输出过 `sanitize()` |
| `.git` 加固 | 宿主 git 带 `-c core.hooksPath=<devnull>`；克隆后与每次沙盒跑完重建 `.git/config`、删 `.git/hooks`、`push --no-verify` |
| 写操作互斥 | 跨进程 `flock`、同进程 `WRITE_GATE`；Web 写端点拿不到回 409；flush 批次原子抢占 |

**人工闸门**：PR 永不自动合并，只打标签给建议；`approve-merge` 也只是标签 + 评论。

## 4. webhook 的三条边界

`webhooks.enabled: true` 时注册 `POST {api.prefix}/webhooks/<平台>`。该端点**刻意不带身份鉴权**
（平台只带自己的签名；挂上会话/Key 在真实部署里会永远收不到事件），边界换成三条，缺一不可：

| 边界 | 说明 |
|---|---|
| 签名 / 密钥 | GitHub HMAC-SHA256（`X-Hub-Signature-256`），其余共享密钥头（`X-Gitlab-Token` / `X-Gitee-Token`）；一律 `hmac.compare_digest` 常数时间比对 |
| fail-closed | 平台没配密钥：配置层拒绝启动，请求期再返 403。密钥与 API token 不是一回事，**别复用** |
| 仓库白名单 | 只接受 `repos` 里登记且启用的仓库，其余 202 + 原因 |

事件体只被解析、不被信任：解析出的仓库与编号仍走正常抓取口径，伪造事件最多多拉一次；后台
那一轮同样在写锁内，只回 202。

## 5. 部署者须知

- 开会话鉴权（默认 `web.auth.enabled: true`），`/login` 用一次性引导口令建管理员（用后即焚）
- **挂反代就声明 `web.trusted_networks`**——启动检查只能看绑定地址，看不见代理
- 脚本/CI 用 API Key（`Bearer fissue_<prefix>_<secret>`），别把登录口令贴命令行；不用的及时吊销
- 改口令后确认其它会话已注销（改口令会踢掉其它会话，这是有意的）
- 对外暴露保持 `web.auth.enabled: true`；**关掉不等于谁都能进**，而是只认回环与可信网段
- 不要用 `sandbox.runtime: local`，也不要开 `allow_local_fallback` 处理不可信代码
- Token 用最小权限（抓取只读，只有自动修复需要写）；Web 默认只绑 `127.0.0.1`
- webhook 给每个已启用平台配 `FISSUE_WEBHOOK_<平台>_SECRET`（可在 `/settings` 填），且**不要
  复用 API token**——Token 是「我们去调平台」，Secret 是「平台来调我们」的验签凭证
- 定期轮换平台 Token 与 LLM Key

## 6. 已知边界

- 单进程**内存态限速**在多进程部署下不共享。
- CIDR 默认不信转发头；要按真实客户端判定需**两层声明**：`web.trust_proxy=true` **且**
  `web.trusted_proxies` 非空（只填开关会加载期报错），且只取 `X-Forwarded-For` 的**最右一跳**。
- 安全修复只针对最新主分支 / 最新发布版本。

## 7. 报告漏洞

**不要用公开 Issue。** 优先 GitHub 私密渠道
<https://github.com/John-is-playing/Fissue/security/advisories/new>（表单见
`.github/VULNERABILITY_REPORT.yml`）；不便时发 **b297209694@outlook.com**（标题 `[SECURITY]`）。
请给：影响、最小复现/PoC、版本与平台、是否已公开披露。
时限（尽力而为，非 SLA）：确认 7 天内 · 定级 14 天内 · 修复尽量 90 天内。发布后在 CHANGELOG 致谢。

---

相关：[部署与运维](Deployment.md) · [架构设计](Architecture.md) · [验证器与 F2P](Verifier-and-F2P.md) ·
[Web 与 REST API](Web-and-API.md) · [SECURITY.md](../../SECURITY.md)