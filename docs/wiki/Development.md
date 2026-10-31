# 开发与发布

> 开发环境、测试、CI、提 PR 与发布约定。价值观与贡献路径见[贡献指南](Contributing.md)。

## 1. 开发环境

```bash
git clone https://github.com/John-is-playing/Fissue.git && cd Fissue
python -m venv .venv && source .venv/Scripts/activate   # Linux/macOS 用 .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp config.example.yaml config.yaml   # 测试不连网、不需要真实 Key
```

> 全局 Python 若处于 HTTPS 中间人环境会缺 `truststore`，**必须**用虚拟环境的 Python。

## 2. 测试完全离线

```bash
python -m pytest -q                          # 全量
python -m pytest tests/test_pipeline.py -q   # 单文件
python -m pytest -q -k duplicate             # 按关键字
ruff check src tests && ruff format --check src tests
```

`tests/conftest.py` 有 autouse **网络守卫**，任何外连会立刻以清晰报错暴露，而不是伪装成「跑得慢」。
LLM 用打桩客户端、平台适配器用 `respx` 拦截、沙盒走 `local + force_inline`——不需要 Token、不需要 Docker。

> 单测只锁定「规则写对了」，**不锁定「模型每次照做」**。报 LLM 相关问题请附原始响应
> （`data/artifacts/**`）。

## 3. CI（`.github/workflows/ci.yml`，四个 job 全阻断）

| job | 内容 |
|---|---|
| `pytest (3.10 / 3.12 / 3.14)` | 最低支持版本 + 现代版本 + 本机实测跑通全量的版本 |
| `pytest (windows)` | `windows-2022` 跑同一套离线用例（不跑覆盖率） |
| `sandbox (docker)` | `FISSUE_TEST_DOCKER=1` 跑 `tests/test_sandbox_docker.py`——真容器用例 |
| `ruff + mypy` | `ruff check src tests`、`mypy src/fissue tests` |

**为什么有真容器任务**：默认套件完全离线，`sandbox.runtime=docker` 那条路径在别处一次都不会执行
（测试对 Docker 全是打桩），而挂载构造、`--user`、`--read-only`、`--network none` 写错了单测照样
全绿。项目反复吃过「只有真机才照得出来」的亏（PostgreSQL / MySQL / Gitee 写操作 / 私有仓库克隆）。

**为什么有 Windows 任务**：代码里有成片的 Windows 专属分支（inline 回退、命名管道、`os.name`
守卫、路径分隔符），在 ubuntu 上一条都不会执行；反过来 AF_UNIX 类用例在 Windows 只能 skip。
两个平台互相补盲区。覆盖率只在 ubuntu 统计，两处各算一套只会打架。

单条用例有 **120s 硬超时**（`pytest-timeout`）：网络守卫只放行 localhost，而本机代理恰在
localhost，真实请求既不被拦也不被 respx 拦截，会静默重试到超时（表现为 pytest 假死）；
有了它，同样的故障会变成明确的 `Timeout` 并指到卡住的用例。

CI **不需要任何 Secret**（完全离线）；`runs-on` 钉 `ubuntu-24.04`；触发为 push 到 `main`、
所有 PR 与手动 `workflow_dispatch`，`concurrency` 取消同分支进行中任务。

> 本地提交前先跑与 CI 一致的命令（`ruff check`、`mypy src/fissue tests`、`pytest`）——
> 历史上出现过「本地不跑 mypy 就直接推、把 CI 打红」。

## 4. 提交与 PR

Conventional Commits + 简体中文说明：`<type>: <中文说明>`，type 取
`feat / fix / test / docs / refactor / chore`。

- 从 `main` 拉分支，目标 `main`；一个 PR 只做一件事
- 填 PR 模板并给出验证证据；`ruff check` 通过、`pytest` 全绿
- 新增行为带测试，修 Bug 带回归测试

敏感区域：`sandbox/`（安全边界）· `pipeline/stages.py` 重复阈值 0.22（实测标定）· `verifier/`
（F2P 核心）· `fixer/` 护栏 · `config.py` 默认值。

## 5. 发布约定

- 语义化版本；`v1.0.0` 是首个正式版本（`pyproject.toml` 同步）。
- **快照版本** `vX.Y.Z-bN`（如 `v1.1.0-b3`）在正式版之间沉淀，落地形态：
  1. 建 `rel/vX.Y.Z-bN` 分支；2. 打**附注 tag**；3. `CHANGELOG.md` 写好条目，Release 内容与之一致；
  4. **快照提交不改 `pyproject.toml` / `__version__`**；5. 推送 `origin`（GitHub）与 `atomgit`。
- `CHANGELOG.md` 参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；安全修复在
  Security 段致谢（除非要求匿名）。

## 6. 跑分基线

```bash
git status --short          # 应无输出（正式跑分前先提交）
fissue bench capture --repo John-is-playing/ratekit --label ratekit-before
#   … 改代码 / 调配置 …
fissue bench capture --repo John-is-playing/ratekit --label ratekit-after
fissue bench diff data/benchmarks/ratekit-before.json data/benchmarks/ratekit-after.json
```

记录**前提**（commit、未提交改动、模型与采样参数、配置指纹）与**结果**（各阶段 outcome 交叉表、
F2P 成立数、修复结果）。退出码 `0` 可比无变化 / `1` 不可比 / `2` 可比有变化——可塞进 CI 当回归门。
工作区脏的基线标 `reproducible: false`（不允许拿「代码随时会变」的两次测量当回归依据）。
基线是**唯一进版本库的 `data/` 子目录**。

## 7. 常见坑

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前 `git status` |
| Windows 换行符 | 夹具强制 LF，CRLF 会让 `git apply` 失败 |
| 文本模式写文件 | `write_text` 会把 `\n` 转 `\r\n`——参照 `_write_patch` 用二进制 |
| 子进程编码 | 必须显式 `encoding="utf-8"` |
| 全局 Python 缺 truststore | 中间人环境必须用虚拟环境的 Python |
| 缓存目录误命中 | `grep` 可能命中 `.ruff_cache`、`__pycache__` |

---

相关：[贡献指南](Contributing.md) · [架构设计](Architecture.md) · [扩展指南](Extending.md) ·
[基准与可信度](Benchmark.md) · [CHANGELOG](../../CHANGELOG.md)