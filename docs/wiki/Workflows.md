# 使用场景与配方

> 可直接照抄的操作序列。命令细节见 [CLI 参考](CLI-Reference.md)，配置键见[配置参考](Configuration.md)。

## 1. 首次试跑（10 分钟，不写平台）

```bash
fissue fetch --repo psf/requests --limit 20
fissue eval --limit 20
fissue status
fissue report --repo psf/requests --out data/exports/first.md
```

只读平台数据、写本地库，**不会**打标签或提 PR。

## 2. 观察自动修复质量（推荐上线姿势）

```yaml
auto_fix: { pr_strategy: { mode: patch_only } }   # 只产补丁，不建分支不提 PR
```

```bash
fissue fetch && fissue eval --all && fissue verify && fissue flush && fissue fix --plan
# 看 data/patches/*.patch 与 data/manual/*/report.md
```

满意后改 `mode: fork`（fork → 分支 → 上游 PR）。

## 3. 多仓库批量托管

```yaml
repos:
  - { platform: github,  owner: psf,         name: requests,    since_days: 14 }
  - { platform: github,  owner: pallets,     name: flask,       since_days: 14 }
  - { platform: gitee,   owner: mindspore,   name: mindspore,   since_days: 30 }
  - { platform: atomgit, owner: openharmony, name: arkcompiler, since_days: 30 }
  - { platform: gitlab,  owner: gitlab-org,  name: gitlab,      since_days: 7, api_base: https://gitlab.com/api/v4 }
```

```bash
fissue fetch && fissue eval --all --concurrency 4 && fissue serve --with-web
```

## 4. 只关心高重要性

```yaml
evaluation: { thresholds: { alert_importance: 80 } }
notify:
  enabled: true
  channels: [{ type: wecom, enabled: true, url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." }]
  events: [high_importance, needs_manual, pr_created]
```

## 5. 交给 CI 定时跑

```bash
fissue serve --once
# 或分步更可控
fissue fetch --concurrency 1 && fissue eval --all --limit 50 && fissue verify --limit 20 \
  && fissue flush --auto && fissue fix --limit 3
```

GitHub Actions 示例见 [USAGE.md §9.5](../USAGE.md)。

## 6. 只跑某一条

```bash
KEY=github:psf/requests#6712
fissue verify --key $KEY
fissue flush
fissue fix --key $KEY --dry-run
```

> `eval` 没有 `--key`（批量语义），单条用 `--limit 1`。

## 7. 条目卡住了：`requeue`

```bash
fissue requeue --stale --dry-run      # 结论过期但修复已开工：先看计划
fissue requeue --stale --yes
fissue requeue --failed --yes         # 失败条目重试
```

## 8. 模型说不修，但你要修：`force`

```bash
fissue force -k github:psf/requests#42 --dry-run
fissue force -k github:psf/requests#42 --yes
fissue force -k github:psf/requests#42 --undo
```

置位后走一遍验证阶段，条目直落待修 tier1，由常驻服务 / `fissue fix` 接力提 PR。分类 / 刷量 /
可复现 / 优先级闸门全部旁路，**验证器闸门不旁路**（没有验证器无从判定修复）。仅 Issue 可用；
已在修复流程中的条目保持原状（防重复开 PR）。

## 9. 成本控制

| 步骤 | 单条消耗 | 说明 |
|---|---|---|
| `eval` | 中 | 一次调用出四维评分 |
| `eval --deep` | 中高 | 额外喂 PR diff |
| `verify` | 中 | 生成验证器（不可靠时重试 `max_rounds` 轮） |
| `flush` | 低（分摊） | 一批一次，条数越多越便宜 |
| `fix` | 高 | Agent 循环，每轮带上下文 |

```yaml
llm:
  concurrency: 4
  budget: { daily_tokens_per_repo: 2000000, daily_usd_per_repo: 5.0, hard_stop: true }
  pricing: { input_per_1k: 0.00014, output_per_1k: 0.00028 }
```

六个省钱开关：缩小 `since_days` · `use_keywords_first: true` · 不开 `--deep` ·
`verifier.max_rounds: 2` · `auto_fix.agent_max_rounds: 8` · 收紧 `fix_policy.tier2`。
增量抓取天然省钱：指纹没变就不重复评测。

## 10. 跑分基线：改动前后比一比

`BENCHMARK.md` 的数字来自开发期累计运行，期间项目自己在改代码、模型输出又不确定，所以回答不了
「这次改动让结果变好了还是变坏了」。`bench` 把测量变成可对比的快照：

```bash
git status --short && git rev-parse HEAD        # 前提必须先干净
fissue bench capture --repo John-is-playing/ratekit --label ratekit-before
#   … 改代码 / 调配置 …
fissue bench capture --repo John-is-playing/ratekit --label ratekit-after
fissue bench diff data/benchmarks/ratekit-before.json data/benchmarks/ratekit-after.json
```

- 每份基线记录**前提**（commit、是否有未提交改动、模型与采样参数、配置指纹）与**结果**（条目
  分布、各阶段 outcome 交叉表、F2P 成立数、修复结果）。
- `diff` **先判可比性再给数字**：commit / 模型 / 工作区状态不一致就明确点名「不可比——结论不能
  当回归依据」。
- 退出码 `0` 可比无变化 / `1` 不可比 / `2` 可比有变化，可塞进 CI 当回归门。
- 工作区脏的基线标 `reproducible: false`（不允许拿「代码随时会变」的两次测量当依据）。

---

相关：[快速开始](Getting-Started.md) · [CLI 参考](CLI-Reference.md) · [故障排查](Troubleshooting.md) ·
[基准与可信度](Benchmark.md) · [部署与运维](Deployment.md)