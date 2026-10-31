# Fissue Wiki

> 自动抓取 GitHub / Gitee / AtomGit / GitLab 的 Issue 与 PR，用 AI 判断**真实性 / 重要性 / 可行性 /
> PR 质量**，生成验证器在 Docker 沙盒做 F2P 验证，对「低难度 + 高重要性」的 Issue 自动修复提 PR
> ——**永不自动合并**。核心主张：**别信模型的自述，要看独立证据。**

Python 3.10+，四种交付形态（CLI / Web 工作台 / REST API / 常驻服务），存储默认 SQLite（可换
PostgreSQL / MySQL）。本项目绝大多数代码由 AI Agent 驱动编写，请带此前提阅读。

## 我该从哪里读

| 我想…… | 看这几页 |
|---|---|
| 搞清它做什么、靠不靠谱 | [项目概览](Overview.md) → [基准与可信度](Benchmark.md) |
| 尽快在本机跑通 | [快速开始](Getting-Started.md) → [使用场景](Workflows.md) |
| 填配置、查某个键 | [配置参考](Configuration.md) → [术语表](Glossary.md) |
| 查某条命令 | [CLI 参考](CLI-Reference.md) |
| 用 Web 或 REST API | [Web 与 REST API](Web-and-API.md) |
| 报错了 | [故障排查](Troubleshooting.md) |
| 改代码、加功能 | [架构设计](Architecture.md) → [开发与发布](Development.md) |
| 加平台 / 通知渠道 / 换模型 | [扩展指南](Extending.md) |
| 部署到服务器 | [部署与运维](Deployment.md) → [安全模型](Security.md) |
| 想贡献 | [贡献指南](Contributing.md) → [安全模型](Security.md) |

## 四类读者的入口

- **使用者**：[快速开始](Getting-Started.md) · [配置参考](Configuration.md) · [CLI 参考](CLI-Reference.md) · [Web 与 REST API](Web-and-API.md) · [使用场景](Workflows.md) · [故障排查](Troubleshooting.md)
- **开发者**：[项目概览](Overview.md) · [架构设计](Architecture.md) · [验证器与 F2P](Verifier-and-F2P.md) · [AI 层](AI-Layer.md) · [数据模型](Data-Model.md) · [扩展指南](Extending.md)
- **维护者**：[安全模型](Security.md) · [基准与可信度](Benchmark.md) · [贡献指南](Contributing.md) · [开发与发布](Development.md)
- **运维**：[部署与运维](Deployment.md) · [配置参考](Configuration.md) · [安全模型](Security.md) · [Web 与 REST API](Web-and-API.md)

## 关于这套 Wiki

- 页面在 `docs/wiki/`，采用 **GitHub Wiki 兼容**形态：`Home` / `_Sidebar` / `_Footer` + 一组扁平页。
- 每页约定：**先给结论与摘要，深水区指向仓库文档**——[README](../../README.md)、
  [DESIGN](../DESIGN.md)、[USAGE](../USAGE.md)、[ENV](../ENV.md)、[AUTH](../AUTH.md)、
  [REGRESSION-GATE](../REGRESSION-GATE.md)、[SECURITY](../../SECURITY.md)、[BENCHMARK](../../BENCHMARK.md)。
- 链接一律是相对链接：页间互链 `页面名.md`，仓库文档 `../xxx.md`，根目录 `../../xxx.md`。
- **发布到平台 Wiki**：`python scripts/publish_wiki.py` 默认干跑，确认后加 `--push --remote <wiki 地址>`。
  脚本会去掉页间链接的 `.md`、把仓库文档链接换成完整 URL，且不动代码块内示例。平台的 wiki 仓库
  是**懒创建**的——必须先在网页上保存一次首页，否则 `git push` 会被拒。

## 一条硬规矩

**只写实测、已知失败照写、没跑过就写没跑。** 凡涉及能力与测量的说法，都能在
[基准与可信度](Benchmark.md) 或源码/数据库里找到出处；凡未验证的，都会显式标注为未验证。