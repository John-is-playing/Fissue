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

### 文档

- 新增架构设计（`docs/DESIGN.md`）、使用手册（`docs/USAGE.md`）、环境说明（`docs/ENV.md`）、回归门说明（`docs/REGRESSION-GATE.md`）
- 新增五套夹具各自的 README，说明每套夹具「考什么」与期望结果对照表
- 新增 `30ae40b-BUG.md`：基于 ratekit 实测的缺陷清单与逐项修复记录

### 安全

- 详见 [`SECURITY.md`](SECURITY.md)：沙盒隔离参数、自动修复护栏、威胁模型与部署检查清单

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
