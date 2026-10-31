# AI 层

> LLM 客户端、预算、提示词、评估器与反刷子。细节见 [DESIGN.md §6](../DESIGN.md)。

## 1. 客户端与协议

- 只说 **OpenAI 兼容协议**（`/chat/completions`），换供应商只改 `llm.base_url` + `llm.model`。
- 不支持 `response_format` 的端点自动识别并降级为文本解析。
- 输出必须是合法 JSON；`ai/client.extract_json` 鲁棒解析：**直解 → 去 ```` ```json ```` 围栏 →
  括号平衡截取**。
- 主模型失败自动降级 `llm.fallback_model`（留空则报错）。

| 供应商 | `base_url` | `model` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:14b` |

## 2. 预算与并发

```yaml
llm:
  concurrency: 4
  timeout_seconds: 120
  max_retries: 4
  max_tokens: 16384
  max_tokens_ceiling: 32768
  budget: { daily_tokens_per_repo: 2000000, daily_usd_per_repo: 5.0, hard_stop: true }
  pricing: { input_per_1k: 0.00014, output_per_1k: 0.00028 }
```

- 并发由 `llm.concurrency` 信号量限制。
- **每次调用写入 `llm_usage`**（purpose / model / tokens / cost）——预算从这张表算。
- `BudgetGuard` 每次调用前查当日累计，超限时按 `hard_stop` 抛异常或仅告警。
- `pricing` 只用于成本估算；被截断时**自动加码重试**到 `max_tokens_ceiling`（推理模型的
  reasoning 会吃掉大量预算）。

`fissue status` 查看当日消耗。

## 3. 提示词设计（`ai/prompts.py`）

1. 只依据给定材料，**不得臆造**；信息不足时降低 `confidence`。
2. 每维评分必须给 `reason`，尽量在 `evidence` 里引用原文。
3. 输出必须是合法 JSON。
4. 内容注入统一经 `render_item()` 截断（正文 6k 字符、评论 15 条、diff 20k 字符）。

> 一条实测教训：**只改提示词**（不换模型），同一批待修条目的产出补丁率从 38% 提升到 100%
> ——判据从「读够了没」换成「够了就动手」，模型不再空转。
> 见[基准：提示词重写前后](Benchmark.md#提示词重写前后同一批待修条目)。

## 4. 评估器（固定口径）

`ai/evaluator.py`：真实性 / 重要性 / 可行性 / PR 质量（各 0~100 + 理由 + 证据引用），
外加难度 0~100（**越低越好修**）。`--deep` 时 PR 额外喂 diff 评代码质量。
这四个维度与区间**不通过配置切换**。

**反刷子恒开、不可关闭**——识别误报、重复提交、AI 灌水，是「别信模型自述」主张的一部分。

阈值 `evaluation.thresholds`：`importance_high: 70` · `difficulty_low: 40` ·
`authenticity_min: 40` · `alert_importance: 80`。

## 5. 关键词预筛

分类先用**词边界匹配**的关键词预筛，一边倒时直接定论、不调 LLM：中文用子串匹配；ASCII 用
`(?<![a-z0-9_])kw(?![a-z0-9_])` 避免 `request` 误命中 `requests.get`；两者都命中或都不命中 →
交 AI（`fallback_to_llm: true`）。

## 6. 重复检测

判定相似 Issue 为 `is_duplicate=true` 并给出 `duplicate_of`，能召回中文同义重复且不误标
「同源但非重复」。

> ⚠️ `pipeline/stages.py` 的**阈值 0.22 是实测标定**（中文用 bigram + 包含度），改它必须附
> 标定数据，见[贡献指南](Contributing.md)。

## 7. 提示注入

Issue/PR 正文与评论是**不可信输入**，可能试图操纵评测、验证器生成或修复 Agent。三层应对：
提示词要求只依据材料不臆造 · `render_item()` 截断限制注入面 · **模型输出一律不可信**，
由[宿主侧护栏](Architecture.md)强制执行。

---

相关：[架构设计](Architecture.md) · [验证器与 F2P](Verifier-and-F2P.md) · [安全模型](Security.md) ·
[基准与可信度](Benchmark.md) · [配置参考](Configuration.md)