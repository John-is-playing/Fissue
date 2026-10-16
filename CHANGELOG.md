# 更新日志

本文件记录 Fissue 的重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> **关于版本**：项目尚未发布首个带标签的版本（`0.1.0` 见 `pyproject.toml`）。
> 在 `1.0.0` 之前，`0.x` 的次版本号可能包含不向后兼容的变更。
> 首个正式版本发布后，本节将替换为规范的版本记录。

---

## [未发布]

### 新增

- **四平台适配器**：GitHub、Gitee、AtomGit（与 Gitee 共用 `v5.py` 实现）、GitLab（含自建实例，支持 `api_base`）
- **四维 AI 评测**：真实性 / 重要性 / 可行性 / PR 质量，每维给出理由与原文证据
- **反刷子机制**：误报识别、重复检测、AI 灌水识别，命中即归零优先级
- **验证器生成 + F2P 验证**：`base` 阶段必须失败、`fix` 阶段必须通过，两者同时成立才判定 `f2p_satisfied`
- **既有测试回归门**（`regression_gate`）：`off | warn | strict` 三档，拦住「修好目标却弄坏别处」的修复
- **自动修复 Agent 循环**：读文件 → 决策 → 改文件 → 跑验证器 → 看结果，未通过则继续（受 `agent_max_rounds` 限制）
- **Docker 沙盒**：禁网、只读根、非 root、`cap-drop=ALL`、限 CPU/内存/PID/时长；调用方不持 Docker 句柄，走本地 socket 转发组件
- **宿主侧硬护栏**：路径白名单、保护路径、验证器锁定、变更规模上限、提 PR 前 F2P 复核
- **四种交付形态**：CLI（typer）+ Web 仪表盘 + REST API（FastAPI）+ 常驻服务（APScheduler）
- **预算熔断**：token / 美元双预算（按仓库按天），支持 `hard_stop` 或仅告警；主模型失败降级 `fallback_model`
- **通知渠道**：console / webhook / 企业微信 / 钉钉 / 飞书，按 `dedup_key` 去重
- **导出**：JSON / Markdown，支持按分类与状态过滤
- **优先级分档策略可配置**：`importance`（默认）/ `dual`（取严）/ `custom`（用户函数）
- **测试夹具体系**：textkit、ratekit、pipekit、envkit、lockkit 五套对抗夹具（60 Issue + 12 PR），覆盖单函数语义、数值边界、跨模块调用链、状态副作用、资源不变量
- **增量抓取**：靠 `items.content_hash`（标题/正文/标签数/评论数/状态任一变化才算变更）
- **`fissue requeue`**：把「只能人工放行」的条目放回流水线——`--stale`（内容实质变化后结论已过期、但当时修复已开工的条目）与 `--failed`（失败条目按已有结论回退到该走的下一步），默认先列计划再确认
- **常驻服务的验证开关**：`schedule.verify_enabled`（关掉即把验证交给人工 `fissue verify`）与 `schedule.verify_batch_limit`（每轮最多验多少条，默认 30；积压由后续轮次继续消化）
- **`sandbox.git_readonly`**：把工作区里的 `.git` 以只读挂进容器（`index` 除外），默认开启；测试套件需要容器写 git 时可关掉

### 变更

- 重复检测：中文分词由**单字**改为**字符二元组（bigram）**，相似度由 Jaccard 改为**包含度**（交 / 较短一侧），阈值经实测标定为 **0.22**
- 重复检测：候选范围补上**正文同源**条目，判重口径保持从严
- 重复检测：候选只取**同类型、更早**的条目（Issue 的重复对象不是"修它的那个 PR"）
- 结论优先级：规则优先级从「verify 队列专属」提升为**所有队列统一**，并回写落库，保证展示层与存储层一致
- 回归门默认 `warn`：base 阶段既有测试本就不绿的老仓库，一律视为**不可信并放行**，避免大面积误杀
- 评测提示词：区分「API 设计偏好变更」与真缺陷，行为偏好变更判低真实性，但**不误伤新增功能**
- 验证器提示词：落实正文的兼容性声明为回归断言；要求逐条覆盖正文里每个复现用例

### 修复

