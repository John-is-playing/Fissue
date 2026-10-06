# Fissue 测试夹具（五）：lockkit

一个**全新的、纯净的**测试仓库夹具，与前面四套完全独立：

| 夹具 | 领域 | 形态 | 考什么 |
|---|---|---|---|
| `textkit` | 文本处理 | 单模块、独立函数 | 单函数语义 |
| `ratekit` | 金额 / 费率 | 单模块、数值边界 | 数值边界 |
| `pipekit` | 分片流水线 / 区间 | 多模块、调用链耦合 | 跨模块根因定位 |
| `envkit` | 配置 / 缓存 / 校验 | 有状态对象 + 环境依赖 | 状态副作用、环境依赖、输入校验 |
| **`lockkit`** | **并发原语 / 资源管理** | **不变量约束 + 时间注入** | **资源不变量、超时上限、一次性语义** |

它一次覆盖 **15 个 Issue + 5 个 PR**，其中刻意混入错误、无效、同源但不重复、刷量的请求。

## 这套夹具回答什么

前四套的缺陷基本都能「跑一次就看见」。`lockkit` 把缺陷挪到三个更硬的位置：

> **当缺陷表现为「资源不变量被悄悄破坏」、「本该封顶的等待无限增长」、
> 「本该只成功一次的语义变成了缓存失败」时，
> Fissue 还能不能构造出稳定的最小复现，并给出正确的定论？**

三个维度各有一组代表：

- **资源不变量**（`Semaphore` / `ResourcePool`）：
  许可总数、资源「借出必还」都是**不变量**，缺陷破坏的是不变量本身，
  而不是某个返回值。`release()` 多还几次就能凭空造出许可；
  `borrow()` 在异常路径上不归还——两者在正常路径上完全看不出来。

- **超时与上限**（`RetryPolicy` / `TokenBucket`）：
  `max_delay` 是**封顶**语义，缺陷让它失效后等待时间指数爆炸；
  `allow(n)` 的边界是「恰好取尽」，`peek()` 的语义是「按流逝时间补充」。
  这一类考的是 Fissue 能不能读出**边界与上限**，而不是只测一个好走的路径。

- **一次性语义**（`Once` / `Latch`）：
  `Once` 的契约是「最多**成功**执行一次」，缺陷把「执行过」当成了「成功过」；
  `Latch` 的「打开」条件是**累计**落下次数跨过阈值，缺陷只在**一次跨过**时错乱。
  这一类考的是「语义里的那个关键词（成功 / 累计）有没有被验证器落实」。

最具代表性的两项：

- **Issue #1**（`Semaphore.release` 无限归还）：验证器必须**多还一次**才能抓住；
  只做「借一次还一次」配对的验证器永远通过。
- **PR 2（半吊子）**：把 `Once` 的失败支路改成 `_done = False`，**修好了目标用例**
  （失败后可重试、能拿到真实结果），却弄坏了 `calls` 的语义——
  它把「尝试次数」一起清零，于是 `once.calls` 变成 1 且再次重试时计数错乱。
  只看「失败后能重试」的验证器会判它「可合并」。

## 目录结构

