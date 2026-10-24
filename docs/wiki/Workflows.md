# 使用场景与配方

> 可直接照抄的操作序列。命令细节见 [CLI 参考](CLI-Reference.md)，配置键见[配置参考](Configuration.md)。

---

## 1. 首次试跑（10 分钟，不写任何东西到平台）

```bash
fissue fetch --repo psf/requests --limit 20
fissue eval --limit 20
fissue status
fissue report --repo psf/requests --out data/exports/first.md
```

只读平台数据、写本地库，**不会**在平台上打标签或提 PR。

---

## 2. 观察自动修复质量（推荐的上线姿势）

先只出补丁：

```yaml
# config.yaml
auto_fix:
  pr_strategy:
    mode: patch_only      # 只产出补丁，不建分支不提 PR
```

```bash
fissue fetch && fissue eval --all && fissue verify && fissue flush && fissue fix --plan
# 看 data/patches/*.patch 与 data/manual/*/report.md，人工评估 AI 修得怎么样
```

质量满意后再放开：

```yaml
auto_fix:
  pr_strategy:
    mode: fork            # fork → 分支 → 上游 PR
```

---

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
fissue fetch                       # 全部仓库增量抓取
fissue eval --all --concurrency 4  # 并发评测
fissue serve --with-web            # 交给常驻服务持续跑
```

---

## 4. 只关心高重要性问题

```yaml
evaluation:
  thresholds:
    alert_importance: 80      # 只有 ≥80 才推送
notify:
  enabled: true
  channels:
    - { type: wecom, enabled: true, url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." }
  events: [high_importance, needs_manual, pr_created]
```

跑 `fissue serve`，只有真正紧急的才会推到你手机上。

---

## 5. 交给 CI 定时跑（不适合常驻的场合）

```bash
fissue serve --once                    # 每小时跑一轮，跑完退出

# 或分步更可控
fissue fetch --concurrency 1
fissue eval --all --limit 50
fissue verify --limit 20
fissue flush --auto
fissue fix --limit 3
```

GitHub Actions 示例（详见仓库 [USAGE.md §9.5](../USAGE.md#95-交给-ci-定时跑不适合常驻的场合)）：

```yaml
name: fissue
on:
  schedule: [{ cron: "0 * * * *" }]
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e .
      - run: fissue serve --once
        env:
          FISSUE_DATABASE_URL: ${{ secrets.FISSUE_DATABASE_URL }}
          FISSUE_LLM_API_KEY: ${{ secrets.FISSUE_LLM_API_KEY }}
          FISSUE_GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## 6. 只跑某一条 Issue（快速验证效果）

```bash
KEY=github:psf/requests#6712

fissue eval   -r psf/requests --all     # eval 无 --key，用 --limit 1 亦可
fissue verify --key $KEY
fissue flush
fissue fix    --key $KEY --dry-run
```

> `eval` 没有 `--key`（批量语义）；`verify` 与 `fix` 支持 `--key`。

---

## 7. 条目卡住了：`fissue requeue`

两种条目**只能人工放行**：

```bash
fissue requeue --stale --dry-run      # 结论过期但修复已开工：先看计划，不改状态
fissue requeue --stale --yes          # 放回重评
fissue requeue --failed --yes         # 状态为 failed 的条目重试
```

---

## 8. 模型说「不修」，但你就是想修：`fissue force`

评测结论只是参考——「要不要修」由人拍板。

```bash
fissue force -k github:psf/requests#42 --dry-run   # 先看计划，不改状态
fissue force -k github:psf/requests#42 --yes       # 强制修复
fissue force -k github:psf/requests#42 --undo      # 取消强制
```

置位强制标记后走一遍验证阶段，条目直落「待修 tier1」，由常驻服务 / `fissue fix` 接力提 PR。分类 / 刷量 / 可复现 / 优先级这些闸门在强制模式下全部旁路，**验证器闸门不旁路**——没有验证器无从判定修复。

只在 Issue 上可用（PR 没有自动修复流程）；已在修复流程中的条目保持原状（防重复开 PR）。

---

## 9. 成本控制

### 各步骤的 token 消耗量级

| 步骤 | 单条消耗 | 说明 |
|---|---|---|
| `eval` | 中 | 一次调用出四维评分 |
| `eval --deep` | 中高 | 额外喂 PR diff 评代码质量 |
| `verify` | 中 | 生成验证器（不可靠时最多重试 `max_rounds` 轮） |
| `flush` | 低（分摊） | 一批一次，条数越多单条越便宜 |
| `fix` | 高 | Agent 循环，每轮都要带上下文 |

### 预算熔断

```yaml
llm:
  concurrency: 4
  budget:
    daily_tokens_per_repo: 2000000
    daily_usd_per_repo: 5.0
    hard_stop: true             # true=超限即停；false=仅告警
  pricing:
    input_per_1k: 0.00014
    output_per_1k: 0.00028
```

查当日消耗：`fissue status`。

### 省钱的六个开关

```yaml
# 1) 缩小抓取窗口
repos: [{ platform: github, owner: psf, name: requests, since_days: 7 }]

# 2) 关键词预筛（一边倒时不调 LLM）
classification: { use_keywords_first: true }

# 3) 不开 --deep（PR 不走 diff 深度评审）
# 4) 验证器重试次数调小
verifier: { max_rounds: 2 }

# 5) Agent 循环轮次调小
auto_fix: { agent_max_rounds: 8 }

# 6) 收紧自动修复准入
fix_policy: { tier2: { max_difficulty: 20, min_importance: 0 } }
```

### 增量抓取天然省钱

内容指纹没变的条目会被跳过，**不会重复评测**。所以常驻服务长期跑的实际消耗远低于首次全量。

---

## 10. 跑分基线：改动前后比一比

`BENCHMARK.md` 里的数字来自「开发期累计运行」，期间项目自己在改代码、模型输出又不确定，
于是那些数字**回答不了**最基本的问题——*这次改动让结果变好了还是变坏了？*
`bench` 把「测量」变成**可对比的快照**：

```bash
# 前提必须先干净：commit 已提交、工作区无未提交改动
git status --short
git rev-parse HEAD

# 冻结（只读数据库，不触发抓取/评测）
fissue bench capture --repo John-is-playing/ratekit --label ratekit-before
#   … 改代码 / 调配置 …
fissue bench capture --repo John-is-playing/ratekit --label ratekit-after
fissue bench diff data/benchmarks/ratekit-before.json data/benchmarks/ratekit-after.json
```

- 每份基线记录**前提**（git commit + 是否有未提交改动、模型与采样参数、关键配置指纹：回归门档位、`stall_rounds`、tier 阈值…）与**结果**（条目分布、各阶段 outcome 交叉表、F2P 成立数、修复结果）。
- `diff` **先判可比性再给数字**，三种情形显式点名：

```
可比：commit / 模型 / 工作区状态一致
不可比——以下前提不同，结论不能当回归依据：
   - commit 不同（68d5bd49 → c3f1a2b0）
   - 存在未提交改动（新基线 38 个文件）
   - 模型不同（qwen3.6-plus → deepseek-chat）
```

- 退出码：`0` 可比且无变化｜`1` **不可比**｜`2` 可比但有变化——便于塞进 CI 当回归门。
- **工作区脏的基线会被标记 `reproducible: false`**，这是刻意的：不允许拿「代码随时会变」的两次测量当回归依据。所以正式跑分前先提交。

---

## 相关页

[快速开始](Getting-Started.md) · [CLI 参考](CLI-Reference.md) · [故障排查](Troubleshooting.md) · [基准与可信度](Benchmark.md) · [部署与运维](Deployment.md)
