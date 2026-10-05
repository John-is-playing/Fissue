# Fissue 测试夹具（四）：envkit

一个**全新的、纯净的**测试仓库夹具，与前几套完全独立：

| 夹具 | 领域 | 形态 | 考什么 |
|---|---|---|---|
| `textkit` | 文本处理 | 单模块、独立函数 | 单函数语义 |
| `ratekit` | 金额 / 费率 | 单模块、数值边界 | 数值边界 |
| `pipekit` | 分片流水线 / 区间 | 多模块、调用链耦合 | 跨模块根因定位 |
| **`envkit`** | **配置 / 缓存 / 校验** | **有状态对象 + 环境依赖** | **状态副作用、环境依赖、输入校验** |

它一次覆盖 **15 个 Issue + 5 个 PR**，其中刻意混入错误、无效、同源但不重复、刷量的请求。

## 这套夹具回答什么

前三套的缺陷都是「给定输入 → 算出错误结果」，只读代码、跑一次就能定位。
`envkit` 把缺陷挪到三个更难的位置：

> **当缺陷取决于调用顺序与历史状态、只在特定环境下成立、
> 或表现为「本该拦住的输入没拦住」时，
> Fissue 还能不能构造出稳定的最小复现、并给出正确的定论？**

三个维度各有一组代表：

- **状态与副作用**（`Registry` / `TTLCache`）：缺陷要**按特定调用顺序**才复现。
  例如 `snapshot()` 返回内部字典——只调用一次看不出来，必须「取快照 → 改快照 → 再看注册表」。
  `TTLCache` 更典型：必须注入一个**可控时钟**才能在沙盒里稳定复现过期，直接用 `time.sleep` 又慢又不稳。
  这考的是 Fissue 会不会构造**确定性**的复现装置（假时钟），而不是依赖真实时间。

- **环境依赖**（`get_bool` / `load_settings` / `parse_offset`）：
  `get_bool("TRUE")` 在大小写敏感的实现下静默失效；
  `load_settings` 只在**嵌套默认值**上才暴露污染；
  `parse_offset` 只在**西半球时区**才出错。都考「沙盒里能否稳定复现、会不会被环境差异误导」。

- **输入校验**（`parse_size` / `sanitize_filename` / `split_iso`）：
  缺陷是「**违反自身文档契约**」——文档说该支持的写法没支持、
  说该拦住的输入没拦住。同时刻意放入 #13（建议 `get_bool` 遇未知值改抛错）
  这种**与现行为冲突的偏好变更**，考「不该报的别误判成 bug」。

最具代表性的两项：

- **Issue #1**：`snapshot()` 返回内部引用。必须有「改快照 → 验注册表」的两步验证器才抓得住；
  只断言 `isinstance(snap, dict)` 的验证器会漏掉。
- **PR 2（半吊子）**：把 `resolve` 改成「带参数就重建」，
  **修好了 Issue #3 的核心用例**（不同参数 → 不同实例），
  却弄坏了正文里同时声明的另一条契约「参数相同应复用缓存」。
  只看目标用例的验证器会判它「可合并」。

## 目录结构

```
demo/envkit/
├── repo/                        待推送的测试仓库（独立 git 仓库）
│   ├── envkit/                  库源码（9 处植入缺陷，跨 7 个模块）
│   ├── tests/                   73 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                    15 个 Issue 的内容与元数据
│   ├── issue-01-snapshot-mutable.md            tier1：快照未隔离（状态）→ 自动修复
│   ├── issue-02-cache-ignores-ttl.md           tier2：读取忽略过期（状态）→ 自动修复
│   ├── issue-03-registry-resolve-stale-kwargs.md tier1：参数变化不重建（状态）→ 自动修复
│   ├── issue-04-load-settings-mutates-defaults.md tier2：污染入参（状态）→ 自动修复
│   ├── issue-05-get-bool-case-sensitive.md     tier1：大小写敏感（环境）→ 自动修复
│   ├── issue-06-parse-offset-drops-sign.md     tier2：丢负号（环境）→ 自动修复
│   ├── issue-07-parse-size-no-space.md         tier1：无空格写法报错（校验）→ 自动修复
│   ├── issue-08-sanitize-reserved-names.md     tier2：保留名未处理（校验）→ 自动修复
│   ├── issue-09-split-iso-no-range-check.md    tier2：不校验字段范围（校验）→ 自动修复
│   ├── issue-10-feature-config-files.md        FEATURE → 只打标签
│   ├── issue-11-config-type-semantics.md       难度高（需决策）→ 不自动修复
│   ├── issue-12-cache-returns-stale.md         **同源但证据独立** → 判重不误标
│   ├── issue-13-get-bool-should-raise.md       **设计偏好变更（误报）** → 真实性应判低
│   ├── issue-14-spam-performance-promo.md      **刷量** → 真实性应判低
│   └── issue-15-vague-not-reproducible.md      **无效/无法复现** → 真实性应判低
└── scripts/setup_github.py      一键建仓库 / 建 Issue / 建 PR
```