```
demo/lockkit/
├── repo/                        待推送的测试仓库（独立 git 仓库）
│   ├── lockkit/                 库源码（9 处植入缺陷，跨 6 个模块）
│   ├── tests/                   87 个基线测试（全绿，刻意不覆盖那些缺陷）
│   └── README.md, pyproject.toml, .gitattributes
├── fixtures/                    15 个 Issue 的内容与元数据
│   ├── issue-01-semaphore-release-overflow.md      tier1：许可无限归还（不变量）→ 自动修复
│   ├── issue-02-pool-borrow-leaks-on-error.md      tier2：异常路径不归还（不变量）→ 自动修复
│   ├── issue-03-retry-ignores-max-delay.md         tier1：退避不封顶（上限）→ 自动修复
│   ├── issue-04-is-retryable-subclass.md           tier2：子类不重试（上限）→ 自动修复
│   ├── issue-05-tokenbucket-exact-drain.md         tier1：恰好取尽被拒（边界）→ 自动修复
│   ├── issue-06-once-caches-failure.md             tier2：缓存失败（一次性）→ 自动修复
│   ├── issue-07-latch-overshoot.md                 tier2：跨阈值不打开（一次性）→ 自动修复
│   ├── issue-08-tokenbucket-peek-stale.md          tier1：读数不补充（时间）→ 自动修复
│   ├── issue-09-meter-peak-empty.md                tier2：空样本抛错（口径）→ 自动修复
│   ├── issue-10-feature-lockset-ordering.md        FEATURE → 只打标签
│   ├── issue-11-available-semantics-decision.md    难度高（需决策）→ 不自动修复
│   ├── issue-12-concurrent-over-admission.md       **同源但证据独立** → 判重不误标
│   ├── issue-13-meter-peak-should-raise.md         **设计偏好变更（误报）** → 真实性应判低
│   ├── issue-14-spam-performance-promo.md          **刷量** → 真实性应判低
│   └── issue-15-vague-not-reproducible.md          **无效/无法复现** → 真实性应判低
└── scripts/setup_github.py      一键建仓库 / 建 Issue / 建 PR
```

## 设计意图：每个 fixture 考什么

### 植入的 9 处真实缺陷（基线测试刻意不覆盖）

| # | 位置 | 维度 | 表现 | 期望 Fissue 行为 |
|---|---|---|---|---|
| 1 | `pool.Semaphore.release` | 不变量 | 归还直接自增，`available` 超过 `permits` | tier1 → 自动修复 |
| 2 | `pool.ResourcePool.borrow` | 不变量 | 无 `try/finally`，异常时资源永久泄漏 | tier2 → 自动修复 |
| 3 | `retry.RetryPolicy.delay_for` | 上限 | 不读 `max_delay`，退避无限翻倍 | tier1 → 自动修复 |
| 4 | `retry.is_retryable` | 上限 | `type(exc) in …` 精确匹配，子类不重试 | tier2 → 自动修复 |
| 5 | `limiter.TokenBucket.allow` | 边界 | `>` 应为 `>=`，恰好取尽被判失败 | tier1 → 自动修复 |
| 6 | `gate.Once.call` | 一次性 | 首次失败也置 `_done`，后续直接返回 `None` | tier2 → 自动修复 |
| 7 | `gate.Latch.open` | 一次性 | `== 0` 与 `max(0, …)` 口径不一致，跨阈值错乱 | tier2 → 自动修复 |
| 8 | `limiter.TokenBucket.peek` | 时间 | 少了 `_refill()`，读数与实际可取量矛盾 | tier1 → 自动修复 |
| 9 | `metrics.Meter.peak` | 口径 | 空样本 `max([])` 抛错，与 `mean`/`valley` 不一致 | tier2 → 自动修复 |

> **关键**：基线 87 个测试是**全绿**的——每处缺陷都藏在没被覆盖的边界上。
> 这逼 Fissue 真正去「读代码 → 写验证器 → 跑 F2P」，而不是抄现成测试。

每处缺陷都**违反自身文档契约**（文档写了不变量 / 上限 / 空样本口径，实现没做到），
而不是「另一个合理口径」——这样验证器才有唯一正确答案。

**为什么这套夹具对验证器生成特别硬**：本库所有与时间有关的行为都接受
`clock` / `sleep` 注入点，所以缺陷**能被确定性复现**，不需要真是线程、真 sleep。
Fissue 必须自己发现「注入 `ManualClock` 推进时间」这条路径，而不是写
`time.sleep(1)` 碰运气——这也正是 #8 与 #3 的验证器要过的坎。

缺陷 #1 与 #2 是**同一种不变量**（「数量守恒」）在两个组件上的两次破坏：
一个是「多还」，一个是「少还」。它们不能靠复制另一处的写法来修。

