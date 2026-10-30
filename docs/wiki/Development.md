# 开发与发布

> 开发环境、测试、CI、提交与发布约定。贡献流程与价值观见[贡献指南](Contributing.md)。

---

## 1. 开发环境

```bash
git clone https://github.com/John-is-playing/Fissue.git
cd Fissue

python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash
# source .venv/bin/activate          # Linux / macOS

pip install -e ".[dev]"
```

**最小可跑配置（不需要 LLM Key、不需要 Docker）**：

```bash
cp .env.example .env                 # 测试套件不连网、不需要真实 Key
cp config.example.yaml config.yaml
```

> 全局 Python 若处于 HTTPS 中间人环境会缺 `truststore`，**必须**用 `.venv` 里的 Python。

---

## 2. 测试：完全离线

```bash
python -m pytest -q                          # 全量（接近 700 项）
python -m pytest tests/test_pipeline.py -q   # 单文件
python -m pytest -q -k "duplicate"           # 按关键字
```

**测试套件完全离线**：`tests/conftest.py` 里有 autouse 的**网络守卫**，任何外连会立刻以清晰的报错暴露，
而不是伪装成「跑得慢」。LLM 用打桩客户端、平台适配器用 `respx` 拦截、沙盒走 `local + force_inline`。
所以**不需要配 Token、不需要起 Docker**。

```bash
ruff check src tests
ruff format --check src tests
```

> ⚠️ 单测只锁定「规则写对了」，**不锁定「模型每次照做」**——LLM 输出是非确定性的。
> 报与 LLM 输出有关的问题时，请附上原始响应（`data/artifacts/**` 里有）。

---

## 3. CI

`.github/workflows/ci.yml`，三个 job，**都阻断**（无 `continue-on-error`）：

| job | 内容 |
|---|---|
| `pytest (py3.10 / py3.12 / py3.14)` | 矩阵取**最低支持版本** `3.10`、一个现代版本 `3.12`，以及本机实测跑通全量的 `3.14`；`python -m pytest -q -p no:cacheprovider` |
| `pytest (windows)` | `windows-2022` 上跑同一套离线用例（单个 Python 版本，不跑覆盖率） |
| `ruff + mypy` | `python -m ruff check src tests` 与 `python -m mypy src/fissue tests` |

**为什么有 Windows 任务**：代码里有成片的 Windows 专属分支（`sandbox` 的 inline 回退、
命名管道路径、`os.name == "nt"` 守卫、路径分隔符处理），它们**在 ubuntu 上一条都不会被执行**。
反过来，AF_UNIX 那类 POSIX 专属用例在 Windows 上只能 skip。两个平台各自补对方的盲区，
所以「多跑一遍」不是重复劳动，而是覆盖互相够不到的地方。覆盖率口径只在 ubuntu 上统计，
两处各算一套只会互相打架。

单条用例有 **120s 硬超时**（`pytest-timeout`，配置在 `pyproject.toml` 的 `[tool.pytest.ini_options].timeout`）：本项目的网络守卫只放行 localhost，而本机代理也恰在 localhost，于是真实网络请求既不被拦下也不被 respx 拦截，只静默重试到超时——表现为 pytest **假死**。有了这条，同样的故障会在 120s 内变成一条明确的 `Timeout`，直接指到卡住的用例。

设计要点（读 ci.yml 注释即得）：

1. **完全离线**——因此 CI **不需要任何 Secret**（无 LLM Key、无平台 token、无 Docker）。
2. **测试 / ruff / mypy 全部阻断**：三者存量已清零，再引入新问题会被拦下。
   **mypy 覆盖 `src/fissue` 与 `tests`**（共 70 个源文件）——测试里的类型问题同样是真问题。
3. **runner 与 action 都钉住**：`runs-on` 写死 `ubuntu-24.04`（避免镜像漂移）；action 用当前主版本。
4. 触发：`push` 到 `main`、所有 `pull_request`、以及手动 `workflow_dispatch`；`concurrency` 取消同分支的进行中任务。

> 本地提交前请**自己先跑一遍与 CI 一致的命令**（`ruff check`、`mypy src/fissue tests`、`pytest`）：
> 历史上出现过「本地不跑 mypy 就直接推、把 CI 打红」的教训。

---

## 4. 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，说明文字用**简体中文**：

```
<type>: <中文说明>
```

| type | 用于 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 Bug |
| `test` | 测试与夹具 |
| `docs` | 文档 |
| `refactor` | 重构（不改行为） |
| `chore` | 构建 / 依赖 / CI |

示例：