## 设计意图：每个 fixture 考什么

### 植入的 9 处真实缺陷（基线测试刻意不覆盖）

| # | 位置 | 维度 | 表现 | 期望 Fissue 行为 |
|---|---|---|---|---|
| 1 | `registry.snapshot` | 状态 | 返回内部字典，改快照即污染注册表 | tier1 → 自动修复 |
| 2 | `cache.get` | 状态 | 不比较过期时间，`ttl` 形同虚设 | tier2 → 自动修复 |
| 3 | `registry.resolve` | 状态 | 参数变了仍返回旧实例（串库） | tier1 → 自动修复 |
| 4 | `config.load_settings` | 状态 | 浅拷贝 + 写入，污染调用方 `defaults` | tier2 → 自动修复 |
| 5 | `config.get_bool` | 环境 | 未做大小写归一，`TRUE` 静默失效 | tier1 → 自动修复 |
| 6 | `timeutil.parse_offset` | 环境 | 丢掉负号，`-05:30` → `330` | tier2 → 自动修复 |
| 7 | `units.parse_size` | 校验 | 只按空格切分，`512KB` 直接抛错 | tier1 → 自动修复 |
| 8 | `validate.sanitize_filename` | 校验 | 平台保留名（`CON`/`NUL`）原样放行 | tier2 → 自动修复 |
| 9 | `timeutil.split_iso` | 校验 | 不校验时/分/秒范围，`08:75:00` 被接受 | tier2 → 自动修复 |

> **关键**：基线 73 个测试是**全绿**的——每处缺陷都藏在没被覆盖的边界上。
> 这逼 Fissue 真正去「读代码 → 写验证器 → 跑 F2P」，而不是抄现成测试。

每处缺陷都**违反自身文档契约**（文档写了该支持的写法/该拦的输入，实现没做到），
而不是「另一个合理口径」——这样验证器才有唯一正确答案。

缺陷 #2 与 #12 是刻意的一对：`cache.get` 忽略过期是**真缺陷**（有最小复现），
#12 报「缓存偶尔返回上一次请求的残留数据」听起来像同一个问题，
但**给不出最小复现**、指向的是并发而非 TTL。二者同源而证据独立，
用来检验判重**从严、不误标**。

### 五个 PR：真好×2 / 半吊子 / 文档冒充 / 回归门

| PR | 分支 | 关联 | 性质 | 期望 Fissue 行为 |
|---|---|---|---|---|
| 1 | `fix/snapshot-readonly` | #1 | ✅ 完整修复 + 回归测试 | F2P 成立 + 回归门通过 → 建议合并 |
| 2 | `fix/resolve-opt-in-rebuild` | #3 | ⚠️ **修好目标却违反正文另一条契约** | 建议不合并 |
| 3 | `fix/docs-config-files` | #10 | ❌ **只改文档，冒充功能实现** | FEATURE + 无代码 → 建议不合并 |
| 4 | `fix/sanitize-reserved-names` | #8 | ⚠️ **修好目标却弄坏别处** | F2P 成立但**回归门拦住** → 建议不合并 |
| 5 | `fix/parse-size-no-space` | #7 | ✅ 完整修复 + 回归测试 | F2P 成立 + 回归门通过 → 建议合并 |

**PR 2（半吊子）** 最能体现这套夹具的「未被覆盖维度」价值：
它是唯一一个 **F2P 目标用例通过、但整体仍然错误** 的 PR。

```
reg.resolve("conn", host="db2")   # 目标用例：✅ 现在能拿到 db2 了
reg.resolve("conn", host="db2")   # 正文契约：❌ 参数相同却没复用缓存
```

Issue #3 的正文同时要求「参数不同即重建」与「参数相同仍复用」。
PR 2 只满足了第一句。只有当验证器**完整落实正文里的两条契约**时才能识破它——
用 `fix/resolve-opt-in-rebuild` 的改动去跑就会看到：
`test_resolve_caches_instance` 之外，参数相同的两次 `resolve` 返回了不同对象。

**PR 4（回归门）** 把 `sanitize_filename` 修对了（目标用例通过），
却在同一提交里把 `format_size` 的小数位从两位改成一位，
让 `test_units.py` 的三个既有测试从绿变红。若只看 F2P 会误判为「可合并」，
只有跑了**既有测试回归门**才拦得住。

**PR 3（文档冒充）** 只在 README 里宣称「已支持 `load_toml` / `load_yaml`」并指向
一个并不存在的 `envkit/files.py`。它检验 Fissue 会不会被文本说服。

