# 夹具：lockkit

第五套独立夹具。前四套的缺陷基本「跑一次就看见」，`lockkit` 把缺陷挪到三个更硬的位置：

> **当缺陷表现为「资源不变量被悄悄破坏」、「本该封顶的等待无限增长」、「本该只成功一次的语义
> 变成缓存失败」时，Fissue 还能不能构造出稳定最小复现并给出正确定论？**

15 个 Issue + 5 个 PR，混入错误、无效、同源但不重复、刷量的请求。

## 三个维度

| 维度 | 代表 | 难点 |
|---|---|---|
| 资源不变量 | `Semaphore` / `ResourcePool` | 缺陷破坏的是**不变量本身**而非某个返回值：`release()` 多还几次就能凭空造出许可；`borrow()` 在异常路径不归还。正常路径完全看不出来 |
| 超时与上限 | `RetryPolicy` / `TokenBucket` | `max_delay` 是**封顶**语义，失效后等待指数爆炸；`allow(n)` 的边界是「恰好取尽」，`peek()` 的语义是「按流逝时间补充」 |
| 一次性语义 | `Once` / `Latch` | `Once` 契约是「最多**成功**执行一次」，缺陷把「执行过」当成「成功过」；`Latch` 的打开条件是**累计**落下次数跨阈值，缺陷只在**一次跨过**时错乱 |

最具代表性两项：**#1** 验证器必须**多还一次**才能抓住（只做「借一次还一次」配对永远通过）；
**PR2** 修好了 #6 的目标用例（失败后可重试），却把 `calls` 一起清零，破坏了「失败也是一次真实
执行」的语义，只看目标用例的验证器会判它可合并。

## 目录结构

```
demo/lockkit/
├── repo/                    待推送仓库（独立 git 仓库）
│   ├── lockkit/             库源码（9 处植入缺陷，跨 6 个模块）
│   ├── tests/               87 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                15 个 Issue（issue-01 … issue-15）
└── scripts/setup_github.py  一键建仓库 / Issue / PR
```

## 设计意图

### 9 处真实缺陷（基线测试刻意未覆盖）

| # | 位置 | 维度 | 表现 | 期望 |
|---|---|---|---|---|
| 1 | `pool.Semaphore.release` | 不变量 | 归还直接自增，`available` 超过 `permits` | tier1 → 自动修复 |
| 2 | `pool.ResourcePool.borrow` | 不变量 | 无 `try/finally`，异常时资源永久泄漏 | tier2 → 自动修复 |
| 3 | `retry.RetryPolicy.delay_for` | 上限 | 不读 `max_delay`，退避无限翻倍 | tier1 → 自动修复 |
| 4 | `retry.is_retryable` | 上限 | `type(exc) in …` 精确匹配，子类不重试 | tier2 → 自动修复 |
| 5 | `limiter.TokenBucket.allow` | 边界 | `>` 应为 `>=`，恰好取尽被判失败 | tier1 → 自动修复 |
| 6 | `gate.Once.call` | 一次性 | 首次失败也置 `_done`，后续直接返回 `None` | tier2 → 自动修复 |
| 7 | `gate.Latch.open` | 一次性 | `== 0` 与 `max(0,…)` 口径不一致，跨阈值错乱 | tier2 → 自动修复 |
| 8 | `limiter.TokenBucket.peek` | 时间 | 少了 `_refill()`，读数与实际可取量矛盾 | tier1 → 自动修复 |
| 9 | `metrics.Meter.peak` | 口径 | 空样本 `max([])` 抛错，与 `mean`/`valley` 不一致 | tier2 → 自动修复 |

**87 个基线测试全绿**，每处缺陷都藏在未覆盖的边界上，且都**违反自身文档契约**（不变量 / 上限 /
空样本口径），而非「另一种合理口径」——验证器才有唯一正确答案。

**为什么这套对验证器生成特别硬**：本库所有与时间有关的行为都接受 `clock` / `sleep` 注入点，缺陷
**能被确定性复现**，不需要真线程、真 sleep。Fissue 必须自己发现「注入 `ManualClock` 推进时间」这
条路径，而不是写 `time.sleep(1)` 碰运气——#8 与 #3 的验证器都要过这道坎。

几对刻意设计的「同域不同因」：#1 与 #2 是同一种守恒不变量在两个组件上的「多还」与「少还」；
#5 与 #8 都在 `TokenBucket` 但一个无关时间、一个与时间有关；#3 与 #4 都在 `retry.py`，一个是
**等待时长**、一个是**是否重试**，改错文件就会回归。

### 五个 PR

