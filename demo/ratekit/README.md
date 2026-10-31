# 夹具：ratekit

一个**全新、纯净**的测试仓库夹具，与 `textkit` 完全独立：领域不同（费率/金额 vs 文本处理）、
代码不同、植入问题也不同。它回答一个更硬的问题：

> **换一个仓库、换一套完全没见过的代码，Fissue 还能不能同样准确地分诊、验证、修复？**

10 个 Issue + 3 个 PR，其中刻意混入错误、无效、重复、刷量的请求。

## 目录结构

```
demo/ratekit/
├── repo/                    待推送仓库（独立 git 仓库）
│   ├── ratekit/             库源码（植入 7 处缺陷）
│   ├── tests/               36 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                10 个 Issue
│   ├── issue-01-format-amount-int-loses-decimals.md   tier1
│   ├── issue-02-remove-tax-wrong-formula.md           tier1
│   ├── issue-03-parse-amount-drops-negative-sign.md   tier1
│   ├── issue-04-percent-negative-sign.md              tier2
│   ├── issue-05-weekend-saturday-missed.md            tier2
│   ├── issue-06-float-rounding-inconsistent.md        难度高 → 不修
│   ├── issue-07-hourly-billing-support.md             FEATURE → 只打标签
│   ├── issue-08-duplicate-remove-tax.md               与 #2 重复
│   ├── issue-09-invalid-parse-should-return-zero.md   **误报**
│   └── issue-10-spam-performance-rewrite.md           **刷量**
└── scripts/setup_github.py  一键建仓库 / Issue / PR
```

## 设计意图

### 植入的 7 处真实缺陷（基线测试未覆盖）

| # | 位置 | 表现 | 期望行为 |
|---|---|---|---|
| 1 | `format_amount(1234)` | `'1,234'`，int 入参丢小数位 | tier1 → 自动修复 |
| 2 | `remove_tax(113.0, 0.13)` | `98.31`，应为 `gross/(1+rate)` | tier1 → 自动修复 |
| 3 | `parse_amount("-5.00")` | `5.0`，负号被清洗（资金方向颠倒） | tier1 → 自动修复 |
| 4 | `percent_of(200, -5)` | `10.0`，`abs()` 抹掉符号 | tier2 → 自动修复 |
| 5 | `is_weekend(2024-01-06)` | `False`，只认星期日 | tier2 → 自动修复 |
| 6 | `prorate(10.0, 1, 3)` | `3.33`，应向上取整 `3.34` | 难度偏高 → 不修 |
| 7 | `refund(100.0, 0, 30)` | `100.0`，向下取整丢分 | 同上 |

**基线 36 个测试全绿**——缺陷藏在未覆盖的边界上，逼 Fissue 真去「读代码 → 写验证器 → 跑 F2P」。

### 三个 PR：真好 / 半吊子 / 无效

| PR | 分支 | 修的 Issue | 性质 | 期望行为 |
|---|---|---|---|---|
| 1 | `fix/remove-tax-inverse` | #2 | ✅ 完整修复 + 回归测试 | F2P + 回归门通过 → 建议合并 |
| 2 | `fix/weekend-and-working-days` | #5 | ⚠️ **修好目标却弄坏别处** | F2P 成立但**回归门拦住** → 不合并 |
| 3 | `fix/percent-negative-sign` | #4 | ❌ **假修复**（`abs(x)` 换成 `x * -1`） | F2P 不成立 → 不合并 |

**PR 2 是这套最有价值的一项**：`is_weekend` 修对了（周六返回 `True`），却在同一提交里把
`add_working_days` 的循环写错（`while remaining >= 0`），三个既有测试由绿变红。只看 F2P 会判
「可合并」，只有回归门拦得住。

**PR 3 是假动作**：`abs(-5)` 与 `-5 * -1` 都是 `5`，行为没变，目标用例仍失败。

### 其余 Issue

| Issue | 作用 |
|---|---|
| #6 精度口径不统一 | 难度高、需设计决策 → 检验「不该自动修的要拦住」 |
| #7 按小时计费 | FEATURE → 只打标签、不生成验证器 |
| #8 含税反推不对（无复现） | 与 #2 高度重叠 → 检验**重复检测** |
| #9 非法输入应返回 0 | **误报**：抛异常是该库的正确设计 → 真实性应判低 |
| #10 性能严重问题 + 推广 | **刷量**：无复现、模板化、夹带联系方式 → 真实性应判低 |