### 其余 Issue 的作用

| Issue | 作用 |
|---|---|
| #10 从配置文件装载 | FEATURE → 检验「只打标签、不生成验证器」 |
| #11 类型口径不统一 | 难度高、需设计决策（会破坏现有 API）→ 检验「不该自动修的要拦住」 |
| #12 缓存偶发残留 | 与 #2 **同源但证据独立**（无最小复现）→ 检验判重**不误标** |
| #13 get_bool 应抛错 | **设计偏好变更**：与库现有契约冲突 → 真实性应判低，不应改代码 |
| #14 性能+推广 | **刷量**：无复现、模板化、夹带联系方式 → 真实性应判低 |
| #15 说不清哪里错 | **无效**：无最小复现 → 真实性应判低 |

> #13 是刻意与 #5 配对的反例：#5 是「实现违反自己的文档」（该改），
> #13 是「用户想改掉文档规定的行为」（不该按它的方案改）。
> 两者看起来都是「改 `get_bool`」，但只有 #5 值得动代码。

## 一、推送到 GitHub

GitHub 的写操作（建仓库 / 推分支 / 建 Issue-PR）由你自己执行：

```bash
# 1) 准备 token（勾 repo 权限）
#    https://github.com/settings/tokens  →  Generate new token (classic)  →  勾 repo
export GITHUB_TOKEN=ghp_xxxx

# 2) 先干跑看看计划（不写任何东西）
.venv/Scripts/python.exe demo/envkit/scripts/setup_github.py --owner John-is-playing --dry-run

# 3) 真跑
.venv/Scripts/python.exe demo/envkit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`（本机有 HTTPS 中间人，
> 全局 Python 缺 truststore 会 TLS 失败，必须用 .venv 里的解释器）。

脚本会依次完成：

1. 创建 `John-is-playing/envkit` 仓库（已存在则复用）
2. 推送 `main` 分支
3. 从 `fixtures/*.md` 创建 15 个 Issue（带标签）
4. 推送 5 个修复分支并创建 5 个 PR（正文里带 `Closes #N`）

脚本是**可重入**的：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过，失败可放心重跑。

常用参数：

```bash
--name envkit-demo    # 换个仓库名
--private             # 建私有仓库（注意：Fissue 需要 token 才能读私有库）
--no-prs              # 只建 Issue，先看看评测效果
```

### 手动推送（方式 B）

```bash
cd demo/envkit/repo
git remote add origin https://github.com/John-is-playing/envkit.git
git push -u origin main
git push origin fix/snapshot-readonly fix/resolve-opt-in-rebuild \
                fix/docs-config-files fix/sanitize-reserved-names \
                fix/parse-size-no-space
```

然后在网页上按 `fixtures/*.md` 头部的 `<!-- ... -->`（title / labels）逐个建 Issue，
再为五个分支各建一个 PR（base 选 `main`）。

## 二、用 Fissue 测它

把仓库登记进 `config.yaml` 的 `repos:`：

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: envkit
    base_branch: main
    collect: [issue, pr]
    since_days: 0        # 0 = 不限制时间窗口，确保 15 个 Issue 全抓到
    test_hint: "python -m pytest -q"    # 明确告诉它怎么跑测试
```

然后跑完整链路：

```bash
# 1) 抓取（应拉到 15 个 Issue + 5 个 PR）
fissue fetch --repo John-is-playing/envkit --limit 80

# 2) 评测（四维评分 + 分类 + 反刷子）
fissue eval --repo John-is-playing/envkit --all

# 3) 看结果：#1/#3/#5/#7 应为 tier1，#2/#4/#6/#8/#9 应为 tier2
fissue report --repo John-is-playing/envkit

# 4) 生成验证器并做 F2P 验证
fissue verify --repo John-is-playing/envkit

# 5) 批量定论 + 打标签
fissue flush

