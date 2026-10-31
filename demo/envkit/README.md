# 夹具：envkit

第四套独立夹具。前三套的缺陷都是「给定输入 → 算出错误结果」，只读代码跑一次就能定位；
`envkit` 把缺陷挪到三个更难的位置：

> **当缺陷取决于调用顺序与历史状态、只在特定环境下成立、或表现为「本该拦住的输入没拦住」时，
> Fissue 还能不能构造出稳定的最小复现并给出正确定论？**

15 个 Issue + 5 个 PR，混入错误、无效、同源但不重复、刷量的请求。

## 三个维度

| 维度 | 代表 | 难点 |
|---|---|---|
| 状态与副作用 | `Registry` / `TTLCache` | 缺陷要**按特定调用顺序**才复现；`snapshot()` 返回内部字典必须「取快照 → 改快照 → 再看注册表」；`TTLCache` 需注入**可控时钟**才能稳定复现过期。考的是会不会构造**确定性**复现装置（假时钟），而非依赖真实时间 |
| 环境依赖 | `get_bool` / `load_settings` / `parse_offset` | `"TRUE"` 在大小写敏感实现下静默失效；`load_settings` 只在**嵌套默认值**上暴露污染；`parse_offset` 只在西半球时区出错 |
| 输入校验 | `parse_size` / `sanitize_filename` / `split_iso` | 缺陷是「**违反自身文档契约**」：文档说该支持的没支持、该拦的没拦。同时放入 #13（建议 `get_bool` 遇未知值改抛错）这种**与现行为冲突的偏好变更**，考「不该报的别误判成 bug」 |

最具代表性两项：**#1** 必须有「改快照 → 验注册表」两步验证器才抓得住（只断言 `isinstance`
会漏）；**PR2** 修好了 #3 的核心用例（参数变→重建），却弄坏了正文同时声明的另一条契约
（参数相同应复用），只看目标用例的验证器会判它可合并。

## 目录结构

```
demo/envkit/
├── repo/                    待推送仓库（独立 git 仓库）
│   ├── envkit/              库源码（9 处植入缺陷，跨 7 个模块）
│   ├── tests/               73 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                15 个 Issue（issue-01 … issue-15）
└── scripts/setup_github.py  一键建仓库 / Issue / PR
```

## 设计意图

### 9 处真实缺陷（基线测试刻意未覆盖）

| # | 位置 | 维度 | 表现 | 期望 |
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

**73 个基线测试全绿**，每处缺陷都藏在未覆盖的边界上，且都**违反自身文档契约**（而非「另一种合理
口径」）——这样验证器才有唯一正确答案。

#2 与 #12 是刻意的一对：`cache.get` 忽略过期是**真缺陷**（有最小复现），#12 听起来像同一问题但
**给不出最小复现**、指向并发而非 TTL——检验判重**从严、不误标**。

### 五个 PR

| PR | 分支 | 关联 | 性质 | 期望 |
|---|---|---|---|---|
| 1 | `fix/snapshot-readonly` | #1 | ✅ 完整修复 + 回归测试 | F2P + 回归门通过 → 建议合并 |
| 2 | `fix/resolve-opt-in-rebuild` | #3 | ⚠️ **修好目标却违反正文另一条契约** | 建议不合并 |
| 3 | `fix/docs-config-files` | #10 | ❌ **只改文档，冒充功能实现** | FEATURE + 无代码 → 不合并 |
| 4 | `fix/sanitize-reserved-names` | #8 | ⚠️ **修好目标却弄坏别处** | F2P 成立但**回归门拦住** → 不合并 |
| 5 | `fix/parse-size-no-space` | #7 | ✅ 完整修复 + 回归测试 | F2P + 回归门通过 → 建议合并 |

```
reg.resolve("conn", host="db2")   # 目标用例：✅ 拿到 db2
reg.resolve("conn", host="db2")   # 正文契约：❌ 参数相同却没复用缓存
```