缺陷 #5 与 #8 都落在 `TokenBucket`，但**语义维度不同**：
#5 是「比较符号」的边界（与时间无关），#8 是「读取前要不要补充」（与时间有关）。
两者必须分开验证，不能用一个验证器糊住。

缺陷 #3 与 #4 都在 `retry.py`，看起来都像「重试不生效」，
但 #3 是**等待时长**（`delay_for`），#4 是**是否重试**（`is_retryable`）——
一个改数值、一个改判定，改错文件就会回归。

### 五个 PR：真好×2 / 半吊子 / 文档冒充 / 回归门（样本失效，见下）

| PR | 分支 | 关联 | 性质 | 期望 Fissue 行为 |
|---|---|---|---|---|
| 1 | `fix/semaphore-release-cap` | #1 | ✅ 完整修复 + 回归测试 | F2P 成立 + 回归门通过 → 建议合并 |
| 2 | `fix/once-retry-after-failure` | #6 | ⚠️ **修好目标却破坏计数语义** | 建议不合并 |
| 3 | `fix/docs-lockset` | #10 | ❌ **只改文档，冒充功能实现** | FEATURE + 无代码 → 建议不合并 |
| 4 | `fix/retry-cap-and-subclass` | #3 | ⚠️ 意图「修好目标却弄坏别处」，但**样本失效**（见下） | 实测 F2P 成立 + 回归门通过 → 建议合并 |
| 5 | `fix/tokenbucket-exact-drain` | #5 | ✅ 完整修复 + 回归测试 | F2P 成立 + 回归门通过 → 建议合并 |

**PR 2（半吊子）** 最能体现这套夹具的价值：它是唯一一个
**F2P 目标用例通过、但整体仍然错误** 的 PR。

```
once = Once()
try: once.call(init)            # 首次失败
except ValueError: pass
once.done                       # 目标用例：✅ 现在 False 了，能重试
once.call(lambda: "ok")         # 目标用例：✅ 现在返回 "ok"
once.calls                      # 正文契约：❌ 失败路径把计数一起清零了
```

Issue #6 的正文同时要求两件事：

1. 「失败 → **不**缓存，后续调用会重新执行」（目标用例）
2. `calls`「实际执行过的次数」——失败也是一次**真实执行**，必须计入

PR 2 用「清空状态」的写法满足了第一句，却把 `_calls` 一起归零，
并且因为它把「尝试」和「成功」都塞进同一个标志，重试路径上的计数不再单调。
只有当验证器**完整落实正文里的两条契约**时才能识破它。

**PR 4（回归门样本——构造期实测发现失效）**

设计意图是：把 `delay_for` 修对（目标用例通过），却在同一提交里顺手
「整理」了 `Latch.open` 的判定口径（`== 0` → `<= 0`），让
`tests/test_gate.py` 的既有测试从绿变红，从而演示回归门拦截。

**构造该分支时实测，这个前提不成立**：

```text
把 Latch.open 由 `self._remaining == 0` 改成 `self._remaining <= 0`
 →  87 passed，没有任何一条测试变红
```

三条原因叠加：

1. 基线只断言 `open is False`，此时 `remaining == 2`——`== 0` 与 `<= 0` 都满足；
2. 能区分两者的唯一路径是「一次 `count_down(n)` 跨过阈值」，
   而 `count_down()` 的返回值**就是** `open`，基线从未构造该场景；
3. 最关键：`<= 0` 恰恰是 Issue #7 的**正确修复**而非回归——
   `count_down` 返回 `self.open`，若恢复 `== 0`，`count_down(5)` 会把闩锁
   留在「未打开」状态；PR 正文把它描述成「顺手统一口径」，实际是在修一个真缺陷。

因此 PR4 实际走的是「F2P 成立 + 回归门通过 → 建议合并」这条**正确**路径，
只是预期的「回归门拦截」演示不出来。**即 lockkit 目前无法提供回归门对抗样本**
（`ratekit` / `pipekit` / `envkit` 三套可以提供）。

该问题已记入 [`BENCHMARK.md`](../../BENCHMARK.md) §4.6，属**夹具规格 bug**，待修。