# 6) 试跑自动修复（只出补丁，不提 PR）
fissue fix --repo John-is-playing/envkit --dry-run --limit 9
```

## 三、期望结果对照表

| # | 条目 | 维度 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|---|
| 1 | snapshot 未隔离 | 状态 | bug | **tier1** | base 失败（两步复现） | 自动修复 → 提 PR |
| 2 | cache 忽略 TTL | 状态 | bug | **tier2** | base 失败（需假时钟） | 自动修复 → 提 PR |
| 3 | resolve 参数失效 | 状态 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 4 | load_settings 污染入参 | 状态 | bug | **tier2** | base 失败（嵌套） | 自动修复 → 提 PR |
| 5 | get_bool 大小写 | 环境 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 6 | parse_offset 丢负号 | 环境 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 7 | parse_size 无空格 | 校验 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 8 | sanitize 保留名 | 校验 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 9 | split_iso 范围 | 校验 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 10 | 文件装载配置 | — | **feature** | none | 不验证 | 只打标签 |
| 11 | 类型口径不统一 | — | bug | none | 难自动化（需决策） | 不修复，打标签 |
| 12 | 缓存偶发残留 | 状态 | bug | none | — | **同源但非重复**（不误标） |
| 13 | get_bool 应抛错 | 环境 | **低真实性** | none | — | 设计偏好变更，不按它改 |
| 14 | 性能+推广 | — | **低真实性** | none | — | 判为刷量 |
| 15 | 说不清哪里错 | — | **低真实性** | none | — | 判为无效 |
| PR1 | snapshot 修复 | 状态 | bug | none | **F2P 成立 + 回归通过** | 建议合并 |
| PR2 | resolve「修复」 | 状态 | bug | none | **目标通过但违反正文契约** | 建议不合并 ⚠️ |
| PR3 | 文件装载文档 | feature | none | — | 无代码变更 | 建议不合并 |
| PR4 | sanitize 修复 | 校验 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR5 | parse_size 修复 | 校验 | bug | none | **F2P 成立 + 回归通过** | 建议合并 |

四行最关键：

- **PR2**：如果被判「可合并」，说明验证器没有完整落实 Issue #3 正文里的**两条**契约
  （只验了「参数不同要重建」，漏了「参数相同要复用」）——
  检查验证器是否覆盖了正文里 `assert c is b` 那条断言。
- **PR4**：如果被判「可合并」，说明回归门没生效——检查
  `verifier.regression_gate` 是否为 `strict`（`warn` 只告警不拦）。
- **PR3**：如果被判「可合并」，说明评测被文档文本说服，
  应当确认「feature + 无代码变更」会走不合并分支。
- **#13**：如果被当成真缺陷并生成补丁，说明「设计偏好变更」没有被规则拦住——
  它与 #5 只差在「是谁的契约被违反」。

## 四、本地自检（不连网）

推之前可以先在本地确认夹具是自洽的：

```bash
cd demo/envkit/repo
.venv/Scripts/python.exe -m pytest -q          # 基线应 73 passed

# 确认 9 处缺陷确实存在
.venv/Scripts/python.exe - <<'PY'
import sys; sys.path.insert(0, '.')
from envkit import (Registry, TTLCache, load_settings, get_bool,
                    parse_offset, split_iso, parse_size, sanitize_filename)

class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now

# 1) snapshot 污染注册表
reg = Registry(); reg.register("o", lambda: {"n": 1}); reg.resolve("o")
snap = reg.snapshot(); snap["evil"] = 1
print("1) snapshot 被污染 ->", "evil" in reg.snapshot())        # True（缺陷）

# 2) 过期数据仍可读到
clk = Clock(); cache = TTLCache(ttl=10, clock=clk)
cache.set("k", "v"); clk.now = 11
print("2) 过期后仍 get 到 ->", cache.get("k"))                  # 'v'（缺陷）

# 3) 参数变化不重建
reg2 = Registry(); reg2.register("c", lambda host="h": {"host": host})
print("3) 参数变化仍复用 ->", reg2.resolve("c", host="db2"))    # {'host': 'h'}（缺陷）

# 4) 污染入参
base = {"db": {"host": "localhost"}}
load_settings(base, env={})["db"]["host"] = "prod"
print("4) 入参被污染 ->", base["db"]["host"])                   # 'prod'（缺陷）

# 5) 大小写敏感
print("5) TRUE 未识别 ->", get_bool("F", env={"F": "TRUE"}))    # False（缺陷）

# 6) 丢负号
print("6) 负偏移丢号 ->", parse_offset("-05:30"))               # 330（缺陷）

# 7) 无空格写法报错
try:
    parse_size("512KB"); print("7) 512KB 正常")
except ValueError as e:
    print("7) 512KB 抛错 ->", e)                                # ValueError（缺陷）

# 8) 保留名未处理
print("8) CON 原样放行 ->", sanitize_filename("CON"))           # 'CON'（缺陷）

# 9) 时间字段越界被接受
print("9) 08:75:00 被接受 ->", split_iso("2024-03-05T08:75:00+08:00")["minute"])  # 75（缺陷）
PY
```

预期 9 行全部打出「缺陷」侧的值。

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | token 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用（脚本会自动跳过） |
| `envkit 工作区不干净` | repo 有未提交改动 | `cd demo/envkit/repo && git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异，或 PR 已存在 | 脚本会跳过已存在的；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe`（含 truststore） |

> **为什么强调换行符**：Fissue 验证 PR 时用 `git apply` 打平台返回的 diff。
> 如果仓库里混入 CRLF，diff 的上下文行与实际文件字节不一致，补丁会应用失败。
> 所以 `demo/envkit/repo/.gitattributes` 里固定了 `* text=auto eol=lf`。