## 一、推送到 GitHub（写操作请自行执行）

```bash
export GITHUB_TOKEN=ghp_xxxx        # https://github.com/settings/tokens → classic → 勾 repo
.venv/Scripts/python.exe demo/ratekit/scripts/setup_github.py --owner John-is-playing --dry-run
.venv/Scripts/python.exe demo/ratekit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`：本机有 HTTPS 中间人，全局 Python 缺 truststore 会 TLS 失败。

脚本依次：建仓库（已存在则复用）→ 推 `main` → 按 `fixtures/*.md` 建 10 个 Issue（带标签）→ 推 3 个
修复分支并建 3 个 PR（正文带 `Closes #N`）。**可重入**：已存在的 Issue（按标题）与 PR（按 head
分支）会跳过。参数：`--name`、`--private`、`--no-prs`。

手动方式：`cd demo/ratekit/repo && git remote add origin ... && git push -u origin main`，再推
`fix/remove-tax-inverse fix/weekend-and-working-days fix/percent-negative-sign`，然后按 fixtures
头部注释建 Issue、按分支建 PR。

## 二、用 Fissue 测它

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: ratekit
    base_branch: main
    collect: [issue, pr]
    since_days: 0                       # 不限时间窗口，确保 10 个 Issue 全抓到
    test_hint: "python -m pytest -q"
```

```bash
fissue fetch  --repo John-is-playing/ratekit --limit 50     # 应拉到 10 Issue + 3 PR
fissue eval   --repo John-is-playing/ratekit --all
fissue report --repo John-is-playing/ratekit
fissue verify --repo John-is-playing/ratekit
fissue flush
fissue fix    --repo John-is-playing/ratekit --dry-run --limit 6
```

## 三、期望结果对照表

| # | 条目 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|
| 1 | format_amount int 丢小数位 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 2 | remove_tax 公式错 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 3 | parse_amount 丢负号 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 4 | percent_of 负数取绝对值 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 5 | is_weekend 漏周六 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 6 | 精度口径不统一 | bug | none | 难自动化 | 不修复，打标签 |
| 7 | 按小时计费 | **feature** | none | 不验证 | 只打标签 |
| 8 | 含税反推不对 | bug | none | — | **疑似重复 #2** |
| 9 | 非法输入应返回 0 | **低真实性** | none | — | 判为误报 |
| 10 | 性能问题 + 推广 | **低真实性** | none | — | 判为刷量 |
| PR1 | remove_tax 修复 | bug | none | **F2P 成立** | 建议合并 |
| PR2 | is_weekend 修复 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR3 | percent_of「修复」 | bug | none | **F2P 不成立** | 建议不合并 |

PR2 是最关键的一项：Fissue 必须在**目标用例已通过**的前提下，靠既有测试回归门发现它破坏了三处
既有行为。若把 PR2 判为可合并，说明回归门没生效——检查 `verifier.regression_gate` 是否为
`strict`（`warn` 只告警不拦）。

## 四、本地自检（不连网）

```bash
cd demo/ratekit/repo && .venv/Scripts/python.exe -m pytest -q    # 基线应全绿
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
from datetime import date
from ratekit import format_amount, remove_tax, percent_of, prorate, refund, is_weekend, parse_amount
print(format_amount(1234))          # '1,234'（缺陷）
print(remove_tax(113.0, 0.13))      # 98.31（缺陷）
print(percent_of(200, -5))          # 10.0（缺陷）
print(prorate(10.0, 1, 3))          # 3.33（缺陷）
print(refund(100.0, 0, 30))         # 100.0（缺陷）
print(is_weekend(date(2024,1,6)))   # False（缺陷）
print(parse_amount('-5.00'))        # 5.0（缺陷）
"
```

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用 |
| `ratekit 工作区不干净` | repo 有未提交改动 | `git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异 / PR 已存在 | 脚本会跳过；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe`（含 truststore） |

> 换行符：Fissue 验证 PR 时用 `git apply` 打平台返回的 diff，混入 CRLF 会让上下文行与字节不一致，
> 补丁应用失败。`repo/.gitattributes` 固定了 `* text=auto eol=lf`。

---

相关：[夹具总览](../README.md) · [BENCHMARK.md](../../BENCHMARK.md) · [缺陷与修复记录](../../30ae40b-BUG.md)