**PR 3（文档冒充）** 只在 README 里宣称「已支持 `LockSet`」并指向
一个并不存在的 `lockkit/lockset.py`。它检验 Fissue 会不会被文本说服。

### 其余 Issue 的作用

| Issue | 作用 |
|---|---|
| #10 有序加锁集 | FEATURE → 检验「只打标签、不生成验证器」 |
| #11 可用数口径统一 | 难度高、需设计决策（含破坏性变更）→ 检验「不该自动修的要拦住」 |
| #12 并发下放行超额 | 与 #8 **同源但证据独立**（无最小复现）→ 检验判重**不误标** |
| #13 空 peak 应抛错 | **设计偏好变更**：与库现有契约正好相反 → 真实性应判低，不应改代码 |
| #14 性能+推广 | **刷量**：无复现、模板化、夹带联系方式 → 真实性应判低 |
| #15 说不清哪里错 | **无效**：无最小复现 → 真实性应判低 |

> #13 是刻意与 #9 配对的反例：#9 是「实现漏了判空、违反自己的文档」（该改），
> #13 是「用户想改掉文档规定的行为」（不该按它的方案改）。
> 两者看起来都是「改 `Meter.peak`」，但只有 #9 值得动代码。

> #12 是刻意与 #8 配对的反例：#8 是**单线程、纯读取**的读数问题（有确定的最小复现），
> #12 是**多线程**下的放行超额（给不出复现）。二者都在说「令牌数不对」，
> 但一个可复现、一个不可复现，用来检验判重**从严、不误标**。

## 一、推送到 GitHub

GitHub 的写操作（建仓库 / 推分支 / 建 Issue-PR）由你自己执行：

```bash
# 1) 准备 token（勾 repo 权限）
#    https://github.com/settings/tokens  →  Generate new token (classic)  →  勾 repo
export GITHUB_TOKEN=ghp_xxxx

# 2) 先干跑看看计划（不写任何东西）
.venv/Scripts/python.exe demo/lockkit/scripts/setup_github.py --owner John-is-playing --dry-run

# 3) 真跑
.venv/Scripts/python.exe demo/lockkit/scripts/setup_github.py --owner John-is-playing
```

> Windows 上用 `.venv/Scripts/python.exe`（本机有 HTTPS 中间人，
> 全局 Python 缺 truststore 会 TLS 失败，必须用 .venv 里的解释器）。

脚本会依次完成：

1. 创建 `John-is-playing/lockkit` 仓库（已存在则复用）
2. 推送 `main` 分支
3. 从 `fixtures/*.md` 创建 15 个 Issue（带标签）
4. 推送 5 个修复分支并创建 5 个 PR（正文里带 `Closes #N`）

脚本是**可重入**的：已存在的 Issue（按标题）与 PR（按 head 分支）会跳过，失败可放心重跑。

常用参数：

```bash
--name lockkit-demo    # 换个仓库名
--private              # 建私有仓库（注意：Fissue 需要 token 才能读私有库）
--no-prs               # 只建 Issue，先看看评测效果
```

### 手动推送（方式 B）

```bash
cd demo/lockkit/repo
git remote add origin https://github.com/John-is-playing/lockkit.git
git push -u origin main
git push origin fix/semaphore-release-cap fix/once-retry-after-failure \
                fix/docs-lockset fix/retry-cap-and-subclass \
                fix/tokenbucket-exact-drain
```

然后在网页上按 `fixtures/*.md` 头部的 `<!-- ... -->`（title / labels）逐个建 Issue，
再为五个分支各建一个 PR（base 选 `main`）。

## 二、用 Fissue 测它

把仓库登记进 `config.yaml` 的 `repos:`：

```yaml
repos:
  - platform: github
    owner: John-is-playing
    name: lockkit
    base_branch: main
    collect: [issue, pr]
    since_days: 0        # 0 = 不限制时间窗口，确保 15 个 Issue 全抓到
    test_hint: "python -m pytest -q"    # 明确告诉它怎么跑测试
```