```
fix: 中文标题的重复预筛改用 bigram 与包含度
test: 新增 lockkit 纯净测试夹具（15 Issue + 5 PR）
docs: 补记 C/D/E/F 的修复过程并收敛遗留清单
```

**好的提交信息解释「为什么」，不只是「做了什么」。** 若某个改动看起来反直觉
（例如「为什么回归门默认 `strict` 而不是 `warn`」），请在提交信息或代码注释里写明理由——
这个项目有大量这类「为什么」注释，是它的风格。

---

## 5. 提 PR

- 从 `main` 拉分支，PR 目标为 `main`。
- 填完 PR 模板（会引导你给出**验证证据**：命令与输出、修复前后 F2P 证据、回归考虑）。
- **必须**：`ruff check` 通过、`pytest` 全绿。
- **必须**：新增行为附带测试；修 Bug 附带回归测试。
- 一个 PR 只做一件事，不要混入无关改动。

### 动了这些地方要格外小心

| 区域 | 为什么 |
|---|---|
| `sandbox/` | 安全边界。任何削弱隔离的改动都要在 PR 里说明威胁模型影响 |
| `pipeline/stages.py` 的重复检测 | 阈值 0.22 是实测标定的，改阈值必须附上标定数据 |
| `verifier/` | F2P 是产品的核心主张，改动会影响所有下游结论 |
| `fixer/` 的护栏 | 宿主侧护栏是最后一道防线 |
| `config.py` 的默认值 | 默认值会决定所有用户的初始行为 |

---

## 6. 发布约定

- **版本号**遵循语义化版本；`v1.0.0` 是首个正式版本（`pyproject.toml` 同步为 `1.0.0`）。
- **快照版本**：正式版之间在 `main` 上沉淀预发布快照，形如 **`vX.Y.Z-bN`**（如 `v1.1.0-b3`），
  历史上有 `rel/v1.0.0-b1…b10`、`rel/v1.0.1-a1`/`a2`。
- 每个快照的落地形态（本项目实际做法）：
  1. 建 **`rel/vX.Y.Z-bN`** 分支；
  2. 打 **附注 tag**（annotated tag）；
  3. 在 `CHANGELOG.md` 写好该版本的条目，**GitHub Release 的内容与该条目一致**；
  4. **快照提交不改 `pyproject.toml` / `__version__`**；
  5. 同时推送到两个远端：`origin`（GitHub）与 `atomgit`。
- `CHANGELOG.md` 格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
  安全修复会在 Security 段致谢（除非要求匿名）。

---

## 7. 跑分基线（`fissue bench`）

改动前后各冻结一次，再逐项对比——这是回答「这次改动让结果变好了还是变坏了」的唯一机械手段：

```bash
git status --short          # 应无输出（正式跑分前先提交）
fissue bench capture --repo John-is-playing/ratekit --label ratekit-before
#   … 改代码 / 调配置 …
fissue bench capture --repo John-is-playing/ratekit --label ratekit-after
fissue bench diff data/benchmarks/ratekit-before.json data/benchmarks/ratekit-after.json
```

- 基线记录**前提**（commit + 是否有未提交改动、模型与采样参数、关键配置指纹）与**结果**（各阶段 outcome 交叉表、F2P 成立数、修复结果）。
- 退出码：`0` 可比且无变化｜`1` **不可比**｜`2` 可比但有变化——可塞进 CI 当回归门。
- 工作区脏的基线会被标记 `reproducible: false`（刻意设计，不允许拿「代码随时会变」的两次测量当回归依据）。
- 基线是**唯一进版本库的 `data/` 子目录**。

---

## 8. 常见坑

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前先 `git status` 确认干净 |
| Windows 上换行符 | 夹具仓库有 `.gitattributes` 强制 LF。混入 CRLF 会导致 `git apply` 失败 |
| 写文件用文本模式 | Windows 上 `write_text` 会把 `\n` 转 `\r\n`，破坏补丁/换行敏感产物——参照 `_write_patch` 用二进制写 |
| 子进程编码 | 子进程调用**必须**显式 `encoding="utf-8"`（中文环境按 GBK 解码会乱码） |
| 全局 Python 缺 truststore | 中间人环境下必须用 `.venv` 的 Python |
| 缓存目录误命中 | `grep` 类工具可能命中 `.ruff_cache`、`__pycache__`，注意排除 |

---

## 相关页

[贡献指南](Contributing.md) · [架构设计](Architecture.md) · [扩展指南](Extending.md) · [基准与可信度](Benchmark.md) · [CHANGELOG](../../CHANGELOG.md)