| PR | 分支 | 关联 | 性质 | 期望 |
|---|---|---|---|---|
| 1 | `fix/semaphore-release-cap` | #1 | ✅ 完整修复 + 回归测试 | F2P + 回归门通过 → 建议合并 |
| 2 | `fix/once-retry-after-failure` | #6 | ⚠️ **修好目标却破坏计数语义** | 建议不合并 |
| 3 | `fix/docs-lockset` | #10 | ❌ **只改文档，冒充功能实现** | FEATURE + 无代码 → 不合并 |
| 4 | `fix/retry-cap-and-subclass` | #3 | ⚠️ **修好目标却弄坏别处** | F2P 成立但**回归门拦住** → 不合并 |
| 5 | `fix/tokenbucket-exact-drain` | #5 | ✅ 完整修复 + 回归测试 | F2P + 回归门通过 → 建议合并 |

```
once = Once()
try: once.call(init)            # 首次失败
except ValueError: pass
once.done                       # 目标用例：✅ False，能重试
once.calls                      # 正文契约：❌ 失败路径把计数一起清零了
```

Issue #6 正文同时要求「失败不缓存、后续重新执行」（目标用例）与「`calls` 是实际执行次数，失败也
计入」。PR2 用「清空状态」满足第一句，却把 `_calls` 一起归零——只有验证器落实**两条**契约才能识破。

**PR4（回归门）**：`delay_for` 修对了，却顺手「整理」了 `should_retry`：剥掉 `attempt >=
self.attempts` 的次数判断，只留异常类型判定。听起来是职责收敛，但 `should_retry` 是对外契约：

```text
FAILED tests/test_retry.py::test_should_retry_exhausted_attempts
E   assert True is False
1 failed, 86 passed
```

`retry_call` 自己用 `attempt >= active.attempts` 判断、并未走 `should_retry`，所以功能上「看着
没事」——只看 F2P 会误判可合并。

> **构造期记录**：PR4 最初设计为「顺手把 `Latch.open` 改成 `<= 0`」，但实测该改动**不会**让任何
> 测试变红（87 passed）——它恰恰是 Issue #7 的正确修复。已改用上面这个真正破坏契约的改动。
> 见 [BENCHMARK.md](../../BENCHMARK.md)。

**PR3（文档冒充）**：只在 README 宣称「已支持 `LockSet`」并指向不存在的 `lockkit/lockset.py`。

### 其余 Issue

| Issue | 作用 |
|---|---|
| #10 有序加锁集 | FEATURE → 只打标签、不生成验证器 |
| #11 可用数口径统一 | 难度高、需设计决策（含破坏性变更）→ 不该自动修 |
| #12 并发下放行超额 | 与 #8 **同源但证据独立**（多线程，无最小复现）→ 判重不误标 |
| #13 空 peak 应抛错 | **设计偏好变更**：与库现有契约相反 → 真实性应判低 |
| #14 性能 + 推广 | **刷量** → 真实性应判低 |
| #15 说不清哪里错 | **无效**（无最小复现）→ 真实性应判低 |

> #13 与 #9 刻意配对：#9 是「实现漏了判空、违反自己的文档」（该改），#13 是「用户想改掉文档规定
> 的行为」（不该按它的方案改）。#12 与 #8 同理：#8 单线程纯读取、有确定复现，#12 多线程、给不出
> 复现——两者都在说「令牌数不对」，用来检验判重**从严、不误标**。

## 一、推送到 GitHub（写操作请自行执行）

```bash
export GITHUB_TOKEN=ghp_xxxx
.venv/Scripts/python.exe demo/lockkit/scripts/setup_github.py --owner John-is-playing --dry-run
.venv/Scripts/python.exe demo/lockkit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`（全局 Python 缺 truststore，会 TLS 失败）。

脚本依次：建仓库（已存在则复用）→ 推 `main` → 建 15 个 Issue → 推 5 个修复分支并建 5 个 PR。
**可重入**：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过。参数：`--name`、`--private`、`--no-prs`。

手动方式：`cd demo/lockkit/repo && git push -u origin main`，再推
`fix/semaphore-release-cap fix/once-retry-after-failure fix/docs-lockset
fix/retry-cap-and-subclass fix/tokenbucket-exact-drain`。

## 二、用 Fissue 测它

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: lockkit
    base_branch: main
    collect: [issue, pr]
    since_days: 0                       # 确保 15 个 Issue 全抓到
    test_hint: "python -m pytest -q"