- **Windows**：本地沙盒不再误选 WSL 的 `bash` 垫片（受限 PATH 下从 `git` 安装根推导 POSIX shell）
- **沙盒**：验证器自错与真复现的区分——按异常抛出位置判定，不再误伤「库自身抛异常」的真缺陷；自错识别补上 `TypeError`
- **沙盒**：可执行验证器缺少 `command` 时带反馈重新生成，而非静默降级为空跑
- **LLM**：`chat_json` 解析失败时带原始输出重试；长度截断时自动加大 `max_tokens`（默认预算提到 16384），缓解推理模型截断
- **修复流程**：dry-run 不消费待修队列，未产出 PR 的条目留在 `fix_queued`；修复候选只取仍排队的条目，避免反复重修同一批
- **修复流程**：Agent 循环中验证器通过即收工；修复重试闸门只看成功；dry-run 的成功不再计入失败
- **产物**：补丁以二进制纯 LF 落盘，保证能被 `git apply`
- **PR 验证**：功能类 PR 恢复验证；纯文档 PR 明确不推荐合并；GitLab/V5 抓取 PR 时也落文件清单
- **队列**：疑似重复 / 刷量的条目不再进入自动修复；重评后不再够格时收敛 `fix_queued` 状态
- 子进程输出统一按 UTF-8 解码
- **队列**：flush 只把**真拿到结论**的条目算作已消费——原先模型漏项或整批被长度截断时条目照样被标记已 flush，从此再也不会被 flush 看到（拿不到结论、打不上标签、进不了修复队列），而整条链路还报成功
- **队列**：批次改为条件 UPDATE **原子抢占**，同一批不会被两个写者（daemon / Web / CLI）各跑一次（重复计费 + 重复打标签）；崩溃残留按超龄放回，未拿到结论的留待下轮并带重试上限
- **队列**：回收「入队后未收尾」的残留——进程中断会让行永久停在 `pending`，把「队列已无待处理条目」这条兜底永久废掉，条目也卡在 `queued` 再也无人碰
- **数据**：内容**实质变化**（标题/正文/标签/状态/merged）后重评。此前 `changed_keys` 没有任何消费者、`content_hash` 又在同一处被无条件覆写，结论永远停在旧内容上且事后无法判定过期；评论增长不算实质变化（否则每条评论烧一次 token）
- **数据**：内容变更时若已有结论**已进入修复**（`fix_queued`/`fixing`/`pr_created`），不回落重评（免得重复修复 / 重复建 PR），只把「结论过期」如实留痕并在导出与 API 中可见
- **数据**：评论抓取残缺时不再用空结果覆盖已入库讨论，评测阶段也跳过残缺条目（结论会被回写平台，据残缺数据下结论等于把错误固化）
- **导出**：概览与明细统一过滤口径；计数改聚合查询、批量结论一次取回（原先逐条查询 + 十万级对象加载）
- **抓取**：早停判定改用适配器**交出来的**条数——原先用过滤后的计数，配了标签过滤时游标会照推，`limit` 之后的旧条目永久落在增量窗口外
- **平台**：建 PR/MR 的幂等探测只认 open/opened 且必须按分支核对。GitLab 原先没有覆写 `find_open_pr`（永远查不到已有 MR），v5 的探测会「拿列表第一条充数」→ 把别人的 PR 记成自己建的
- **平台**：AtomGit 打标签改为**读改写取并集**——`PATCH` 的 `labels` 语义未经验证，若是替换语义就会抹掉维护者原有的标签；读不回现值则拒绝写
- **平台**：`_is_already_exists` 不再「409/422 就算已存在」（422 也会因标签名不合法等原因返回，误判会让打标签在缓存 TTL 内静默失效）
- **通知**：去重占位失败不再当成「已发过」——一次 `database is locked` 原先会让告警永久消失还报成功
- **沙盒**：超时后**强制回收容器**（原先只 kill 宿主的 `docker` CLI，容器留在后台按限额吃资源且永不释放）
- **沙盒**：单流输出保留上限 2MB，超出丢弃并标注「已截断」（原先无上限，一个打印循环就能吃光内存，或撑爆转发组件的帧让整条验证 stage 抛异常）
- **沙盒 / 转发组件**：客户端超时按**本次请求**算（原先被第一个请求固化，长任务半途假超时）；服务端每连接一线程 + 读请求超时（原先一条只连不说的连接能把服务锁死）
- **数据库**：SQLite 开 WAL 并把 busy 超时提到 30s（驱动默认 5s，回滚日志模式下长读事务会挡住写者提交）
- **CLI**：日志一律写 stderr——`RichHandler` 默认落在 stdout，一句 WARNING 就能把 `fissue status --json` 的输出打坏（实测 `JSONDecodeError`）
- **常驻服务**：补齐**验证**这一步。`run_once` 原先只做抓取 → 评测 → flush → 修复，条目停在 `evaluated`，「部署成服务它自己转」这条链是断的；现按 CLI 口径接上（带镜像前置检查与预算门），Issue-FEATURE 不验证