Issue #3 正文同时要求「参数不同即重建」与「参数相同仍复用」，PR2 只满足第一句——只有验证器
**完整落实两条契约**才能识破它。

**PR4（回归门）**：`sanitize_filename` 修对了，却在同一提交把 `format_size` 小数位从两位改一位，
让 `test_units.py` 三个既有测试由绿变红。**PR3（文档冒充）**：只在 README 宣称「已支持
`load_toml` / `load_yaml`」并指向不存在的 `envkit/files.py`。

### 其余 Issue

| Issue | 作用 |
|---|---|
| #10 从配置文件装载 | FEATURE → 只打标签、不生成验证器 |
| #11 类型口径不统一 | 难度高、需设计决策（会破坏现有 API）→ 不该自动修 |
| #12 缓存偶发残留 | 与 #2 同源但证据独立（无最小复现）→ 判重不误标 |
| #13 get_bool 应抛错 | **设计偏好变更**：与库现有契约冲突 → 真实性应判低 |
| #14 性能 + 推广 | **刷量** → 真实性应判低 |
| #15 说不清哪里错 | **无效**（无最小复现）→ 真实性应判低 |

> #13 与 #5 刻意配对：#5 是「实现违反自己的文档」（该改），#13 是「用户想改掉文档规定的行为」
> （不该按它的方案改）。两者看起来都是「改 `get_bool`」，只有 #5 值得动代码。

## 一、推送到 GitHub（写操作请自行执行）

```bash
export GITHUB_TOKEN=ghp_xxxx
.venv/Scripts/python.exe demo/envkit/scripts/setup_github.py --owner John-is-playing --dry-run
.venv/Scripts/python.exe demo/envkit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`（全局 Python 缺 truststore，会 TLS 失败）。

脚本依次：建仓库（已存在则复用）→ 推 `main` → 建 15 个 Issue → 推 5 个修复分支并建 5 个 PR。
**可重入**：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过。参数：`--name`、`--private`、`--no-prs`。

手动方式：`cd demo/envkit/repo && git push -u origin main`，再推
`fix/snapshot-readonly fix/resolve-opt-in-rebuild fix/docs-config-files
fix/sanitize-reserved-names fix/parse-size-no-space`。

## 二、用 Fissue 测它

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: envkit
    base_branch: main
    collect: [issue, pr]
    since_days: 0                       # 确保 15 个 Issue 全抓到
    test_hint: "python -m pytest -q"
