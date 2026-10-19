# 术语表

> 读 Wiki 或源码时遇到的专有名词，在这里一次说清。带 ★ 的是**本项目特有**、
> 在别处查不到准确含义的词。

## 条目与标识

| 术语 | 含义 |
|---|---|
| **条目（item）** ★ | 一条被纳入处理的 Issue 或 PR，落库在 `items` 表。 |
| **key** ★ | 条目的全局唯一标识，格式 `<平台>:<owner>/<repo>#<编号>`，例如 `github:psf/requests#6712`。在 URL 里需编码为 `github%3Apsf%2Frequests%236712`（`#` → `%23`）。 |
| **content_hash** ★ | 条目的内容指纹：标题 / 正文 / 标签 / **评论条数** / 状态 / merged 的摘要。变了说明「平台侧这条与库里不一样」。 |
| **MATERIAL_FIELDS** ★ | 会让**已有结论作废**的实质字段：标题 / 正文 / 标签 / 状态 / merged。`content_hash` 里含评论条数，但不因此作废结论。 |
| **stale_reasons** ★ | 「结论已过期」的留痕。修复已开工的条目内容变了，不自动重评，只记录原因，等 `fissue requeue --stale` 人工放行。 |
| **category** | BUG 或 FEATURE。由关键词预筛 + AI 兜底判定，决定走哪条流水线。 |
| **status** | 条目的流水线状态机：`new → evaluated → queued → labeled → fix_queued → fixing → pr_created`，另有 `needs_manual / skipped / failed`。见[架构设计](Architecture.md#状态机status)。 |
| **priority / tier** ★ | 由 `fix_policy` 决定的优先级：`tier1`（难度低 + 重要性高）、`tier2`（难度低 + 重要性低）、`none`（不自动修）。 |

## 评分维度

| 术语 | 含义 |
|---|---|
| **四个维度** | 真实性（authenticity）/ 重要性（importance）/ 可行性（feasibility）/ PR 质量（quality），各 0~100，**口径固定**、硬编码在 `ai/evaluator.py`，不可配置。 |
| **difficulty** | 修复难度，0~100，**越低越好修**。 |
| **反刷子** ★ | 识别误报、重复提交、AI 灌水的机制。**恒开、不可关闭**——它是本项目核心主张的一部分，不是一个可关掉的选项。 |
| **重复检测** | 判定相似 Issue 为 `is_duplicate=true` 并给出 `duplicate_of`。阈值 0.22 是实测标定的（改它必须附标定数据）。 |

## 验证

| 术语 | 含义 |
|---|---|
| **验证器（verifier）** ★ | 由 AI 生成的、能在沙盒里跑的复现程序（或自然语言清单）。 |
| **F2P（fail-to-pass）** ★ | 两段式判据：**base** 阶段在未修复代码上必须**失败**（证明问题真实存在）；**fix** 阶段在修复/合并后必须**通过**（证明修复有效）。两者同时成立才算 `f2p_satisfied`。 |
| **base / fix 阶段** | 两个代码状态上的沙盒执行。`base` = 未修复 / 未合并；`fix` = 修复后 / 已合并 PR。 |
| **回归门（regression gate）** ★ | 补上 pass-to-pass 方向的闸门：在 base 与 fix 上各跑一次**仓库自己的测试套件**，base 绿而 fix 红即判定修复「弄坏了别处」。三档：`off / warn / strict`（默认 `strict`）。 |
| **checklist 型验证器** | 仓库无测试框架时的降级形态：由 LLM 逐条判读执行输出的自然语言清单。 |
| **needs_manual** | 无法可靠自动化判定（base 就通过、base 报错/超时、无测试框架等）而转人工的状态。**绝不会据此自动修码**。 |
| **假修复** ★ | 目标用例看着修好了，但实际没解决（F2P 不成立）或弄坏了别处（被回归门拦下）。 |
| **回归重试（regression_retries）** | 回归门失败后，把「哪几条用例被改坏」喂回 Agent 重跑修复的次数。 |

## 队列与批量

| 术语 | 含义 |
|---|---|
| **队列（queue）** | `verify`（Issue-BUG 验证）、`fix_bug`、`fix_feature`（PR 合并验证，Bug/Feature 各一套，共用沙盒）。 |
| **flush** ★ | 把队列里已成批积累的条目一次性交 AI 定论（打标签、派发修复）。触发条件有四：数量阈值 `flush_size`、空闲超时 `idle_flush_seconds`、队列排空、手动 `fissue flush`。 |
| **为什么批量** ★ | 同一批条目的验证输出放在同一上下文里，模型能**横向比较**（例如识别成批的重复提交），也比逐条调用省钱。 |
| **原子抢占** ★ | flush 的定义批次用条件 UPDATE 抢占，保证同一批不会被两个写者（daemon / Web / CLI）各跑一次。 |
| **写锁 / WRITE_GATE** ★ | 同一时刻只允许一处写外部仓库：跨进程用 `flock`，同进程（`serve --with-web`）另有 `WRITE_GATE`；Web 写端点拿不到回 `409`。 |

## 修复与护栏

| 术语 | 含义 |
|---|---|
| **Agent 循环** ★ | 自动修复不是一次性吐 patch，而是「读文件 → 模型决定下一步 → 写文件 / 跑验证器 → 看结果」最多 N 轮的受控循环。 |
| **stall_rounds / no_write_rounds** ★ | 两道空转闸门：连续 N 轮「验证器输出完全一致」/「一个文件都没改」即提前收工，避免白烧轮次。 |
| **protected_paths** | 修复禁止写入的路径，默认 `.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock`。 |
| **syntax_check** | 落盘前对 Python 文件 `compile()` 一次，不合法就拒绝该文件并把「行号 + 出错处」回给模型。 |
| **评审（review）** ★ | 修复完成后的第二道检查：只看改动本身（diff + 改动后的文件），把结构/语义问题交回 Agent 返修。F2P 与回归门**挡不住「写得脏」**，这是补它的。 |
| **人工闸门** ★ | PR **永不自动合并**；`approve-merge` 也只是打标签 + 评论。修复失败转 `data/manual/<key>/report.md`。 |
| **转发组件（forwarder）** | 宿主侧常驻进程，沙盒执行的受控通道；起它之后调用方**不持有 Docker 句柄**。未起时走 `inline`（默认）。 |

## 平台与沙盒

| 术语 | 含义 |
|---|---|
| **适配器（adapter）** | 平台接入实现，继承 `PlatformAdapter`；v5 风格平台可直接继承 `V5Adapter`。注册表在 `platforms/registry.py:ADAPTERS`。 |
| **夹具（fixture）** ★ | 刻意构造的**对抗性样本仓库**：植入真实缺陷 + 一套全绿但不覆盖缺陷的基线测试 + 假修复/重复/刷量/误报等干扰项。是项目最重要的资产。当前五套：`textkit / ratekit / pipekit / envkit / lockkit`。 |
| **基线（benchmark baseline）** ★ | 用 `fissue bench capture` 冻结的一份「前提 + 结果」快照，落在 `data/benchmarks/*.json`，是**唯一进版本库的 `data/` 子目录**。 |
| **可比性判定** ★ | `fissue bench diff` 先比 commit / 模型 / 工作区状态 / 仓库集等前提，**不可比就标出来、不给会被误当回归依据的增量**。 |
| **降级（local runtime）** ★ | `sandbox.runtime: local` 或显式打开 `sandbox.allow_local_fallback` 后走本机子进程执行——**没有任何隔离**，仅限本机开发，切勿用于不可信代码。 |

## 发布

| 术语 | 含义 |
|---|---|
| **快照版本** ★ | 正式版之间发布在 `main` 上的一批预发布产物，形如 `vX.Y.Z-bN`（如 `v1.1.0-b3`），配 `rel/vX.Y.Z-bN` 分支与附注 tag；**快照提交不改 `pyproject.toml` / `__version__`**。 |
| **主模型 / fallback_model** | LLM 调用失败自动降级到 `fallback_model`（留空则不降级）。 |

---

相关页：[项目概览](Overview.md) · [架构设计](Architecture.md) · [验证器与 F2P](Verifier-and-F2P.md) · [配置参考](Configuration.md)
