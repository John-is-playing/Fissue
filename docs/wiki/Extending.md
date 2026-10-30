# 扩展指南

> Fissue 的扩展点都遵循同一套模式：**实现一个接口 → 注册进注册表 → 在配置里启用**。
> 本页给出各扩展点的落点与步骤；贡献流程见[贡献指南](Contributing.md)。

---

## 1. 加一个平台适配器

**适用**：管你公司的 Gitea / 自建 GitLab，或任何你关心的代码托管平台。

```
继承 PlatformAdapter → 实现 _headers / fetch_items / fetch_diff / item_url
                    → 注册进 platforms/registry.py:ADAPTERS
```

- 若目标平台是 **v5 风格**（`/repos/:owner/:repo/...`），直接继承 `V5Adapter` 即可。
- GitLab 自建实例只需在 `repos` 里补 `api_base: https://gitlab.example.com/api/v4`。

**最小权限 Token**：

| 平台 | 权限 |
|---|---|
| GitHub | classic token 勾 `repo`；或 fine-grained 勾 Contents+Issues+PR 读写 |
| Gitee | 「私人令牌」勾 projects / pull_requests / issues |
| AtomGit | 「访问令牌」勾 read_repository / write_repository / issues / pull_requests |
| GitLab | 「Access Token」勾 `api`（或 `read_api` + `write_repository`） |

> ⚠️ 已测事实：四平台**只读**链路全部跑通；**写操作** GitHub / 极狐 GitLab / AtomGit 已验证。
> **Gitee**：打标签与建评论是通的，只有「建 Issue / 改 Issue」两个动作 404（原因未定位，
> 见 [基准](Benchmark.md#5-尚未测量)）。加新适配器时，只读可先跑通、**写操作请逐条标注验证状态**，
> 并且注意请求体形状——发错形状时平台常报「解析失败」而不是「字段不认识」，很容易被误读成「不支持」。

---

## 2. 加一个通知渠道

在 `service/notify.py` 实现 `Channel.send`，注册进 `CHANNELS`，然后在 `config.yaml` 里启用：

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

内置类型：`webhook / email / wecom / dingtalk / feishu / console`。

---

## 3. 换 AI 供应商

改两个值即可（协议为 OpenAI 兼容的 `/chat/completions`）：

```bash
FISSUE_LLM_BASE_URL=https://api.openai.com/v1
FISSUE_LLM_MODEL=gpt-4o-mini
```

- 不支持 `response_format` 的端点会被**自动识别并降级为文本解析**。
- 想加**第二意见**，可把修复后的评审换成另一个模型：`auto_fix.review_model`（留空则用全局 `llm.model`）。

---

## 4. 调整自动修复策略

三层配置，从上到下依次收紧：

```yaml
fix_policy:                     # 谁能被自动修
  tier1: { max_difficulty: 40, min_importance: 70 }
  tier2: { max_difficulty: 40, min_importance: 0 }
  only_issues: true
  tier_strategy: importance     # importance | dual | custom
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
  pr_strategy:
    mode: fork                  # fork | direct | patch_only
    label: ai-generated
```

### 自定义分档函数（`tier_strategy: custom`）

支持 `pkg.mod:func` 与 `pkg.mod.func` 两种写法：

```python
def tier_of(difficulty, importance, policy):
    if difficulty <= 20 and importance >= 70:
        return "tier1"
    if difficulty <= 40:
        return "tier2"
    return "none"
```

```yaml
fix_policy:
  tier_strategy: custom
  custom_tier: "myproj.policy:tier_of"
```

返回值接受 `"tier1" / "tier2" / "none"`（大小写不敏感），也可返回 `fissue.models.Priority`。
**加载或执行失败会记 warning 并回退默认策略**，避免「配置写错 → 静默不修」。

> ⚠️ 修改护栏相关配置前请读[安全模型](Security.md)：`protected_paths`、规模上限、验证器锁定是**最后一道防线**，
> 削弱它们的改动需要在 PR 里说明威胁模型影响。

---

## 5. 加一套测试夹具（门槛最低、价值最高）

夹具是 Fissue **最重要的资产**。一套新夹具的目录形态：

```
demo/<yourkit>/
├── README.md        # 说明「这套夹具考什么」与期望结果对照表
├── fixtures/        # issue-NN-<slug>.md，头部注释含 title / labels / target
├── repo/            # 待推送的测试仓库（独立 git 仓库，被外层 .gitignore 忽略）
│   ├── <pkg>/       # 库源码（植入若干缺陷）
│   ├── tests/       # 基线测试（必须全绿，且刻意不覆盖那些缺陷）
│   └── pyproject.toml
└── scripts/setup_github.py
```

**三个硬要求**：

1. **基线测试必须全绿**，且**不覆盖**植入的缺陷——逼 Fissue 真去读代码写验证器，而不是抄现成测试。
2. **必须有对抗性样本**：至少一个「只修一半」的 PR、一个重复 Issue、一个刷量 Issue、一个误报/设计偏好 Issue。
3. **期望结果写进 `fixtures/*.md` 头部的 `target`**，对照表写进 README。

自检（不连网）：`cd demo/<yourkit>/repo && python -m pytest -q`（期望全绿）。

现有五套：`textkit`（单函数语义）· `ratekit`（数值边界）· `pipekit`（跨模块根因定位）· `envkit`（状态副作用/环境依赖/输入校验）· `lockkit`（资源不变量/超时上限/一次性语义）。

详细步骤与常见坑见[贡献指南](Contributing.md#2-三条推荐的贡献路径)里的「路径 ① 加一套测试夹具」。

---

## 6. 加一个验证器模式 / 缺陷模式

这是**最有价值**的贡献：把一类反复出现的缺陷沉淀成可复用的验证器知识。
比如 `lockkit` 里「资源不变量被破坏」这种——**正常路径完全看不出来**，必须多还一次许可才能抓住。

如果你在真实仓库里遇到某一类缺陷反复出现，欢迎开 Issue 描述模式，并附上你的验证器写法。

---

## 相关页

[架构设计](Architecture.md) · [贡献指南](Contributing.md) · [安全模型](Security.md) · [配置参考](Configuration.md) · [docs/DESIGN.md §8](../DESIGN.md#8-扩展指南)
