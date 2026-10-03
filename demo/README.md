# Fissue 测试夹具：textkit

一个刻意设计的小型 Python 库，用来**真实验证 Fissue 的完整链路**：
抓取 → AI 评测 → 生成验证器 → 沙盒 F2P 验证 → 自动修复提 PR。

## 目录结构

```
demo/
├── textkit/                     待推送的测试仓库（独立 git 仓库）
│   ├── textkit/                 库源码（植入 2 个 bug）
│   ├── tests/                   32 个基线测试（全绿，刻意不覆盖那两个 bug）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                    5 个 Issue 的内容与元数据
│   ├── issue-01-word-count-empty.md          tier1：难度低 + 重要性高 → 应自动修复
│   ├── issue-02-slugify-collapse.md          tier2：难度低 + 重要性低 → 应自动修复
│   ├── issue-03-inconsistent-text-semantics.md  难度高 → 不应自动修复
│   ├── issue-04-wrap-cjk-support.md          FEATURE → 只打标签
│   └── issue-05-slugify-extra-dashes.md      疑似重复 → 应被识别
└── scripts/setup_github.py      一键建仓库 / 建 Issue / 建 PR
```

## 设计意图：每个 fixture 考什么

### 植入的 2 个真实 Bug（未被基线测试覆盖）

| # | Bug | 表现 | 期望 Fissue 行为 |
|---|---|---|---|
| 1 | `word_count("")` 返回 1 | 空串应返回 0 | 生成验证器 → base 失败 → tier1 → 自动修复 |
| 2 | `slugify("Hello   World")` → `hello---world` | 连续空白未折叠 | 生成验证器 → tier2 → 自动修复 |

> 关键在于**基线 32 个测试是全绿的**——bug 藏在没被覆盖的路径上。
> 这正好逼 Fissue 真正去「读代码 → 写验证器 → 跑 F2P」，而不是抄现成测试。

### 两个 PR（一个真好、一个半吊子）

| PR | 分支 | 修的 Issue | 质量 | 期望 Fissue 行为 |
|---|---|---|---|---|
| 1 | `fix/word-count-empty` | #1 | ✅ 完整修复，含回归测试 | 验证器通过 → 建议合并 |
| 2 | `fix/slugify-collapse` | #2 | ⚠️ **只修一半** | 验证器仍失败 → 建议不合并 |

PR 2 是刻意设计的**假修复**：它用 `\s+` 折叠了连续空格，
但 `Hello - World`（空格与连字符混排）仍产出 `hello---world`。

```
✅ "Hello   World"  -> hello-world    （修好了）
❌ "Hello - World"  -> hello---world   （漏了）
```

这是最有价值的一个用例——它检验 Fissue 能不能**识破看似正确的修复**。

### 其余 3 个 Issue 的作用

| Issue | 作用 |
|---|---|
| #3 文本口径不统一 | 难度高、需设计决策 → 检验「不该自动修的要拦住」 |
| #4 CJK 折行 | FEATURE → 检验「只打标签、不生成验证器」 |
| #5 多余的横线 | 与 #2 高度重叠 → 检验反刷子的**重复检测** |

---

## 一、推送到 GitHub

### 方式 A：一键脚本（推荐）

只需要一个 GitHub token（勾 `repo` 权限）。

```bash
# 1) 准备 token
#    https://github.com/settings/tokens  →  Generate new token (classic)  →  勾 repo
export GITHUB_TOKEN=ghp_xxxx

# 2) 先干跑看看计划（不写任何东西）
python demo/scripts/setup_github.py --owner 你的用户名 --dry-run

# 3) 真跑
python demo/scripts/setup_github.py --owner 你的用户名
```

脚本会依次完成：

1. 创建 `你的用户名/textkit` 仓库（已存在则复用）
2. 推送 `main` 分支
3. 从 `fixtures/*.md` 创建 5 个 Issue（带标签）
4. 推送 2 个修复分支并创建 2 个 PR（正文里带 `Closes #N`）

脚本是**可重入**的：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过，失败可放心重跑。

常用参数：