```

```bash
fissue fetch  --repo John-is-playing/envkit --limit 80      # 应拉到 15 Issue + 5 PR
fissue eval   --repo John-is-playing/envkit --all
fissue report --repo John-is-playing/envkit
fissue verify --repo John-is-playing/envkit
fissue flush
fissue fix    --repo John-is-playing/envkit --dry-run --limit 9
```

## 三、期望结果对照表

| # | 条目 | 维度 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|---|
| 1 | snapshot 未隔离 | 状态 | bug | **tier1** | base 失败（两步复现） | 自动修复 |
| 2 | cache 忽略 TTL | 状态 | bug | **tier2** | base 失败（需假时钟） | 自动修复 |
| 3 | resolve 参数失效 | 状态 | bug | **tier1** | base 失败 | 自动修复 |
| 4 | load_settings 污染入参 | 状态 | bug | **tier2** | base 失败（嵌套） | 自动修复 |
| 5 | get_bool 大小写 | 环境 | bug | **tier1** | base 失败 | 自动修复 |
| 6 | parse_offset 丢负号 | 环境 | bug | **tier2** | base 失败 | 自动修复 |
| 7 | parse_size 无空格 | 校验 | bug | **tier1** | base 失败 | 自动修复 |
| 8 | sanitize 保留名 | 校验 | bug | **tier2** | base 失败 | 自动修复 |
| 9 | split_iso 范围 | 校验 | bug | **tier2** | base 失败 | 自动修复 |
| 10 | 文件装载配置 | — | **feature** | none | 不验证 | 只打标签 |
| 11 | 类型口径不统一 | — | bug | none | 需决策 | 不修复 |
| 12 | 缓存偶发残留 | 状态 | bug | none | — | **同源但非重复** |
| 13 | get_bool 应抛错 | 环境 | **低真实性** | none | — | 设计偏好变更 |
| 14 | 性能 + 推广 | — | **低真实性** | none | — | 判为刷量 |
| 15 | 说不清哪里错 | — | **低真实性** | none | — | 判为无效 |
| PR1 | snapshot 修复 | 状态 | bug | none | **F2P + 回归通过** | 建议合并 |
| PR2 | resolve「修复」 | 状态 | bug | none | **目标通过但违反正文契约** | 建议不合并 ⚠️ |
| PR3 | 文件装载文档 | feature | none | — | 无代码变更 | 建议不合并 |
| PR4 | sanitize 修复 | 校验 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR5 | parse_size 修复 | 校验 | bug | none | **F2P + 回归通过** | 建议合并 |

四行最关键：**PR2** 若可合并 → 验证器漏了正文里的「参数相同应复用」契约；**PR4** 若可合并 →
回归门没生效（查 `verifier.regression_gate` 是否 `strict`）；**PR3** 若可合并 → 被文档文本说服；
**#13** 若生成了补丁 → 「设计偏好变更」没被规则拦住（它与 #5 只差「谁的契约被违反」）。

## 四、本地自检（不连网）

```bash
cd demo/envkit/repo && .venv/Scripts/python.exe -m pytest -q      # 基线应 73 passed
.venv/Scripts/python.exe - <<'PY'
import sys; sys.path.insert(0, '.')
from envkit import (Registry, TTLCache, load_settings, get_bool,
                    parse_offset, split_iso, parse_size, sanitize_filename)

class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now

reg = Registry(); reg.register("o", lambda: {"n": 1}); reg.resolve("o")
snap = reg.snapshot(); snap["evil"] = 1
print("1) snapshot 被污染 ->", "evil" in reg.snapshot())        # True
clk = Clock(); cache = TTLCache(ttl=10, clock=clk)
cache.set("k", "v"); clk.now = 11
print("2) 过期后仍 get 到 ->", cache.get("k"))                  # 'v'
reg2 = Registry(); reg2.register("c", lambda host="h": {"host": host})
print("3) 参数变化仍复用 ->", reg2.resolve("c", host="db2"))    # {'host': 'h'}
base = {"db": {"host": "localhost"}}
load_settings(base, env={})["db"]["host"] = "prod"
print("4) 入参被污染 ->", base["db"]["host"])                   # 'prod'
print("5) TRUE 未识别 ->", get_bool("F", env={"F": "TRUE"}))    # False
print("6) 负偏移丢号 ->", parse_offset("-05:30"))               # 330
try:
    parse_size("512KB"); print("7) 512KB 正常")
except ValueError as e:
    print("7) 512KB 抛错 ->", e)
print("8) CON 原样放行 ->", sanitize_filename("CON"))           # 'CON'
print("9) 08:75:00 被接受 ->", split_iso("2024-03-05T08:75:00+08:00")["minute"])  # 75
PY
```

预期 9 行全部打出「缺陷」侧的值。

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用 |
| `envkit 工作区不干净` | repo 有未提交改动 | `git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异 / PR 已存在 | 脚本会跳过；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe` |

> 换行符：验证 PR 时用 `git apply` 打平台 diff，CRLF 会导致补丁应用失败。`repo/.gitattributes`
> 固定了 `* text=auto eol=lf`。

---

相关：[夹具总览](../README.md) · [BENCHMARK.md](../../BENCHMARK.md) · [缺陷与修复记录](../../30ae40b-BUG.md)