然后跑完整链路：

```bash
# 1) 抓取（应拉到 15 个 Issue + 5 个 PR）
fissue fetch --repo John-is-playing/lockkit --limit 80

# 2) 评测（四维评分 + 分类 + 反刷子）
fissue eval --repo John-is-playing/lockkit --all

# 3) 看结果：#1/#3/#5/#8 应为 tier1，#2/#4/#6/#7/#9 应为 tier2
fissue report --repo John-is-playing/lockkit

# 4) 生成验证器并做 F2P 验证
fissue verify --repo John-is-playing/lockkit

# 5) 批量定论 + 打标签
fissue flush

# 6) 试跑自动修复（只出补丁，不提 PR）
fissue fix --repo John-is-playing/lockkit --dry-run --limit 9
```

## 三、期望结果对照表

| # | 条目 | 维度 | 期望分类 | 期望优先级 | 期望验证 | 期望动作 |
|---|---|---|---|---|---|---|
| 1 | release 无限归还 | 不变量 | bug | **tier1** | base 失败（需多还一次） | 自动修复 → 提 PR |
| 2 | borrow 异常泄漏 | 不变量 | bug | **tier2** | base 失败（异常路径） | 自动修复 → 提 PR |
| 3 | 退避不封顶 | 上限 | bug | **tier1** | base 失败（需够多尝试次数） | 自动修复 → 提 PR |
| 4 | 子类不重试 | 上限 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 5 | 恰好取尽被拒 | 边界 | bug | **tier1** | base 失败 | 自动修复 → 提 PR |
| 6 | 失败被缓存 | 一次性 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 7 | 跨阈值不打开 | 一次性 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 8 | peek 读数陈旧 | 时间 | bug | **tier1** | base 失败（需注入假时钟） | 自动修复 → 提 PR |
| 9 | 空 peak 抛错 | 口径 | bug | **tier2** | base 失败 | 自动修复 → 提 PR |
| 10 | 有序加锁集 | — | **feature** | none | 不验证 | 只打标签 |
| 11 | 可用数口径统一 | — | bug | none | 难自动化（需决策） | 不修复，打标签 |
| 12 | 并发放行超额 | 时间 | bug | none | — | **同源但非重复**（不误标） |
| 13 | 空 peak 应抛错 | 口径 | **低真实性** | none | — | 设计偏好变更，不按它改 |
| 14 | 性能+推广 | — | **低真实性** | none | — | 判为刷量 |
| 15 | 说不清哪里错 | — | **低真实性** | none | — | 判为无效 |
| PR1 | Semaphore 修复 | 不变量 | bug | none | **F2P 成立 + 回归通过** | 建议合并 |
| PR2 | Once「修复」 | 一次性 | bug | none | **目标通过但破坏 calls 语义** | 建议不合并 ⚠️ |
| PR3 | LockSet 文档 | feature | none | — | 无代码变更 | 建议不合并 |
| PR4 | delay_for 修复 | 上限 | bug | none | **F2P 成立但回归门不过** | 建议不合并 ⚠️ |
| PR5 | allow 修复 | 边界 | bug | none | **F2P 成立 + 回归通过** | 建议合并 |

四行最关键：

- **PR2**：如果被判「可合并」，说明验证器没有完整落实 Issue #6 正文里的**两条**契约
  （只验了「失败后可重试」，漏了「失败也是一次真实执行、必须计入 `calls`」）——
  检查验证器是否覆盖了正文里对 `calls` 的断言。
- **PR4**：如果被判「可合并」，说明回归门没生效——检查
  `verifier.regression_gate` 是否为 `strict`（`warn` 只告警不拦）。
- **PR3**：如果被判「可合并」，说明评测被文档文本说服，
  应当确认「feature + 无代码变更」会走不合并分支。
- **#13**：如果被当成真缺陷并生成补丁，说明「设计偏好变更」没有被规则拦住——
  它与 #9 只差在「是谁的契约被违反」。

另外两条**验证器质量**的观察点：

