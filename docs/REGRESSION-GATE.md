# 既有测试回归门（Regression Gate）

> **状态：待实现（本文是制作规范，交给实施者执行）。**
> 阅读顺序：先读 [DESIGN.md §4](DESIGN.md#4-验证器与-f2pQ7) 了解 F2P 与验证器体系，再看本文。

---

## 1. 背景与动机

F2P（fail-to-pass）只表达**一个**方向：目标用例在修复前失败、修复后通过。

它**表达不了**「别弄坏别的」——即 pass-to-pass（回归）要求。后果是实测过的（demo 夹具 issue #1）：

| | `word_count("")` | `word_count("hello  world")` |
|---|---|---|
| 修复前（`split(" ")`） | 1 ❌ 目标问题 | **3**（既有语义） |
| 修复后（`split()`） | 0 ✅ 目标达成 | **2** ⚠️ 语义被改 |

issue #1 正文写明「**其余用例不受影响**」，但验证器只把列出的目标用例翻成断言，
而两种修法在目标用例上**表现完全相同**（都返回 0），差异全在未列举的连续空格上。
于是「改对了目标、却弄坏了别处」的修复照样通过 F2P。

**注意这是与验证器提示词互补、而非替代的机制**：提示词（`verifier_prompt` 第 9 条）
已要求验证器把正文的兼容性声明转成回归断言；但那只覆盖**issue 作者显式写出来**的约束。
对于真实仓库里海量的历史行为，唯一可靠的判据是**仓库自己的测试套件**。

## 2. 目标与非目标

**目标**

* 在验证流程中加入一道「既有测试必须仍然全绿」的闸门，成本随**运行时间**而非 **Issue 数**增长
  （几千条 Issue 的仓库不得因此产生任何额外 LLM token）。
* 能拦住「改对了目标、却弄坏了别处」的修复与 PR。
* base / fix 两个代码状态都要跑，且判定必须**不误伤**本来就不绿的仓库。

**非目标**

* ❌ 不做跨 Issue 的语义裁决（例如「修 #1 会不会违背 #3 的结论」）。
  那需要按 Issue 数检索，成本不可控，**明确排除**。
* ❌ 不替代 F2P。目标用例仍由验证器负责。
* ❌ 不追求覆盖「仓库没测到的既有行为」——那是验证器提示词那条的职责。

## 3. 核心设计

### 3.1 一次判定要看三样东西

```
确定性验证器（现有）：base 失败 + fix 通过        → f2p_satisfied
回归门（新增）      ：base 既有测试绿 + fix 仍绿   → regression_ok
```

两者**都**满足才允许自动修复/建议合并。

### 3.2 与现有阶段的关系（关键：不得引入第三种代码状态）

沿用现有语义，只是在同一工作区上**多跑一条命令**：

| 流程 | 现有 | 新增 |
|---|---|---|
| Issue-BUG（`VerifyStage`） | 克隆 → base 跑验证器（期望失败） | 同一工作区跑既有测试（期望**通过**） |
| PR（`PRVerifyStage`） | 克隆 → base 跑验证器 → 合并 PR → fix 跑验证器 | base 处跑一次既有测试（期望通过）+ 合并后再跑一次（期望通过） |

> 不要为了回归门另建工作区。复用 `run_once` 已有的 workspace 挂载方式即可。

### 3.3 判定矩阵（**这是最容易做错的地方**）

既有测试的结果与 F2P 的组合，必须按下表处理：

| base 既有测试 | fix 既有测试 | F2P | 结论 |
|---|---|---|---|
| 通过 | 通过 | 成立 | ✅ 二者都成立，可继续 |
| 通过 | **失败** | 成立 | ❌ **拒绝**：修复破坏了既有行为（本门要拦的正是它） |
| **失败** | — | — | ⚠️ **仓库本身不绿 → 本门不可信**：记 `warn` 并放行，**绝不据此拒绝** |
| ERROR/TIMEOUT | — | — | ⚠️ 同「不绿」：环境/依赖问题，不可归咎于修复 |
| 通过 | 通过 | 不成立 | 回归门不是 F2P 的替代，仍按 F2P 结论处理 |

> **第 3 行是安全底线**：老仓库普遍存在本来就失败/跳过的测试、或沙盒内依赖没装全。
> 若把「base 不绿」当成修复的错，会把**所有**条目误杀，闸门反而变成故障源。

## 4. 制作要求

### 4.1 配置项（新增）

`VerifierConfig`（`src/fissue/config.py`）新增：

```python
regression_gate: str = "warn"        # off | warn | strict
regression_command: str | None = None  # 留空则取仓库级 test_hint
regression_timeout_seconds: int = 900  # 单独预算，通常远大于单测验证器
```

* **默认必须是 `warn`**：上线首日谁也不知道仓库绿不绿，`strict` 会在老仓库上大面积误杀。
* `warn`：跑、记录、在报告里标注，但**不改变** F2P 结论。
* `strict`：按 §3.3 判定，base 绿而 fix 不绿时**拒绝**。
* `off`：完全不跑（零开销）。

同步更新 `config.example.yaml` 与 `docs/USAGE.md` 的配置说明。

### 4.2 命令来源

优先级：`regression_command` → 仓库配置的 `repo.test_hint` → 探测（有 `pyproject.toml`/`pytest.ini` 用 pytest，
`package.json` 用 npm test…）→ 探不到就跳过本门并记 `skipped`。

**不要**让 LLM 现编命令——本门的价值在于「跑仓库自己的既有测试」，编出来的就不是既有测试了。

### 4.3 必须排除验证器自己写入的文件

验证器会把自己的测试文件写进工作区（`spec.files`，如 `tests/test_reproduce.py`）。
跑全量套件时它们会被一起收集，导致：

* base 阶段因为验证器文件而失败 → 与「既有测试」语义混淆。

处理：跑之前把 `spec.files` 的路径**临时移出**（或 `git stash`/`git clean` 只留仓库原有跟踪文件），
跑完恢复。实现上建议**基于 git 解析**：只跑 `git ls-files` 里的测试文件，
天然排除验证器新加、未被跟踪的文件。

> 若仓库的测试发现机制会扫到未跟踪文件（pytest 默认会），上面的「临时移出」是必需的，
> 不能只靠 git 列表。

### 4.4 执行与记录

* 复用 `VerifierRunner` 的沙盒通道（`SandboxManager` → `ExecRequest`），
  **不得**直接 `subprocess` 跑在宿主上（安全模型见 DESIGN §5）。
* 用新的 `stage` 标识落库，便于与 base/fix 区分，例如 `stage="regression"`；
  沿用 `save_verifier_run`，`VerifierRun.outcome` 复用现有枚举。
* 超时用 `regression_timeout_seconds`，**不要**沿用 `spec.timeout_seconds`
  （后者是单测验证器的预算，通常远小于全量套件）。
* 产物沿用 `_save_artifacts`（stdout/stderr/meta.json）以便人工复核。

### 4.5 结论呈现

* `warn` 模式下：结论文案追加一行，如「⚠️ 既有测试未通过（未阻断）」。
* `strict` 模式下拒绝时，结论必须说清**哪一条测试挂了**（从输出里提取失败用例名），
  否则人工无从复核。
* 失败时走既有的人工处置路径（`needs_manual` + `data/manual/<key>/report.md`），
  在报告里给出回归门的完整输出。

### 4.6 成本约束

* 额外成本 = **N 次全量套件运行时间**（每 Issue 2 次：base + fix），与 Issue 数无关、与 token 无关。
* 若仓库套件极慢，`regression_timeout_seconds` 到点即放弃并记 `TIMEOUT`（按 §3.3 视为不可信，
  不阻断），**不得**无限等待。
* 考虑加一条「同一仓库短时间内复用结果」的缓存（可选，非必须）。

---

## 5. 实施环节（按序执行）

### 环节 1：配置与校验

1. `src/fissue/config.py`：`VerifierConfig` 加 §4.1 的三个字段，并做取值校验
   （`regression_gate` 只接受 `off|warn|strict`，非法值报 `ConfigError`）。
2. `config.example.yaml` 补注释说明（默认 `warn`，并写明为何默认不阻断）。
3. `tests/test_config_models.py` 补用例：默认值、非法值报错、`strict` 可开启。

### 环节 2：命令解析

1. 新增 `resolve_regression_command(settings, repo_cfg, workspace) -> str | None`：
   按 §4.2 优先级返回命令，探不到返回 `None`。
2. 单测覆盖：显式配置覆盖 `test_hint`；`test_hint` 覆盖探测；探测识别
   `pyproject.toml`/`package.json`/`go.mod`；都没有时返回 `None`。

### 环节 3：执行器（核心）

1. 新增 `RegressionGate` 类，放在 `src/fissue/verifier/`（与 runner 同层）。
2. 方法签名建议：

   ```python
   async def run(
       self,
       *,
       item_key: str,
       workspace: RepoWorkspace,
       spec: VerifierSpec,          # 用于排除验证器写入的文件
       repo_cfg: RepoConfig,
       stage: str,                  # "base" | "fix"（与调用点一致）
   ) -> VerifierRun:
   ```

3. 实现要点：
   * 命令为空 → 返回 `VerifierOutcome.SKIPPED`，`error="未找到既有测试命令"`。
   * 跑之前按 §4.3 把 `spec.files` 的路径临时移出工作区，跑完**无论成败都恢复**
     （用 `try/finally`）。
   * 通过 `SandboxManager` 下发 `ExecRequest`，超时用 `regression_timeout_seconds`。
   * 结果用 `save_verifier_run` 落库，`stage` 用独立标识（建议 `regression:base` / `regression:fix`）。
4. 单测（用 local 沙盒，参照 `tests/test_sandbox_verifier.py` 的 `_sandbox_outcome` 写法）：
   * 全绿 → `PASS`；有失败用例 → `FAIL`；超时 → `TIMEOUT`；
   * 探不到命令 → `SKIPPED`；
   * **验证器写入的测试文件确实被排除**（造一个「验证器文件会失败」的场景，
     断言回归门仍然 `PASS`）。

### 环节 4：接入两条流水线

1. `src/fissue/pipeline/stages.py`：
   * `VerifyStage`：`base` 跑完验证器后，若 `regression_gate != "off"` 就跑 base 回归门。
   * `PRVerifyStage`：**base 处**（合并前）跑一次；`_merge_pr` 之后跑一次 fix 回归门。
     ⚠️ 注意保持 §A 已确立的顺序契约：base 相关的一切都在 `_merge_pr` **之前**。
2. `src/fissue/verifier/runner.py`：把回归结果并入 `judge_f2p` 之外的判定，
   建议新增一个纯函数（便于单测）：

   ```python
   def judge_regression(
       base_reg: VerifierRun | None,
       fix_reg: VerifierRun | None,
       *, mode: str, f2p_satisfied: bool,
   ) -> tuple[bool, str]:
       """返回 (是否通过, 说明)。严格按 §3.3 判定矩阵。"""
   ```

3. **`judge_f2p` 本身不要改语义**——F2P 与回归门是两个独立判据，混在一起会破坏
   现有测试对 F2P 的契约（`f2p_satisfied` / `reproducible` / `fixed`）。
4. 自动修复侧（`src/fissue/fixer/autofix.py`）：`strict` 模式下 `judge_regression`
   未通过 → 走既有 `_handle_failure` 路径（`needs_manual` + 手工报告）。

### 环节 5：报告与文档

1. `report` / `export` / Web 看板：展示回归门结果（至少 `warn` 模式要可见）。
2. `docs/DESIGN.md` §4 补一小节，指向本文并给出判定矩阵。
3. `docs/USAGE.md` 配置章节补 `regression_gate` 的三档说明与推荐姿势
   （先 `warn` 跑几天看多少仓库是不绿的，再决定是否上 `strict`）。

---

## 6. 验收标准

实施者交付前必须逐条自证：

### 6.1 行为正确性

- [ ] `regression_gate=off` 时**零开销**：不发起任何沙盒执行（用 spy/mock 断言未调用）。
- [ ] `warn` 模式下，既有测试失败**不改变** F2P 结论，但报告/日志里能看到告警。
- [ ] `strict` 模式下，构造「base 绿、fix 红」的场景 → 修复被拒，落 `needs_manual`，
      且结论文案**点名了失败的测试**。
- [ ] `strict` 模式下，构造「base 红」（仓库本来就不绿）→ **不阻断**，记 warn 并放行。
      **这是必须单独验证的一条**，写错会导致大面积误杀。
- [ ] base 回归门失败时**不**污染 F2P 的 `reproducible/fixed/f2p_satisfied` 语义。
- [ ] PR 流水线里，base 回归门在 `_merge_pr` **之前**执行（可仿照
      `test_pr_verify_runs_base_before_merge` 用「记录调用顺序」的桩来断言）。
- [ ] 验证器写入的测试文件被排除：造一个「该文件会失败」的场景，回归门仍 `PASS`。
- [ ] 命令探不到时返回 `SKIPPED` 且不阻断流程。
- [ ] 超时不无限等待：到 `regression_timeout_seconds` 即返回 `TIMEOUT`。

### 6.2 成本与规模

- [ ] 证明成本与 Issue 数无关：代码路径里**没有**按 Issue 数增长的检索/LLM 调用。
- [ ] `warn` 模式下不新增任何 LLM 调用（用 stub LLM 断言 `calls == 0`）。

### 6.3 工程要求

- [ ] `pytest -q` 全量通过（当前基线 **362 passed**），且**无新增失败**。
- [ ] 新增用例覆盖 §6.1 的全部条目。
- [ ] 改动文件 `ruff check` **不新增**问题（仓库既有 lint 问题不算）。
- [ ] 同步更新 `config.example.yaml`、`DESIGN.md`、`USAGE.md`。
- [ ] 涉及文件写盘时**不得**用文本模式（本项目已多次踩坑：Windows 上 `write_text`
      会把 `\n` 转 `\r\n`，破坏补丁/换行敏感产物）。参照 `_write_patch`。
- [ ] 子进程调用**必须**显式 `encoding="utf-8"`（本项目已多次踩坑：
      中文环境下按 GBK 解码会乱码）。

---

## 7. 风险与已知取舍

| 风险 | 说明 | 处置 |
|---|---|---|
| 老仓库普遍不绿 | `strict` 会大面积误杀 | 默认 `warn`；先用它统计「不绿率」再决定 |
| 套件很慢 | 每 Issue 2 次全量运行 | 独立超时预算 + 到点放弃；可选结果缓存 |
| 依赖装不全 | 沙盒禁网，仓库依赖可能装不上 | base 红即判不可信、不阻断（§3.3 第 3 行） |
| 测试发现机制扫到验证器文件 | base 阶段误失败 | §4.3 临时移出 + 基于 `git ls-files` 限定 |
| 与 F2P 语义混淆 | 把回归失败记成「修复无效」 | 独立判据函数 + 独立 stage 标识 |

---

## 8. 参考实现锚点

| 需要复用的东西 | 位置 |
|---|---|
| 沙盒执行入口 | `src/fissue/verifier/runner.py` `VerifierRunner.run_once` |
| 命令固定化（不许 LLM 现编） | 同上，`spec.command` 的处理方式 |
| 结果落库 | `Repository.save_verifier_run` |
| 产物留存 | `VerifierRunner._save_artifacts` |
| 判定矩阵的现成先例 | `judge_f2p`（`src/fissue/verifier/runner.py`） |
| PR 顺序契约 | `tests/test_pipeline.py::test_pr_verify_runs_base_before_merge` |
| 沙盒内跑脚本的测试写法 | `tests/test_sandbox_verifier.py::_sandbox_outcome` |
| 二进制写文件范例 | `src/fissue/fixer/autofix.py::_write_patch` |
