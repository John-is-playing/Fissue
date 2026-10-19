# 故障排查

> 先自检，再对症状查表。更长的排查叙事见 [docs/USAGE.md §11](../USAGE.md#11-故障排查)。

---

## 1. 快速自检

```bash
fissue db check          # 数据库通不通
fissue sandbox check     # Docker 与隔离参数
fissue status            # 条目 / 队列 / 用量 / 沙盒 全景
fissue --help
```

任何命令都支持 `-v / --verbose` 看调试日志。

---

## 2. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `缺少 LLM API Key` | `.env` 里没填 `FISSUE_LLM_API_KEY` | 填上；或确认 `fissue` 是在项目根目录执行（能找到 `.env`） |
| `无法连接数据库` | 连接串错 / PostgreSQL 没起 | `fissue db check`；PG 用 `pg_isready` 确认 |
| `数据库不存在` | 没建库 | `createdb -U fissue fissue` 后 `fissue db init` |
| `401 / Bad credentials` | Token 过期或权限不足 | 重新生成 Token，检查 scope |
| `触发限流` | 未配 Token 或调用过密 | 配 Token（GitHub 匿名 60 次/小时 → 5000 次/小时）；调小 `--concurrency` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 环境里有 HTTPS 中间人（企业网关 / 抓包工具）。**先看报错里的「当前生效的信任链」**：显示 `certifi` → 装 `truststore`；显示「操作系统信任库（truststore）」→ 根证书连系统库也没有，需导出并设 `FISSUE_CA_BUNDLE` | 详见 [配置参考 §TLS](Configuration.md#关于-tls-中间人) 与 [ENV.md §3.7](../ENV.md) |
| `未找到 docker 命令` | 没装 Docker / 不在 PATH | 装 Docker Desktop；或 `runtime: local` 降级（**无隔离**） |
| `镜像不可用` | 没构建基础镜像 | `docker build -t fissue/sandbox-base:latest docker/sandbox` |
| `无法连接转发组件` | Linux 上没起 `sandbox serve` | 起它，或确认 socket 路径与配置一致 |
| 验证器一直 `needs_manual` | 问题无法自动化验证 / 验证器不可靠 | **正常现象**；看 `data/artifacts/**` 里的执行日志人工复核 |
| 修复总是失败 | 仓库没有测试框架 / 问题太难 | 降级预期；或给 `test_hint` 明确测试命令 |
| `打标签失败` | Token 无写权限 | Token 需要写权限（GitHub `repo`） |
| 仪表盘 404 | 条目 key 含 `#`，URL 未编码 | 用页面链接；API 用 `%23` 代替 `#` |
| `serve` 说「配置里没有启用的仓库」 | `config.yaml` 的 `repos` 为空 | 登记至少一个仓库，或改用 CLI 传 `--repo` |

---

## 3. 让测试命令更准

AI 有时猜不准项目的测试方式。加一条提示即可大幅提升验证器质量：

```yaml
repos:
  - platform: github
    owner: psf
    name: requests
    test_hint: "python -m pytest tests/ -q"
```

`test_hint` 同时是**回归门**的命令来源之一（优先级：`verifier.regression_command` → `repo.test_hint` → 探测）。

---

## 4. 产物落盘位置

都在 `app.data_dir`（默认 `./data`）下：

```
data/
  artifacts/<key>/base/       # 验证器 base 阶段输出（stdout/stderr/meta）
  artifacts/<key>/fix/        # 修复阶段输出
  artifacts/<key>/agent/      # Agent 循环每轮输出
  manual/<key>/report.md      # 需人工处理的完整报告
  manual/<key>/attempt.patch  # 尝试过的补丁
  patches/<key>.patch         # dry-run / patch_only 产出的补丁
  exports/                    # report / export 输出
  benchmarks/                 # bench capture 的基线（进版本库）
```

人工报告 `manual/<key>/report.md` 含：失败原因、Agent 动作轨迹、被拒写入、最后一次验证输出。

---

## 5. 直接用数据库查

以 PostgreSQL 为例：

```bash
# 最近的高优先级待修条目
psql -d fissue -c "select key,priority,status from items where priority in ('tier1','tier2') order by id desc limit 20;"

# 今日 token 消耗
psql -d fissue -c "select purpose,sum(prompt_tokens+completion_tokens) t,round(sum(cost_usd)::numeric,4) usd from llm_usage where created_at > now()-interval '1 day' group by purpose;"

# 队列堆积情况
psql -d fissue -c "select queue,status,count(*) from queue_entries group by 1,2;"
```

---

## 6. 彻底重来

```bash
fissue db init --drop            # 清空所有表（危险，需确认）
rm -rf data/                     # 清掉本地产物
fissue db init
```

---

## 7. 排查时的常见陷阱

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前先 `git status` 确认干净 |
| Windows 上换行符 | 夹具仓库有 `.gitattributes` 强制 LF。混入 CRLF 会导致 `git apply` 失败（验证 PR 时依赖它） |
| 全局 Python 缺 truststore | 本机若处于 HTTPS 中间人环境，**必须**用 `.venv` 里的 Python，否则 TLS 失败 |
| 缓存目录误命中 | `grep` 类工具可能命中 `.ruff_cache`、`__pycache__`，注意排除 |
| LLM Key 缺失 | 单测不需要；但 `eval/verify/fix` 需要，报错信息很明确 |

---

## 相关页

[配置参考](Configuration.md) · [CLI 参考](CLI-Reference.md) · [部署与运维](Deployment.md) · [安全模型](Security.md)
