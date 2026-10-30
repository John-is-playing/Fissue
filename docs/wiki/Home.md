# Fissue Wiki

> **Fissue 自动从 GitHub / Gitee / AtomGit / GitLab 检索 Issue / PR，用 AI 大模型判断
> 真实性 / 重要性 / 可行性 / PR 质量，生成验证器并在 Docker 沙盒中做 F2P 验证，
> 对「低难度 + 高重要性」的 Issue 自动修复并提 PR。**

维护一个开源仓库，最痛的不是写代码，而是面对一堆 Issue/PR 不知道**先看哪个**：
这条是真 Bug 还是用户用错了？这个 PR 真的修好了吗？哪些可以放心让 AI 先修？
Fissue 把「分诊（triage）→ 验证 → 修复」这一整套流程自动化，并留下**完整的评分理由与证据链**供人复核。

它以 CLI、Web 仪表盘、REST API、常驻服务四种形态交付，Python 3.10+，存储默认 SQLite（可选换 PostgreSQL，已完成**真实部署验证**）。

---

## 我该从哪里读？

| 我想…… | 看这几页 |
|---|---|
| 先搞清它到底做什么、靠不靠谱 | [项目概览](Overview.md) → [基准与可信度](Benchmark.md) |
| 尽快在本机跑通一遍 | [快速开始](Getting-Started.md) → [使用场景与配方](Workflows.md) |
| 填配置、查某个键 | [配置参考](Configuration.md) → [术语表](Glossary.md) |
| 查某条命令怎么用 | [CLI 参考](CLI-Reference.md) |
| 用 Web 工作台或 REST API | [Web 与 REST API](Web-and-API.md) |
| 报错了 | [故障排查](Troubleshooting.md) |
| 改代码、加功能 | [架构设计](Architecture.md) → [开发与发布](Development.md) |
| 加平台 / 加通知渠道 / 换模型 | [扩展指南](Extending.md) |
| 部署到服务器 | [部署与运维](Deployment.md) → [安全模型](Security.md) |
| 想贡献 | [贡献指南](Contributing.md) → [安全模型](Security.md) |

---

## 四类读者的入口

- **使用者**：[快速开始](Getting-Started.md) · [配置参考](Configuration.md) · [CLI 参考](CLI-Reference.md) · [Web 与 REST API](Web-and-API.md) · [使用场景与配方](Workflows.md) · [故障排查](Troubleshooting.md)
- **开发者**：[项目概览](Overview.md) · [架构设计](Architecture.md) · [验证器与 F2P](Verifier-and-F2P.md) · [AI 层](AI-Layer.md) · [数据模型](Data-Model.md) · [扩展指南](Extending.md) · [开发与发布](Development.md)
- **维护者**：[安全模型](Security.md) · [基准与可信度](Benchmark.md) · [贡献指南](Contributing.md) · [发布约定](Development.md#6-发布约定)
- **运维**：[部署与运维](Deployment.md) · [配置参考](Configuration.md) · [安全模型](Security.md) · [Web 与 REST API](Web-and-API.md#5-入站-webhook)

---

## 关于这套 Wiki

- 页面位于仓库的 `docs/wiki/`，采用 **GitHub Wiki 兼容**的形态：`Home` / `_Sidebar` / `_Footer` 加一组扁平命名页。
- 每页遵循同一约定：**本页给出结论与摘要，深水区仍以仓库文档为准**（[README](../../README.md)、[docs/DESIGN.md](../DESIGN.md)、[docs/USAGE.md](../USAGE.md)、[docs/ENV.md](../ENV.md)、[docs/AUTH.md](../AUTH.md)、[SECURITY.md](../../SECURITY.md)、[BENCHMARK.md](../../BENCHMARK.md)）。
- 链接一律是可点击的相对链接：页内互链用 `页面名.md`，指向仓库文档用 `../xxx.md`。在 GitHub、AtomGit 的网页浏览与任意本地 Markdown 阅读器里都能直接点开。
- **发布到平台 Wiki（GitHub / AtomGit）**：用 `python scripts/publish_wiki.py`——默认**干跑**（只报告会改什么），确认后加 `--push --remote <wiki 仓库的 git 地址>` 发布。脚本会按 Wiki 渲染器的要求把页间链接的 `.md` 去掉、把指向仓库文档的链接换成完整 URL，且**代码块与行内代码里的示例一律不动**。注意：平台的 wiki 仓库是**懒创建**的——必须先在网页上点「创建第一页」保存一次，否则 `git push` 会被拒（`Repository not found`），而且没有任何 API 能代做。

---

## 事实一处、结论两处

本项目对"跑分"和"结论"有一条硬规矩：**只写实测、已知失败照写、没跑过就写没跑**。
这条规矩同样适用于本 Wiki——凡涉及能力与测量的说法，都能在
[基准与可信度](Benchmark.md) 或源码/数据库里找到出处；凡未验证的，都会**显式标注为未验证**。

> 该项目绝大多数代码由 **AI Agent 驱动编写**，请带着这个前提阅读，注意辨识。
