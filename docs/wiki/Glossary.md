# 术语表

> ★ = 本项目特有、别处查不到准确含义的词。

## 条目与标识

| 术语 | 含义 |
|---|---|
| **条目（item）** ★ | 一条被纳入处理的 Issue 或 PR，落库在 `items` 表 |
| **key** ★ | 全局唯一标识 `<平台>:<owner>/<repo>#<编号>`；URL 里需编码（`#` → `%23`） |
| **content_hash** ★ | 内容指纹：标题/正文/标签/**评论条数**/状态/merged，用于判「平台侧与库里不一样」 |
| **MATERIAL_FIELDS** ★ | 会让**已有结论作废**的实质字段：标题/正文/标签/状态/merged（不含评论条数） |
| **stale_reasons** ★ | 「结论已过期」留痕：修复已开工的条目内容变了不自动重评，等 `requeue --stale` 人工放行 |
| **category** | BUG 或 FEATURE，决定走哪条流水线 |
| **status** | `new → evaluated → queued → labeled → fix_queued → fixing → pr_created`，另有 `needs_manual / skipped / failed` |
| **priority / tier** ★ | `tier1`（难度低+重要性高）· `tier2`（难度低+重要性低）· `none`（不自动修） |

## 评分

| 术语 | 含义 |
|---|---|
| **四个维度** | 真实性 / 重要性 / 可行性 / PR 质量，各 0~100，**口径固定**、硬编码在 `ai/evaluator.py` |
| **difficulty** | 修复难度 0~100，**越低越好修** |
| **反刷子** ★ | 识别误报、重复提交、AI 灌水。**恒开不可关**，是核心主张的一部分 |
| **重复检测** | 判 `is_duplicate` 并给 `duplicate_of`；阈值 0.22 属实测标定，改它须附标定数据 |

## 验证

| 术语 | 含义 |
|---|---|
| **验证器（verifier）** ★ | AI 生成的、能在沙盒里跑的复现程序（或自然语言清单） |
| **F2P（fail-to-pass）** ★ | base 未修复**必须失败** + fix 修复后**必须通过**，同时成立才是 `f2p_satisfied` |
| **base / fix 阶段** | 两个代码状态上的沙盒执行 |
| **回归门** ★ | 补 pass-to-pass 方向：base 与 fix 各跑一次仓库自己的测试套件，base 绿而 fix 红即「弄坏了别处」。`off / warn / strict`（默认 strict） |
| **checklist 验证器** | 无测试框架时的降级形态，LLM 按证据逐条判定；force 条目修完后在修复工作区重判 |
| **needs_manual** | 无法可靠自动判定而转人工。**绝不据此自动修码** |
| **假修复** ★ | 看着修好了，但 F2P 不成立或被回归门拦下 |

## 队列与批量

| 术语 | 含义 |
|---|---|
| **队列** | `verify`（Issue-BUG 验证）· `fix_bug` · `fix_feature`（PR 合并验证，共用沙盒） |
| **flush** ★ | 把成批条目一次交 AI 定论（打标签、派发修复）。四触发：`flush_size` / `idle_flush_seconds` / 队列排空 / 手动 |
| **为什么批量** ★ | 同批验证输出在同一上下文，模型能横向比较（识别成批重复），也更省钱 |
| **原子抢占** ★ | 批次用条件 UPDATE 抢占，同一批不会被两个写者各跑一次 |
| **写锁 / WRITE_GATE** ★ | 同一时刻只允许一处写外部仓库；跨进程 `flock`，同进程 `WRITE_GATE`，Web 拿不到锁回 409 |

## 修复与护栏

| 术语 | 含义 |
|---|---|
| **Agent 循环** ★ | 「读文件 → 模型决定下一步 → 写文件/跑验证器 → 看结果」最多 N 轮，不是一次性吐 patch |
| **stall_rounds / no_write_rounds** ★ | 两道空转闸门：连续 N 轮验证器输出完全一致 / 一个文件都没改即提前收工 |
| **protected_paths** | 禁止写入的路径：`.github/**`、`.gitlab/**`、`LICENSE`、`**/*.lock` |
| **syntax_check** | 落盘前 `compile()` 一次，不合法就拒绝并把「行号 + 出错处」回给模型 |
| **评审（review）** ★ | 修复后的第二道检查：只看 diff 与改动后文件，把结构/语义问题交回返修。F2P 与回归门挡不住「写得脏」 |
| **人工闸门** ★ | PR 永不自动合并；`approve-merge` 只是标签 + 评论；失败转 `data/manual/<key>/report.md` |
| **转发组件（forwarder）** | 宿主侧常驻进程，沙盒执行的受控通道；未起时走 `inline`（默认） |

## 平台、沙盒与发布

| 术语 | 含义 |
|---|---|
| **适配器（adapter）** | 继承 `PlatformAdapter`；v5 风格可直接继承 `V5Adapter`；注册表 `platforms/registry.py:ADAPTERS` |
| **夹具（fixture）** ★ | 对抗性样本仓库：植入缺陷 + 全绿但不覆盖缺陷的基线测试 + 假修复/重复/刷量/误报干扰项。五套：`textkit / ratekit / pipekit / envkit / lockkit` |
| **基线（baseline）** ★ | `fissue bench capture` 冻结的「前提 + 结果」快照，落 `data/benchmarks/*.json`，是唯一进版本库的 `data/` 子目录 |
| **可比性判定** ★ | `bench diff` 先比 commit / 模型 / 工作区状态，**不可比就标出来**，不给会被误当回归依据的增量 |
| **降级（local runtime）** ★ | 本机子进程执行，**无任何隔离**，仅限本机开发 |
| **快照版本** ★ | 正式版之间的 `vX.Y.Z-bN`，配 `rel/` 分支与附注 tag；**不改** `pyproject.toml` / `__version__` |

---

相关：[项目概览](Overview.md) · [架构设计](Architecture.md) · [验证器与 F2P](Verifier-and-F2P.md) ·
[配置参考](Configuration.md)