### 文档

- 新增架构设计（`docs/DESIGN.md`）、使用手册（`docs/USAGE.md`）、环境说明（`docs/ENV.md`）、回归门说明（`docs/REGRESSION-GATE.md`）
- 新增五套夹具各自的 README，说明每套夹具「考什么」与期望结果对照表
- 新增 `30ae40b-BUG.md`：基于 ratekit 实测的缺陷清单与逐项修复记录
- 新增 `BENCHMARK.md`：五套夹具的公开基准，含方法学、实测结果、已知失败与尚未测量项
- 新增 `CONTRIBUTING.md` / `SECURITY.md` / `CODE_OF_CONDUCT.md` / `LICENSE`，
  以及 `.github` 下的 Issue 模板、PR 模板与漏洞上报表单
- `BENCHMARK.md` 补第三轮 P0/P1 审计台账，并**更正两处过时留档**（「独立 Web 进程不持锁」已不成立；SQLite 的 busy 超时并非「没有」而是驱动默认 5s）
- `SECURITY.md` 补 `.git` 加固与写操作互斥两条护栏、隔离一节补「取值白名单 / 超时回收容器 / 输出上限」，部署清单补「挂反向代理必须设 `FISSUE_API_TOKEN`」
- `docs/USAGE.md` / `docs/ENV.md` / `README.md` 与现状对齐：`FISSUE_CA_BUNDLE` 不可用时**直接报错**（不再静默回退系统信任库）、命令表补 `fissue requeue`、配置示例补 `sandbox.git_readonly` 与 `schedule.verify_enabled`·`verify_batch_limit`、常驻服务的扫描循环补上「验证」这一步

### 安全

- 详见 [`SECURITY.md`](SECURITY.md)：沙盒隔离参数、自动修复护栏、威胁模型与部署检查清单
- **容器 → 宿主逃逸（P0）**：容器对工作区可写，而 `.git` 是宿主的**执行入口**（`hooks/`、`filter.*.clean` 在 `git add` 时执行、`diff.*.textconv` 在 `git diff` 时执行、`credential.helper` 更直接）——容器里的不可信代码只要写下 `.git/hooks/pre-push`，宿主随后的 `git push`（环境里正带着平台令牌）就会执行它。现在：宿主 git 一律 `-c core.hooksPath=<devnull>`；克隆后与**每次沙盒跑完**都重建 `.git/config`、删掉 `.git/hooks`；`push --no-verify`
- **配置 fail-open（P0）**：`sandbox.runtime` 只要不等于精确的 `"docker"`（`Docker`、`podman`、带尾空格）就会**静默**落到无隔离的本地执行，绕过 `allow_local_fallback` 这道显式开关。现在配置层用白名单校验、运行期再 fail-closed 一道
- **无鉴权写控制面**：`api.enabled` 且对外可达却没有鉴权时**拒绝启动**；7 个写端点一律互斥（跨进程 `flock` + 进程内闸门，拿不到回 409），flush 批次原子抢占；未开鉴权时带反向代理转发头的写请求直接 403——**注意这只是补刀**，边界始终是 `FISSUE_API_TOKEN`
- **显式 CA 不可用**：`FISSUE_CA_BUNDLE` 指向不存在的路径或内容非法时改为**报错**，不再静默回退系统信任库（那等于「以为只信某张根证书，实际用的是 OS 信任库」），并展开 `~`
- **提交内容完整性**：`.git` 对容器可写，而 `git commit` 提交的是**索引**——现在提交前会把索引拉回 HEAD、确认仍在自己的分支上、并核对「将要提交的文件正好是 Agent 改过的那些」，多出来的一律拒绝提交（否则容器塞进索引的内容会跟着补丁被推出去）
- **未鉴权写请求再加一道**：除转发头之外，来源不是回环地址的写请求也直接 403——启动检查读的是 `web.host`，把 `create_app()` 嵌进别人的服务器时它看不见。仍然**只是补刀**，边界始终是 `FISSUE_API_TOKEN`
- **`.git` 只读挂进容器**（`sandbox.git_readonly`，默认开）：`.git` 既是宿主的**执行入口**（hooks、`filter.*.clean` 在 `git add` 时执行、`diff.*.textconv` 在 `git diff` 时执行、`credential.helper`），也是**伪造入口**（`objects`/`refs` 能伪造提交与引用，把内容混进面向上游的补丁）。现在整块只读、仅 `index` 可写（留它是为了别让顺手跑 `git status` 的测试套件整片失败，而宿主提交前会把索引拉回 HEAD）。真容器验证：`.git/config`、`.git/hooks`、`.git/refs`、`.git/objects` 写操作一律 `Read-only file system`，工作区、`.git/index`、`git status` 照常可用

