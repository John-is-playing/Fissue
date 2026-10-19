# AI 层

> LLM 相关的设计：客户端、预算与并发、提示词约定、关键词预筛、重复检测、成本。
> 完整细节见 [docs/DESIGN.md §6](../DESIGN.md#6-ai-层)。

---

## 1. 客户端与协议

- 只说 **OpenAI 兼容协议**（`/chat/completions`）。换供应商只改两个值：`llm.base_url` + `llm.model`。
- 不支持 `response_format` 的端点会被**自动识别并降级为文本解析**。
- 输出必须是合法 JSON；`ai/client.extract_json` 做鲁棒解析：**直解 → 去 ```` ```json ```` 围栏 → 括号平衡截取**。
- **主模型失败自动降级**到 `llm.fallback_model`（留空则不降级）。

### 常用供应商对照

| 供应商 | `base_url` | `model` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:14b` |

---

## 2. 预算与并发

```yaml
llm:
  concurrency: 4
  timeout_seconds: 120
  max_retries: 4
  max_tokens: 16384
  max_tokens_ceiling: 32768
  budget:
    daily_tokens_per_repo: 2000000
    daily_usd_per_repo: 5.0
    hard_stop: true
  pricing: { input_per_1k: 0.00014, output_per_1k: 0.00028 }
```

- 并发由 `llm.concurrency` 的**信号量**限制。
- **每次调用写入 `llm_usage` 表**（purpose / model / tokens / cost）——预算是从这张表算出来的。
- `BudgetGuard` 在**每次调用前**查当日累计用量，超 `daily_tokens_per_repo` 或 `daily_usd_per_repo` 时按 `hard_stop`
  抛异常或仅告警。
- `pricing` 仅用于**成本估算**，按你用的模型单价填。
- 被截断（推理模型会吃掉大量预算）时**自动加码重试**到 `max_tokens_ceiling`。

查看当日实际消耗：`fissue status`。

---

## 3. 提示词设计

集中在 `ai/prompts.py`，共同约定：

1. 只依据给定材料，**不得臆造**；信息不足时降低 `confidence`。
2. 每维评分必须给 `reason`，尽量在 `evidence` 里引用原文。
3. 输出必须是合法 JSON（鲁棒解析见上）。
4. 内容注入统一经 `render_item()` 截断（正文 6k 字符、评论 15 条、diff 20k 字符），防止超长 Issue 烧掉预算。

### 一条实测教训：把纪律写进提示词

修复链路在**只改提示词**（不换模型）之后，同一批待修条目的**产出补丁率从 38% 提升到 100%**：
判据从「读够了没」换成「**够了就动手**」，模型不再空转。
这条对照测量与自我更正的完整记录在[基准与可信度](Benchmark.md#提示词重写前后的对照同一批待修条目)。

---

## 4. 评估器：四维评分与反刷子

`ai/evaluator.py` 是**固定口径**（不由配置切换）：

| 维度 | 含义 |
|---|---|
| 真实性 authenticity | 是真问题还是误报 / 刷量 |
| 重要性 importance | 影响面与严重度 |
| 可行性 feasibility | 是否可自动化处理 |
| PR 质量 quality | 仅 PR 适用（`--deep` 时额外喂 diff） |
| 难度 difficulty | 0~100，**越低越好修** |

**反刷子恒开、不可关闭**——识别误报、重复提交、AI 灌水。它是本项目「别信模型自述」的核心主张，
不是一个可以关掉的选项。

阈值（`evaluation.thresholds`）：`importance_high: 70` · `difficulty_low: 40` · `authenticity_min: 40` · `alert_importance: 80`。

---

## 5. 关键词预筛与分类

分类先用**词边界匹配的关键词预筛**，一边倒时直接定论、不调 LLM（省钱）：

- 中文关键词用**子串匹配**。
- ASCII 关键词用 `(?<![a-z0-9_])kw(?![a-z0-9_])`，避免 `request` 误命中 `requests.get`。
- 两者都命中或都不命中 → 交 AI 决定（`fallback_to_llm: true`）。

```yaml
classification:
  use_keywords_first: true
  bug_keywords: [bug, error, crash, fail, exception, regression, 报错, 崩溃, 异常, 失败, 修复, 无法]
  feature_keywords: [feature, enhancement, proposal, support, request, 建议, 新增, 支持, 需求, 优化]
  fallback_to_llm: true
```

---

## 6. 重复检测

判定相似 Issue 为 `is_duplicate=true` 并给出 `duplicate_of`（指向正确编号）。能召回**中文同义重复**，
且不误标「同源但非重复」。

> ⚠️ `pipeline/stages.py` 里的**阈值 0.22 是实测标定的**——改它必须附上标定数据（见[贡献指南](Contributing.md#4-提-pr摘要)）。

---

## 7. 内容注入与提示注入

Issue/PR 正文与评论是**不可信输入**，可能包含**提示注入**，试图操纵评测、验证器生成或修复 Agent。

- 提示词层面：要求只依据给定材料、不得臆造。
- 结构层面：内容统一经 `render_item()` **截断**，避免超长内容吃掉预算。
- 宿主层面：**模型输出一律不可信**——验证器、补丁、路径都可能是恶意或错误的，由[宿主侧护栏](Architecture.md#5-安全模型摘要)强制执行。

---

## 相关页

[架构设计](Architecture.md) · [验证器与 F2P](Verifier-and-F2P.md) · [安全模型](Security.md) · [基准与可信度](Benchmark.md) · [配置参考](Configuration.md#llm)
