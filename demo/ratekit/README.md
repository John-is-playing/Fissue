# Fissue 测试夹具（二）：ratekit

一个**全新的、纯净的**测试仓库夹具，与 `demo/textkit` 完全独立。
领域不同（费率/金额 vs 文本处理）、代码不同、植入的问题也不同，
用来回答一个更硬的问题：

> **Fissue 换一个仓库、换一套完全没见过的代码，还能不能同样准确地分诊、验证、修复？**

它一次覆盖 10 个 Issue + 3 个 PR，其中**刻意混入错误、无效、重复、刷量的请求**。

## 目录结构

```
demo/ratekit/
├── repo/                        待推送的测试仓库（独立 git 仓库）
│   ├── ratekit/                 库源码（植入 7 处缺陷）
│   ├── tests/                   36 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                    10 个 Issue 的内容与元数据
│   ├── issue-01-format-amount-int-loses-decimals.md   tier1：难度低 + 重要性高 → 应自动修复
│   ├── issue-02-remove-tax-wrong-formula.md           tier1：难度低 + 重要性高 → 应自动修复
│   ├── issue-03-parse-amount-drops-negative-sign.md   tier2：难度低 + 重要性低 → 应自动修复
│   ├── issue-04-percent-negative-sign.md              tier2：难度低 + 重要性低 → 应自动修复
│   ├── issue-05-weekend-saturday-missed.md            tier2：难度低 + 重要性低 → 应自动修复
│   ├── issue-06-float-rounding-inconsistent.md        难度高 → 不应自动修复
│   ├── issue-07-hourly-billing-support.md             FEATURE → 只打标签
│   ├── issue-08-duplicate-remove-tax.md               与 #2 重复 → 应被识别
│   ├── issue-09-invalid-parse-should-return-zero.md   **误报/无效** → 真实性应判低
│   └── issue-10-spam-performance-rewrite.md           **刷量**（无复现+推广）→ 真实性应判低
└── scripts/setup_github.py      一键建仓库 / 建 Issue / 建 PR
```

## 设计意图：每个 fixture 考什么

### 植入的 7 处真实缺陷（未被基线测试覆盖）

| # | 位置 | 表现 | 期望 Fissue 行为 |
|---|---|---|---|
| 1 | `format_amount(1234)` | `'1,234'`，int 入参丢小数位 | 生成验证器 → base 失败 → tier1 → 自动修复 |
| 2 | `remove_tax(113.0, 0.13)` | `98.31`，公式应为 `gross/(1+rate)` | 生成验证器 → base 失败 → tier1 → 自动修复 |
| 3 | `parse_amount("-5.00")` | `5.0`，负号被清洗掉 | tier2 → 自动修复 |
| 4 | `percent_of(200, -5)` | `10.0`，负数被 `abs()` 抹掉符号 | tier2 → 自动修复 |
| 5 | `is_weekend(2024-01-06)` | `False`，只认星期日 | tier2 → 自动修复 |
| 6 | `prorate(10.0, 1, 3)` | `3.33`，应向上取整为 `3.34` | 难自动化/边界 → 可判难度偏高 |
| 7 | `refund(100.0, 0, 30)` | `100.0`，向下取整丢了分 | 同上 |

> **关键**：基线 36 个测试是**全绿**的——缺陷藏在没被覆盖的边界上。
> 这正好逼 Fissue 真正去「读代码 → 写验证器 → 跑 F2P」，而不是抄现成测试。

### 三个 PR：一个真好、一个半吊子、一个无效

| PR | 分支 | 修的 Issue | 质量 | 期望 Fissue 行为 |
|---|---|---|---|---|
| 1 | `fix/remove-tax-inverse` | #2 | ✅ 完整修复，含回归测试 | F2P 成立 + 回归门通过 → 建议合并 |
| 2 | `fix/weekend-and-working-days` | #5 | ⚠️ **修好目标却弄坏别处** | F2P 成立但**回归门拦住** → 建议不合并 |
| 3 | `fix/percent-negative-sign` | #4 | ❌ **假修复**（`abs(x)` 换成 `x * -1`） | F2P 不成立 → 建议不合并 |

**PR 2 是这套夹具最有价值的一项**：它把 `is_weekend` 修好了（周六正确返回 `True`），
却在同一提交里把 `add_working_days` 的循环写错（`while remaining >= 0`），
导致三个既有测试从绿变红。若 Fissue 只看 F2P，会把它判为「可合并」；
只有跑了**既有测试回归门**才拦得住。

**PR 3 是「假动作」**：改动看起来像是在修 bug，实际行为没变
（`abs(-5)` 与 `-5 * -1` 都是 `5`），目标用例仍然失败 —— 检验 Fissue 能不能识破。

```
✅ percent_of(200, -5)  -> -10.0      （正确修法）
❌ percent_of(200, -5)  ->  10.0      （PR3 改完仍是这个）
```

### 其余 Issue 的作用

| Issue | 作用 |
|---|---|
| #6 精度口径不统一 | 难度高、需设计决策 → 检验「不该自动修的要拦住」 |
| #7 按小时计费 | FEATURE → 检验「只打标签、不生成验证器」 |
| #8 含税反推不对（无复现细节） | 与 #2 高度重叠 → 检验反刷子的**重复检测** |
| #9 非法输入应返回 0 | **误报**：抛异常是该库的正确设计 → 真实性应判低 |
| #10 性能严重问题+推广 | **刷量**：无复现、模板化、夹带联系方式 → 真实性应判低 |

## 一、推送到 GitHub

GitHub 的写操作（建仓库 / 推分支 / 建 Issue-PR）由你自己执行：