```bash
--name textkit-demo    # 换个仓库名
--private              # 建私有仓库（注意：Fissue 需要 token 才能读私有库）
--no-prs               # 只建 Issue，先看看评测效果
```

### 方式 B：手动推送

```bash
cd demo/textkit

# 1) 在 GitHub 网页上新建一个空仓库（不要勾 README/gitignore）
#    然后关联并推送主分支
git remote add origin https://github.com/你的用户名/textkit.git
git push -u origin main

# 2) 推送两个修复分支
git push origin fix/word-count-empty
git push origin fix/slugify-collapse
```

然后在网页上：

- 按 `demo/fixtures/*.md` 的 **title / labels / 正文** 逐个建 5 个 Issue
  （文件头部的 `<!-- ... -->` 注释里就是标题和标签，正文是注释之后的部分）
- 建 2 个 PR，base 选 `main`，head 分别选两个修复分支

---

## 二、用 Fissue 测它

把仓库登记进 Fissue 的 `config.yaml`：

```yaml
repos:
  - platform: github
    owner: 你的用户名
    name: textkit
    base_branch: main
    collect: [issue, pr]
    since_days: 0        # 0 = 不限制时间窗口，确保 5 个 Issue 全抓到
    test_hint: "python -m pytest -q"    # 明确告诉它怎么跑测试
```

然后跑完整链路：

```bash
# 1) 抓取（应拉到 5 个 Issue + 2 个 PR）
fissue fetch --repo 你的用户名/textkit --limit 50

# 2) 评测（四维评分 + 分类 + 反刷子）
fissue eval --repo 你的用户名/textkit --all

# 3) 看结果：Issue 1/2 应为 tier1/tier2，#4 应为 feature
fissue report --repo 你的用户名/textkit

# 4) 生成验证器并做 F2P 验证
fissue verify --repo 你的用户名/textkit

# 5) 批量定论 + 打标签
fissue flush

# 6) 试跑自动修复（只出补丁，不提 PR）
fissue fix --repo 你的用户名/textkit --dry-run --limit 5
```

---

## 三、期望结果对照表

跑完后逐项核对，就能判断 Fissue 是否真的在工作：

| # | 条目 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|
| 1 | word_count 空串 | bug | **tier1** | base 失败（可复现） | 自动修复 → 提 PR |
| 2 | slugify 连续空白 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 3 | 文本口径不统一 | bug | **none** | 难自动化 | 不修复，打标签 |
| 4 | CJK 折行 | **feature** | none | 不验证 | 只打标签 |
| 5 | 多余的横线 | bug | none | — | **疑似重复 #2** |
| PR1 | word_count 修复 | bug | none | **F2P 成立** | 建议合并 |
| PR2 | slugify 修复 | bug | none | **F2P 不成立** | 建议不合并 ⚠️ |

第 7 行是最关键的一项：如果 Fissue 把 PR2 也判为「可合并」，说明
它的验证器没能覆盖「空格+连字符混排」这个场景，需要检查验证器生成质量。

---

## 四、本地自检（不连网）

推之前可以先在本地确认夹具是自洽的：

```bash
cd demo/textkit
python -m pytest -q            # 基线应 32 passed

# 确认 bug 确实存在
python -c "
import sys; sys.path.insert(0,'.')
from textkit import word_count, slugify
print('word_count(\"\") =', word_count(''))                    # 期望 1（bug）
print('slugify(\"a   b\") =', repr(slugify('a   b')))          # 期望 a---b（bug）
"
```

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | token 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用（脚本会自动跳过） |
| `textkit 工作区不干净` | demo 仓库有未提交改动 | `cd demo/textkit && git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异，或 PR 已存在 | 脚本会跳过已存在的；确认分支已推送 |
| 推送后换行符变了 | Windows 上 CRLF 混入 | 仓库已有 `.gitattributes` 强制 LF，确认它也在目标仓库里 |

> **为什么强调换行符**：Fissue 验证 PR 时用 `git apply` 打平台返回的 diff。
> 如果仓库里混入 CRLF，diff 的上下文行与实际文件字节不一致，补丁会应用失败。
> 所以 `demo/textkit/.gitattributes` 里固定了 `* text=auto eol=lf`。