- **#8**：验证器若写成 `time.sleep(1)` 再读 `peek()`，在沙盒里会不稳定
  （真实时间不可控）。正确的做法是注入 `ManualClock` 并 `tick`。
- **#1**：验证器若只做「借一次还一次」，会**通过**（当前实现也是对的）——
  必须**多还一次**才能让 base 失败。

## 四、本地自检（不连网）

推之前可以先在本地确认夹具是自洽的：

```bash
cd demo/lockkit/repo
.venv/Scripts/python.exe -m pytest -q          # 基线应 87 passed

# 确认 9 处缺陷确实存在
.venv/Scripts/python.exe - <<'PY'
import sys; sys.path.insert(0, '.')
from lockkit import (ManualClock, TokenBucket, Semaphore, ResourcePool,
                     RetryPolicy, is_retryable, Once, Latch, Meter)

# 1) 许可可以凭空多还出来
s = Semaphore(1); s.acquire(); s.release(); s.release()
print("1) available 超过 permits ->", s.available)          # 2（缺陷）

# 2) 异常路径不归还
pool = ResourcePool(1, lambda: object())
try:
    with pool.borrow():
        raise RuntimeError("boom")
except RuntimeError:
    pass
print("2) 异常后可用 ->", pool.available)                    # 0（缺陷）

# 3) 退避不封顶
p = RetryPolicy(attempts=8, base_delay=1.0, factor=2.0, max_delay=5.0)
print("3) delay_for(5) ->", p.delay_for(5))                 # 16.0（缺陷）

# 4) 子类不重试
print("4) TimeoutError 可重试 ->", is_retryable(TimeoutError("x"), (OSError,)))  # False（缺陷）

# 5) 恰好取尽被拒
b = TokenBucket(rate=10, capacity=3)
print("5) allow(3) ->", b.allow(3))                         # False（缺陷）

# 6) 失败被当作已完成
o = Once()
try: o.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
except ValueError: pass
print("6) 失败后 done ->", o.done)                           # True（缺陷）

# 7) 一次跨过阈值不打开
l = Latch(2); l.count_down(5)
print("7) open ->", l.open)                                 # False（缺陷）

# 8) peek 不补充
clk = ManualClock(); b2 = TokenBucket(rate=10, capacity=3, clock=clk)
b2.allow(2); clk.tick(1.0)
print("8) 补充 1 秒后 peek ->", b2.peek())                   # 1.0（缺陷）

# 9) 空样本取 peak
try:
    Meter().peak
    print("9) 空 peak 正常")
except ValueError as e:
    print("9) 空 peak 抛错 ->", e)                           # ValueError（缺陷）
PY
```

预期 9 行全部打出「缺陷」侧的值。

## 五、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `需要 GitHub token` | 没设 `GITHUB_TOKEN` | `export GITHUB_TOKEN=ghp_xxx` |
| `token 无效（HTTP 401）` | token 过期 / 权限不足 | 重新生成，勾 `repo` |
| `创建仓库失败（HTTP 422）` | 同名仓库已存在 | 换 `--name`，或让它复用（脚本会自动跳过） |
| `lockkit 工作区不干净` | repo 有未提交改动 | `cd demo/lockkit/repo && git status` 后提交或还原 |
| `创建 PR 失败（HTTP 422）` | 分支无差异，或 PR 已存在 | 脚本会跳过已存在的；确认分支已推送 |
| TLS 证书校验失败 | 用了全局 Python | 改用 `.venv/Scripts/python.exe`（含 truststore） |
| 基线测试数对不上（≠87） | 收集到了 `__pycache__` 或环境里的同名包 | 在 `demo/lockkit/repo` 下跑，确认没有全局 `lockkit` 干扰 |

> **为什么强调换行符**：Fissue 验证 PR 时用 `git apply` 打平台返回的 diff。
> 如果仓库里混入 CRLF，diff 的上下文行与实际文件字节不一致，补丁会应用失败。
> 所以 `demo/lockkit/repo/.gitattributes` 里固定了 `* text=auto eol=lf`。