```bash
# 1) 准备 token（勾 repo 权限）
#    https://github.com/settings/tokens  →  Generate new token (classic)  →  勾 repo
export GITHUB_TOKEN=ghp_xxxx

# 2) 先干跑看看计划（不写任何东西）
.venv/Scripts/python.exe demo/ratekit/scripts/setup_github.py --owner John-is-playing --dry-run

# 3) 真跑
.venv/Scripts/python.exe demo/ratekit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`（本机有 HTTPS 中间人，
> 全局 Python 缺 truststore 会 TLS 失败，必须用 .venv 里的解释器）。

脚本会依次完成：

1. 创建 `John-is-playing/ratekit` 仓库（已存在则复用）
2. 推送 `main` 分支
3. 从 `fixtures/*.md` 创建 10 个 Issue（带标签）
4. 推送 3 个修复分支并创建 3 个 PR（正文里带 `Closes #N`）

脚本是**可重入**的：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过，失败可放心重跑。

常用参数：

```bash
--name ratekit-demo    # 换个仓库名
--private              # 建私有仓库（注意：Fissue 需要 token 才能读私有库）
--no-prs               # 只建 Issue，先看看评测效果
```

### 手动推送（方式 B）

```bash
cd demo/ratekit/repo
git remote add origin https://github.com/John-is-playing/ratekit.git
git push -u origin main
git push origin fix/remove-tax-inverse fix/weekend-and-working-days fix/percent-negative-sign
```

然后在网页上按 `fixtures/*.md` 头部的 `<!-- ... -->`（title / labels）逐个建 Issue，
再为三个分支各建一个 PR（base 选 `main`）。

## 二、用 Fissue 测它

把仓库登记进 `config.yaml` 的 `repos:`：

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: ratekit
    base_branch: main
    collect: [issue, pr]
    since_days: 0        # 0 = 不限制时间窗口，确保 10 个 Issue 全抓到
    test_hint: "python -m pytest -q"    # 明确告诉它怎么跑测试
```

然后跑完整链路：

```bash
# 1) 抓取（应拉到 10 个 Issue + 3 个 PR）
fissue fetch --repo John-is-playing/ratekit --limit 50

# 2) 评测（四维评分 + 分类 + 反刷子）
fissue eval --repo John-is-playing/ratekit --all

# 3) 看结果：#1/#2 应为 tier1，#3/#4/#5 应为 tier2，#7 应为 feature
fissue report --repo John-is-playing/ratekit

# 4) 生成验证器并做 F2P 验证
fissue verify --repo John-is-playing/ratekit

# 5) 批量定论 + 打标签
fissue flush

# 6) 试跑自动修复（只出补丁，不提 PR）
fissue fix --repo John-is-playing/ratekit --dry-run --limit 6
```

## 三、期望结果对照表

| # | 条目 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|
| 1 | format_amount int 丢小数位 | bug | **tier1** | base 失败（可复现） | 自动修复 → 提 PR |
| 2 | remove_tax 公式错 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 3 | parse_amount 丢负号 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 4 | percent_of 负数取绝对值 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 5 | is_weekend 漏周六 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 6 | 精度口径不统一 | bug | none | 难自动化 | 不修复，打标签 |
| 7 | 按小时计费 | **feature** | none | 不验证 | 只打标签 |
| 8 | 含税反推不对 | bug | none | — | **疑似重复 #2** |
| 9 | 非法输入应返回 0 | **低真实性** | none | — | 判为误报，不打修复标签 |
| 10 | 性能问题+推广 | **低真实性** | none | — | 判为刷量，不打修复标签 |
| PR1 | remove_tax 修复 | bug | none | **F2P 成立** | 建议合并 |
| PR2 | is_weekend 修复 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR3 | percent_of「修复」 | bug | none | **F2P 不成立** | 建议不合并 |

第 12 行（PR2）是最关键的一项：Fissue 必须在**目标用例已通过**的前提下，
靠「既有测试回归门」发现它破坏了三处既有行为。
若把 PR2 判为可合并，说明回归门没生效——检查 `verifier.regression_gate`
是否为 `strict`（`warn` 只告警不拦）。

## 四、本地自检（不连网）

推之前可以先在本地确认夹具是自洽的：

```bash
cd demo/ratekit/repo
.venv/Scripts/python.exe -m pytest -q          # 基线应全绿

# 确认缺陷确实存在
python -c "
import sys; sys.path.insert(0,'.')
from datetime import date
from ratekit import format_amount, remove_tax, percent_of, prorate, refund, is_weekend, parse_amount
print(format_amount(1234))          # 期望 '1,234'（缺陷）
print(remove_tax(113.0, 0.13))      # 期望 98.31（缺陷）
print(percent_of(200, -5))          # 期望 10.0（缺陷）
print(prorate(10.0, 1, 3))          # 期望 3.33（缺陷）
print(refund(100.0, 0, 30))         # 期望 100.0（缺陷）
print(is_weekend(date(2024,1,6)))   # 期望 False（缺陷）
print(parse_amount('-5.00'))        # 期望 5.0（缺陷）
"
```

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | token 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用（脚本会自动跳过） |
| `ratekit 工作区不干净` | repo 有未提交改动 | `cd demo/ratekit/repo && git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异，或 PR 已存在 | 脚本会跳过已存在的；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe`（含 truststore） |

> **为什么强调换行符**：Fissue 验证 PR 时用 `git apply` 打平台返回的 diff。
> 如果仓库里混入 CRLF，diff 的上下文行与实际文件字节不一致，补丁会应用失败。
> 所以 `demo/ratekit/repo/.gitattributes` 里固定了 `* text=auto eol=lf`。
