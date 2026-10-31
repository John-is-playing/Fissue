# 故障排查

> 先自检，再按症状查表。完整的排查叙事见 [USAGE.md §11](../USAGE.md)。

## 1. 快速自检

```bash
fissue db check          # 数据库通不通
fissue sandbox check     # Docker 与隔离参数
fissue status            # 条目 / 队列 / 用量 / 沙盒 全景
```

任何命令都支持 `-v / --verbose`。

## 2. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `缺少 LLM API Key` | `.env` 没填 | 填上；确认在项目根目录执行（能找到 `.env`） |
| `无法连接数据库` | 连接串错 / PG 没起 | `fissue db check`；`pg_isready` |
| `数据库不存在` | 没建库 | `createdb -U fissue fissue` 后 `fissue db init` |
| `401 / Bad credentials` | Token 过期或权限不足 | 重新生成，检查 scope |
| 触发限流 | 未配 Token 或调用过密 | 配 Token（GitHub 匿名 60 → 5000 次/小时）；调小 `--concurrency` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | HTTPS 中间人。**先看报错里的「当前生效的信任链」**：`certifi` → 装 truststore；已是系统信任库 → 导出根证书设 `FISSUE_CA_BUNDLE` | 见 [ENV.md §3.7](../ENV.md) |
| `未找到 docker 命令` | 没装 / 不在 PATH | 装 Docker；或 `runtime: local`（**无隔离**） |
| `镜像不可用` | 没构建镜像 | `docker build -t fissue/sandbox-base:latest docker/sandbox` |
| `无法连接转发组件` | Linux 上没起 `sandbox serve` | 起它，或核对 socket 路径 |
| 验证器一直 `needs_manual` | 无法自动验证 / 验证器不可靠 | **正常现象**；看 `data/artifacts/**` 人工复核 |
| 修复总是失败 | 没测试框架 / 问题太难 | 给 `test_hint` 明确测试命令 |
| 打标签失败 | Token 无写权限 | 需要写权限（GitHub `repo`） |
| 仪表盘 404 | key 含 `#` 未编码 | 用页面链接；API 用 `%23` |
| `serve` 说没有启用仓库 | `repos` 为空 | 登记至少一个，或用 CLI 传 `--repo` |

## 3. 让测试命令更准

```yaml
repos:
  - platform: github
    owner: psf
    name: requests
    test_hint: "python -m pytest tests/ -q"
```

`test_hint` 同时是**回归门**的命令来源（优先级：`verifier.regression_command` → `repo.test_hint` → 探测）。

## 4. 产物位置

都在 `app.data_dir`（默认 `./data`）下：

```
data/
  artifacts/<key>/base/       # 验证器 base 阶段输出（stdout/stderr/meta）
  artifacts/<key>/fix/        # 修复阶段输出
  artifacts/<key>/agent/      # Agent 每轮输出
  manual/<key>/report.md      # 人工处理报告（失败原因、动作轨迹、被拒写入、最后一次验证输出）
  manual/<key>/attempt.patch
  patches/<key>.patch         # dry-run / patch_only 补丁
  exports/                    # report / export
  benchmarks/                 # bench 基线（进版本库）
```

## 5. 直接查数据库

```sql
-- 最近的高优先级待修条目
select key,priority,status from items where priority in ('tier1','tier2') order by id desc limit 20;
-- 今日 token 消耗
select purpose,sum(prompt_tokens+completion_tokens) t,round(sum(cost_usd)::numeric,4) usd
  from llm_usage where created_at > now()-interval '1 day' group by purpose;
-- 队列堆积
select queue,status,count(*) from queue_entries group by 1,2;
```

## 6. 彻底重来

```bash
fissue db init --drop            # 清空所有表（危险，需确认）
rm -rf data/                     # 清掉本地产物
fissue db init
```

## 7. 排查时的陷阱

| 坑 | 说明 |
|---|---|
| 夹具仓库有未提交改动 | `demo/*/repo` 是独立 git 仓库，推送前 `git status` |
| Windows 换行符 | 夹具强制 LF，CRLF 会让 `git apply` 失败 |
| 全局 Python 缺 truststore | 中间人环境必须用虚拟环境的 Python |
| 缓存目录误命中 | `grep` 可能命中 `.ruff_cache`、`__pycache__` |
| LLM Key 缺失 | 单测不需要；`eval/verify/fix` 需要 |

---

相关：[配置参考](Configuration.md) · [CLI 参考](CLI-Reference.md) · [部署与运维](Deployment.md) ·
[安全模型](Security.md) · [ENV.md](../ENV.md)