---

## 历史提交

<details>
<summary>展开完整提交记录（40 条，2026-10-03 ~ 2026-10-06）</summary>

### 功能

| Commit | 说明 |
|---|---|
| `9513c15` | 验证器生成带上公开 API 契约，避免模型臆测方法名与返回类型 |
| `ebcc90f` | 优先级分档策略可配置（importance/dual/custom） |
| `7bddf08` | 既有测试回归门，拦住「修好目标却弄坏别处」的修复 |
| `e4da5dd` | 验证器落实正文的兼容性声明为回归断言 |
| `288dd04` | Fissue —— Issue/PR 的 AI 评测、沙盒验证与自动修复系统 |

### 测试夹具

| Commit | 说明 |
|---|---|
| `f86df4a` | 新增 pipekit 纯净测试夹具（15 Issue + 5 PR） |
| `081cc1e` | 新增 envkit 纯净测试夹具（15 Issue + 5 PR） |
| `5e9de67` | 新增 ratekit 纯净测试夹具（10 Issue + 3 PR） |
| `756b440` | 新增 textkit 测试夹具与一键搭建脚本 |
| `3612bc5` | 补回归用例与离线网络守卫，沙盒镜像在国内可构建 |
| `11d12c1` | 补 PR-FEATURE 验证与纯文档 PR 判定用例 |

### 修复

| Commit | 说明 |
|---|---|
| `30a204d` | 自错识别补上 TypeError，避免「用错语法」的验证器被当成已复现 |
| `64538f1` | GitLab/V5 抓取 PR 时也落文件清单，补齐纯文档判定 |
| `78e0758` | 功能类 PR 恢复验证，纯文档 PR 明确不推荐合并 |
| `57a24c0` | 长度截断时自动加大 max_tokens，默认预算提到 16384 |
| `64dfcbb` | chat_json 解析失败时带原始输出重试，缓解推理模型截断 |
| `e68bd33` | 干跑不消费待修队列，未产出 PR 的条目留在 fix_queued |
| `2fb3a1c` | 验证器自错按异常抛出位置判定，不再误伤「库自身抛异常」的真缺陷 |
| `83f05ef` | 修复候选只取仍排队的条目，避免反复重修同一批 |
| `2fe3ff7` | 区分验证器自错与真复现，并把刷量/重复挡在验证阶段之前 |
| `52e87e6` | 重复预筛补召回正文同源条目，判重口径保持从严 |
| `4184d9d` | 设计偏好规则限定为「替换现有行为」，不误伤新增功能 |
| `8aa6bf4` | 评测提示词区分「API 设计偏好变更」与真缺陷 |
| `1725e85` | 可执行验证器缺 command 时带反馈重生成 |
| `9befe80` | dry-run 下修复汇总与明细统一口径 |
| `f361b71` | Windows 本地沙盒不再误选 WSL 的 bash 垫片 |
| `bd6d55d` | 重复预筛支持中文，结论优先级一律以规则为准 |
| `a9cd76b` | 重评后不再够格自动修复时收敛 fix_queued 状态 |
| `5464e81` | Agent 循环验证器通过即收工；子进程输出按 UTF-8 解码 |
| `8dabc0f` | 修复重试闸门只看成功；dry-run 的成功不再计入失败 |
| `a78983c` | 补丁产物以二进制纯 LF 落盘，保证能被 git apply |
| `b01ba50` | 疑似重复/刷量的条目不再进入自动修复 |
| `5324238` | 修正 PR 验证顺序、优先级准入、重复检测与队列约束 |

### 文档

| Commit | 说明 |
|---|---|
| `f72381b` | 修正 ratekit #3 的期望为 tier1，补记 E/F 实测复测 |
| `c8c5f18` | 补记 C/D/E/F 的修复过程并收敛遗留清单 |
| `d5a68cb` | 记录 ratekit 实测发现的缺陷清单与修复过程 |

### 构建 / CI

| Commit | 说明 |
|---|---|
| `2a5d5b1` | Change destination repository in hubmirror.yml |
| `3d7cb36` | Update hubmirror.yml for repo mirroring configuration |
| `8fa334e` | Update Hub Mirror Action to use master branch |
| `6ab438c` | Add Hub Mirror Action workflow |

</details>

---

[未发布]: https://github.com/John-is-playing/Fissue/commits/main
