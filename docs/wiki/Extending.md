# 扩展指南

> 扩展点统一遵循同一套模式：**实现接口 → 注册进注册表 → 在配置里启用**。

## 1. 加平台适配器

```
继承 PlatformAdapter → 实现 _headers / fetch_items / fetch_diff / item_url
                    → 注册进 platforms/registry.py:ADAPTERS
```

v5 风格（`/repos/:owner/:repo/...`）直接继承 `V5Adapter`；自建 GitLab 只需在 `repos` 里补
`api_base: https://gitlab.example.com/api/v4`。

最小权限 Token：GitHub classic `repo`（或 fine-grained 的 Contents + Issues + PR 读写）· Gitee
`projects / pull_requests / issues` · AtomGit `read_repository / write_repository / issues /
pull_requests` · GitLab `api`（或 `read_api` + `write_repository`）。

> 实测现状：四平台**只读**链路全部跑通；写操作 GitHub / 极狐 GitLab / AtomGit 已验证；**Gitee**
> 打标签与建评论通，只有「建/改 Issue」404（原因未定位）。加新适配器时只读可先跑通，**写操作请
> 逐条标注验证状态**——请求体形状发错时平台常报「解析失败」而不是「字段不认识」，很容易被误读
> 成「不支持」。

## 2. 加通知渠道

`service/notify.py` 实现 `Channel.send`，注册进 `CHANNELS`，再在配置里启用：

```yaml
notify:
  enabled: true
  channels:
    - { type: console,  enabled: true }
    - { type: webhook,  enabled: true, url: "https://...", secret: "" }
    - { type: wecom,    enabled: false, url: "企业微信机器人Webhook" }
    - { type: dingtalk, enabled: false, url: "钉钉Webhook", secret: "加签密钥" }
    - { type: feishu,   enabled: false, url: "飞书Webhook" }
  events: [scan_done, eval_done, verify_done, fix_done, pr_created, needs_manual, budget_exceeded]
```

内置：`webhook / email / wecom / dingtalk / feishu / console`。

## 3. 换 AI 供应商

```bash
FISSUE_LLM_BASE_URL=https://api.openai.com/v1
FISSUE_LLM_MODEL=gpt-4o-mini
```

协议为 OpenAI 兼容 `/chat/completions`；不支持 `response_format` 的端点自动降级为文本解析。
想要**第二意见**，把修复后评审换成另一个模型：`auto_fix.review_model`（留空用全局）。

## 4. 调整自动修复策略

```yaml
fix_policy:                     # 谁能被自动修
  tier1: { max_difficulty: 40, min_importance: 70 }
  tier2: { max_difficulty: 40, min_importance: 0 }
  only_issues: true
  tier_strategy: importance     # importance | dual | custom | percentile
  custom_tier: null

auto_fix:                       # 怎么修、护栏、失败怎么办
  agent_max_rounds: 12
  stall_rounds: 4
  no_write_rounds: 6
  regression_retries: 1
  review_rounds: 1
  protected_paths: [".github/**", ".gitlab/**", "LICENSE", "**/*.lock"]
  max_changed_files: 20
  max_diff_lines: 800
  on_failure: report_manual
  pr_strategy: { mode: fork, label: ai-generated }
```

自定义分档（`tier_strategy: custom`，支持 `pkg.mod:func` 与 `pkg.mod.func`）：

```python
def tier_of(difficulty, importance, policy):
    if difficulty <= 20 and importance >= 70:
        return "tier1"
    if difficulty <= 40:
        return "tier2"
    return "none"
```

```yaml
fix_policy: { tier_strategy: custom, custom_tier: "myproj.policy:tier_of" }
```

返回值接受 `"tier1"/"tier2"/"none"`（大小写不敏感）或 `Priority`；加载/执行失败记 warning 并
回退默认策略（避免「配置写错 → 静默不修」）。

> ⚠️ 改护栏前先读[安全模型](Security.md)：`protected_paths`、规模上限、验证器锁定是**最后一道
> 防线**，削弱它们要在 PR 里说明威胁模型影响。

## 5. 加一套测试夹具（门槛最低、价值最高）

```
demo/<yourkit>/
├── README.md        # 考什么 + 期望结果对照表
├── fixtures/        # issue-NN-<slug>.md，头部含 title / labels / target
├── repo/            # 待推送仓库（独立 git 仓库，被外层 .gitignore 忽略）
│   ├── <pkg>/       # 源码（植入缺陷）
│   ├── tests/       # 基线测试（全绿，且刻意不覆盖缺陷）
│   └── pyproject.toml
└── scripts/setup_github.py
```

三个硬要求：基线测试**全绿且不覆盖**植入缺陷（逼 Fissue 真读代码写验证器）· 必须有对抗样本
（只修一半的 PR / 重复 Issue / 刷量 Issue / 误报或设计偏好 Issue）· 期望结果写进 `target`，
对照表写进 README。自检：`cd demo/<yourkit>/repo && python -m pytest -q`。

现有五套：`textkit`（单函数语义）· `ratekit`（数值边界）· `pipekit`（跨模块根因定位）·
`envkit`（状态副作用/环境依赖/输入校验）· `lockkit`（资源不变量/超时上限/一次性语义）。
步骤与坑见[贡献指南](Contributing.md)与 [demo/README.md](../../demo/README.md)。

## 6. 沉淀验证器模式

最有价值的贡献：把反复出现的缺陷变成可复用知识。例如 `lockkit` 的「资源不变量被破坏」——
正常路径完全看不出来，必须多还一次许可才能抓住。欢迎开 Issue 描述模式并附验证器写法。

---

相关：[架构设计](Architecture.md) · [贡献指南](Contributing.md) · [安全模型](Security.md) ·
[配置参考](Configuration.md) · [DESIGN.md §8](../DESIGN.md)