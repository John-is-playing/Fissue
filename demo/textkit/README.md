# 夹具：textkit

一个刻意设计的小型 Python 库，用来真实验证 Fissue 的完整链路：
抓取 → 评测 → 生成验证器 → 沙盒 F2P → 自动修复提 PR。

> 布局与另外四套（`ratekit` / `pipekit` / `envkit` / `lockkit`）完全一致。

```
demo/textkit/
├── repo/                    待推送仓库（独立 git 仓库）
│   ├── textkit/             库源码（植入 2 个 bug）
│   ├── tests/               32 个基线测试（全绿，刻意不覆盖那两个 bug）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                5 个 Issue 的内容与元数据
│   ├── issue-01-word-count-empty.md
│   ├── issue-02-slugify-collapse.md
│   ├── issue-03-inconsistent-text-semantics.md
│   ├── issue-04-wrap-cjk-support.md
│   └── issue-05-slugify-extra-dashes.md
└── scripts/setup_github.py  一键建仓库 / Issue / PR
```

## 设计意图

### 植入的 2 个真实 Bug（基线测试未覆盖）

| # | Bug | 表现 | 期望 Fissue 行为 |
|---|---|---|---|
| 1 | `word_count("")` 返回 1 | 空串应返回 0 | 生成验证器 → base 失败 → tier1 → 自动修复 |
| 2 | `slugify("Hello   World")` → `hello---world` | 连续空白未折叠 | 生成验证器 → tier2 → 自动修复 |

关键在于**32 个基线测试全绿**——bug 藏在没被覆盖的路径上，这逼 Fissue 真去「读代码 → 写验证器
→ 跑 F2P」，而不是抄现成测试。

### 两个 PR（一个真好、一个半吊子）

| PR | 分支 | 修的 Issue | 质量 | 期望行为 |
|---|---|---|---|---|
| 1 | `fix/word-count-empty` | #1 | ✅ 完整修复，含回归测试 | 验证器通过 → 建议合并 |
| 2 | `fix/slugify-collapse` | #2 | ⚠️ **只修一半** | 验证器仍失败 → 建议不合并 |

PR 2 是刻意设计的**假修复**：用 `\s+` 折叠了连续空格，但空格与连字符混排仍出错：

```
✅ "Hello   World"  -> hello-world    （修好了）
❌ "Hello - World"  -> hello---world   （漏了）
```

这是整套最有价值的用例——检验 Fissue 能不能**识破看似正确的修复**。

### 其余 3 个 Issue

| Issue | 作用 |
|---|---|
| #3 文本口径不统一 | 难度高、需设计决策 → 检验「不该自动修的要拦住」 |
| #4 CJK 折行 | FEATURE → 检验「只打标签、不生成验证器」 |
| #5 多余的横线 | 与 #2 高度重叠 → 检验**重复检测** |

## 一、推送到 GitHub

```bash
export GITHUB_TOKEN=ghp_xxxx        # https://github.com/settings/tokens → classic → 勾 repo
python demo/textkit/scripts/setup_github.py --owner 你的用户名 --dry-run   # 先干跑
python demo/textkit/scripts/setup_github.py --owner 你的用户名            # 真跑
```

脚本依次：创建 `你的用户名/textkit`（已存在则复用）→ 推送 `main` → 按 `fixtures/*.md` 建 5 个
Issue（带标签）→ 推送 2 个修复分支并建 2 个 PR（正文带 `Closes #N`）。**可重入**：已存在的 Issue
（按标题）与 PR（按 head 分支）会跳过，失败可放心重跑。

常用参数：`--name textkit-demo`（换仓库名）· `--private`（私有库需 token 才能读）· `--no-prs`
（只建 Issue 先看评测）。

手动方式：在网页新建空仓库 → `cd demo/textkit/repo && git remote add origin ... && git push -u
origin main` → 推送两个修复分支 → 按 fixtures 头部注释（title / labels）与正文逐个建 Issue，再
建 2 个 PR（base `main`）。

## 二、用 Fissue 测它

```yaml
repos:
  - platform: github
    owner: 你的用户名
    name: textkit
    base_branch: main
    collect: [issue, pr]
    since_days: 0                       # 不限时间窗口，确保 5 个 Issue 全抓到
    test_hint: "python -m pytest -q"
```

```bash
fissue fetch  --repo 你的用户名/textkit --limit 50          # 应拉到 5 Issue + 2 PR
fissue eval   --repo 你的用户名/textkit --all
fissue report --repo 你的用户名/textkit                     # 看分类与优先级
fissue verify --repo 你的用户名/textkit                     # F2P
fissue flush                                                # 定论 + 打标签
fissue fix    --repo 你的用户名/textkit --dry-run --limit 5  # 只出补丁
```

## 三、期望结果对照表

| # | 条目 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|
| 1 | word_count 空串 | bug | **tier1** | base 失败（可复现） | 自动修复 → 提 PR |
| 2 | slugify 连续空白 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 3 | 文本口径不统一 | bug | **none** | 难自动化 | 不修复，打标签 |
| 4 | CJK 折行 | **feature** | none | 不验证 | 只打标签 |
| 5 | 多余的横线 | bug | none | — | **疑似重复 #2** |
| PR1 | word_count 修复 | bug | none | **F2P 成立** | 建议合并 |
| PR2 | slugify 修复 | bug | none | **F2P 不成立** | 建议不合并 ⚠️ |

最后一行是最关键的一项：若 Fissue 把 PR2 也判为可合并，说明验证器没覆盖「空格+连字符混排」，
需要检查验证器生成质量。

## 四、本地自检（不连网）

```bash
cd demo/textkit/repo && python -m pytest -q      # 基线应 32 passed
python -c "
import sys; sys.path.insert(0,'.')
from textkit import word_count, slugify
print(word_count(''))          # 期望 1（bug）
print(repr(slugify('a   b')))  # 期望 a---b（bug）
"
```

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用 |
| `textkit 工作区不干净` | demo 仓库有未提交改动 | `git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异 / PR 已存在 | 脚本会跳过；确认分支已推送 |
| 推送后换行符变了 | Windows CRLF 混入 | 仓库已有 `.gitattributes` 强制 LF |

> 为什么强调换行符：Fissue 验证 PR 时用 `git apply` 打平台返回的 diff，混入 CRLF 会让上下文行与
> 实际字节不一致，补丁应用失败。`demo/textkit/repo/.gitattributes` 固定了 `* text=auto eol=lf`。

---

相关：[夹具总览](../README.md) · [BENCHMARK.md](../../BENCHMARK.md) · [使用手册](../../docs/USAGE.md)