```

```bash
fissue fetch  --repo John-is-playing/lockkit --limit 80     # 应拉到 15 Issue + 5 PR
fissue eval   --repo John-is-playing/lockkit --all
fissue report --repo John-is-playing/lockkit
fissue verify --repo John-is-playing/lockkit
fissue flush
fissue fix    --repo John-is-playing/lockkit --dry-run --limit 9
```

## 三、期望结果对照表

| # | 条目 | 维度 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|---|
| 1 | release 无限归还 | 不变量 | bug | **tier1** | base 失败（需多还一次） | 自动修复 |
| 2 | borrow 异常泄漏 | 不变量 | bug | **tier2** | base 失败（异常路径） | 自动修复 |
| 3 | 退避不封顶 | 上限 | bug | **tier1** | base 失败（需够多尝试次数） | 自动修复 |
| 4 | 子类不重试 | 上限 | bug | **tier2** | base 失败 | 自动修复 |
| 5 | 恰好取尽被拒 | 边界 | bug | **tier1** | base 失败 | 自动修复 |
| 6 | 失败被缓存 | 一次性 | bug | **tier2** | base 失败 | 自动修复 |
| 7 | 跨阈值不打开 | 一次性 | bug | **tier2** | base 失败 | 自动修复 |
| 8 | peek 读数陈旧 | 时间 | bug | **tier1** | base 失败（需假时钟） | 自动修复 |
| 9 | 空 peak 抛错 | 口径 | bug | **tier2** | base 失败 | 自动修复 |
| 10 | 有序加锁集 | — | **feature** | none | 不验证 | 只打标签 |
| 11 | 可用数口径统一 | — | bug | none | 需决策 | 不修复 |
| 12 | 并发放行超额 | 时间 | bug | none | — | **同源但非重复** |
| 13 | 空 peak 应抛错 | 口径 | **低真实性** | none | — | 设计偏好变更 |
| 14 | 性能 + 推广 | — | **低真实性** | none | — | 判为刷量 |
| 15 | 说不清哪里错 | — | **低真实性** | none | — | 判为无效 |
| PR1 | Semaphore 修复 | 不变量 | bug | none | **F2P + 回归通过** | 建议合并 |
| PR2 | Once「修复」 | 一次性 | bug | none | **目标通过但破坏 calls 语义** | 建议不合并 ⚠️ |
| PR3 | LockSet 文档 | feature | none | — | 无代码变更 | 建议不合并 |
| PR4 | delay_for 修复 | 上限 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR5 | allow 修复 | 边界 | bug | none | **F2P + 回归通过** | 建议合并 |

四行最关键：**PR2** 若可合并 → 验证器漏了正文对 `calls` 的断言；**PR4** 若可合并 → 回归门没生效
（查 `verifier.regression_gate` 是否 `strict`）；**PR3** 若可合并 → 被文档文本说服；**#13** 若生成
补丁 → 「设计偏好变更」没被规则拦住（与 #9 只差「谁的契约被违反」）。

两个**验证器质量**观察点：**#8** 若写成 `time.sleep(1)` 再读 `peek()`，沙盒里不稳定，正确做法是
注入 `ManualClock` 并 `tick`；**#1** 若只做「借一次还一次」会**通过**（当前实现也正确），必须
**多还一次**才能让 base 失败。

## 四、本地自检（不连网）

```bash
cd demo/lockkit/repo && .venv/Scripts/python.exe -m pytest -q      # 基线应 87 passed
.venv/Scripts/python.exe - <<'PY'
import sys; sys.path.insert(0, '.')
from lockkit import (ManualClock, TokenBucket, Semaphore, ResourcePool,
                     RetryPolicy, is_retryable, Once, Latch, Meter)

s = Semaphore(1); s.acquire(); s.release(); s.release()
print("1) available 超过 permits ->", s.available)          # 2
pool = ResourcePool(1, lambda: object())
try:
    with pool.borrow():
        raise RuntimeError("boom")
except RuntimeError:
    pass
print("2) 异常后可用 ->", pool.available)                    # 0
p = RetryPolicy(attempts=8, base_delay=1.0, factor=2.0, max_delay=5.0)
print("3) delay_for(5) ->", p.delay_for(5))                 # 16.0
print("4) TimeoutError 可重试 ->", is_retryable(TimeoutError("x"), (OSError,)))   # False
b = TokenBucket(rate=10, capacity=3)
print("5) allow(3) ->", b.allow(3))                         # False
o = Once()
try: o.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
except ValueError: pass
print("6) 失败后 done ->", o.done)                           # True
l = Latch(2); l.count_down(5)
print("7) open ->", l.open)                                 # False
clk = ManualClock(); b2 = TokenBucket(rate=10, capacity=3, clock=clk)
b2.allow(2); clk.tick(1.0)
print("8) 补充 1 秒后 peek ->", b2.peek())                   # 1.0
try:
    Meter().peak; print("9) 空 peak 正常")
except ValueError as e:
    print("9) 空 peak 抛错 ->", e)
PY
```

预期 9 行全部打出「缺陷」侧的值。

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用 |
| `lockkit 工作区不干净` | repo 有未提交改动 | `git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异 / PR 已存在 | 脚本会跳过；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe` |
| 基线测试数对不上（≠87） | 收集到 `__pycache__` 或环境同名包 | 在 `demo/lockkit/repo` 下跑，确认无全局 `lockkit` 干扰 |

> 换行符：验证 PR 时用 `git apply` 打平台 diff，CRLF 会导致补丁应用失败。`repo/.gitattributes`
> 固定了 `* text=auto eol=lf`。

---

相关：[夹具总览](../README.md) · [BENCHMARK.md](../../BENCHMARK.md) · [TRIAGE.md](../../TRIAGE.md)