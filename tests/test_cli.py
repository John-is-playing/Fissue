"""CLI 命令测试。

重点覆盖两类问题：

1. **空库可用性（回归）**：全新数据库上直接跑 ``status`` / ``report`` / ``export``
   必须正常退出，而不是 ``no such table: llm_usage`` 崩掉。
   （这是真实踩过的坑：只读命令原先从不建表。）
2. **参数与退出码**：错误输入要给明确报错和非零退出码，而不是抛裸异常。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import fissue.cli  # noqa: F401  —— 触发 ops 里的命令注册
from fissue.cli.main import app
from fissue.models import (
    Category,
    DimensionScore,
    Evaluation,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    QueueName,
    RawItem,
    RepoRef,
    Scores,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# 夹具：一份完全隔离的配置（SQLite + 打桩 key）
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """准备独立的 config.yaml / .env，并切到临时 cwd。

    返回路径字典，方便用例里 `--config`/`--env` 显式传入（避免读到仓库根的真实配置）。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FISSUE_DATABASE_URL", raising=False)
    monkeypatch.delenv("FISSUE_LLM_API_KEY", raising=False)

    db_path = (tmp_path / "cli.db").as_posix()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "database": {"url": f"sqlite:///{db_path}"},
                "app": {"data_dir": str(tmp_path / "data"), "log_level": "warning"},
                "export": {"output_dir": str(tmp_path / "exports")},
                "notify": {"enabled": False, "channels": []},
                "repos": [{"platform": "github", "owner": "psf", "name": "requests", "since_days": 0}],
            }
        ),
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text("FISSUE_LLM_API_KEY=test-key\n", encoding="utf-8")

    return {
        "config": str(cfg),
        "env": str(env),
        "db": f"sqlite:///{db_path}",
        "db_file": str(tmp_path / "cli.db"),
        "root": str(tmp_path),
    }


def _run(args: list[str], env: dict[str, str]) -> Any:
    """统一带上 --config/--env 执行。"""
    return runner.invoke(app, [*args, "--config", env["config"], "--env", env["env"]])


# ---------------------------------------------------------------------------
# 1. 空库回归：只读命令必须能用
# ---------------------------------------------------------------------------


def test_status_on_empty_database(cli_env: dict[str, str]) -> None:
    """全新库直接跑 status 不能崩（回归：曾报 no such table: llm_usage）。"""
    assert not Path(cli_env["db_file"]).exists(), "前置条件：库文件尚不存在"

    result = _run(["status"], cli_env)

    assert result.exit_code == 0, result.output
    assert result.exception is None, f"不该有未捕获异常：{result.exception!r}"
    assert "沙盒" in result.output
    assert "今日用量" in result.output
    assert "psf/requests" in result.output


def test_report_on_empty_database(cli_env: dict[str, str]) -> None:
    """空库生成报告应输出「条目总数 0」而不是报错。"""
    result = _run(["report", "--repo", "psf/requests"], cli_env)

    assert result.exit_code == 0, result.output
    assert "条目总数" in result.output
    # 报告是 Markdown 表格，同样会被 Rich 按控制台宽度折行；这里原先写的是
    # ``replace(" ", " ")``（空格换空格，等于什么都没做），一旦折行断言即失效。
    assert "| 0 |" in " ".join(result.output.split())


def test_export_on_empty_database(cli_env: dict[str, str]) -> None:
    """空库导出应产出合法 JSON（count=0）。"""
    out = str(Path(cli_env["root"]) / "empty.json")
    result = _run(["export", "--format", "json", "--out", out], cli_env)

    assert result.exit_code == 0, result.output
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    assert payload["count"] == 0
    assert payload["items"] == []


def test_export_markdown_on_empty_database(cli_env: dict[str, str]) -> None:
    out = str(Path(cli_env["root"]) / "empty.md")
    result = _run(["export", "--format", "markdown", "--out", out], cli_env)

    assert result.exit_code == 0, result.output
    assert "# Fissue 评测报告" in Path(out).read_text(encoding="utf-8")


def test_read_only_commands_do_not_require_llm_key(cli_env: dict[str, str]) -> None:
    """只读命令不该强制要求 LLM key（没 key 也能看状态）。"""
    Path(cli_env["env"]).write_text("FISSUE_LLM_API_KEY=\n", encoding="utf-8")
    result = _run(["status"], cli_env)
    assert result.exit_code == 0, result.output


def test_llm_commands_fail_fast_without_a_key(cli_env: dict[str, str]) -> None:
    """要用 LLM 的命令缺 key 时**当场**报错，而不是跑到一半才炸。

    覆盖面（都走 `load_cfg(..., require_llm=True)`）：`eval` / `verify` / `flush` / `fix` /
    `serve`。`web` 刻意不在此列——只读浏览不该被 key 挡住。
    这里用 `eval` 代表这一组：报错要指明怎么补 key。
    """
    Path(cli_env["env"]).write_text("FISSUE_LLM_API_KEY=\n", encoding="utf-8")

    result = _run(["eval", "--limit", "1"], cli_env)

    assert result.exit_code != 0, result.output
    detail = f"{result.exception or ''}{result.output}"
    assert "FISSUE_LLM_API_KEY" in detail, f"要说清怎么补 key：{detail[:200]}"


def test_status_json_output(cli_env: dict[str, str]) -> None:
    result = _run(["status", "--json"], cli_env)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data) >= {"repos_config", "sandbox", "items", "queues", "llm_usage_today"}
    assert "psf/requests" in data["repos_config"]


def test_status_json_is_clean_in_a_real_process(cli_env: dict[str, str]) -> None:
    """真进程里 `--json` 的 stdout 只能是 JSON——日志钻进去就立刻不可解析。

    **为什么必须用子进程**：pytest 的 logging 插件会接管根日志器、把日志**捕获**掉，于是
    进程内跑 CliRunner 根本看不到「日志进了 stdout」这件事，反而会掩盖这个 bug（先写错过
    一版，就是这么假通过的）。

    回归：`sandbox.runtime: local` 会在加载配置时发一条 WARNING，而 `RichHandler()` 默认
    自建的 Console 落在 **stdout** 上（回退分支的 `StreamHandler` 才是 stderr），于是
    ``json.loads(stdout)`` 直接 `JSONDecodeError`。
    """
    import os
    import subprocess
    import sys

    cfg = Path(cli_env["config"])
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["sandbox"] = {"runtime": "local"}          # 触发那条 WARNING
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")

    repo_src = Path(__file__).resolve().parents[1] / "src"
    # 继承父进程环境，但**剔除 `FISSUE_*`**——这才是这里限制环境的本意：父进程若带着它们
    # （CI 注入、或本机 .env 导出过），会把 CLI 的配置覆盖掉，这条用例就测不到真东西。
    #
    # 为什么不能只传 PATH/HOME/PYTHONPATH：Windows 上缺 `SYSTEMROOT` 时子进程连 Winsock 都
    # 初始化不了（`OSError: [WinError 10106]`，报在 `import _overlapped` 上），用例于是死在
    # 「子进程根本没起来」而不是它要守的断言上。这个坑原先被上一层解码错误挡着，修完才露出来。
    child_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("FISSUE_")}
    child_env["PYTHONPATH"] = str(repo_src)
    child_env["PYTHONIOENCODING"] = "utf-8"          # 与下面的 encoding 成对，见那里的说明

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fissue.cli.ops import cli_main; cli_main()",
            "status", "--json", "--config", cli_env["config"], "--env", cli_env["env"],
        ],
        capture_output=True,
        # 子进程默认按**平台代码页**编码（中文 Windows 上是 GBK），而 `text=True` 又按 locale
        # 猜解码方式——两边不约定，这条用例会死在 `UnicodeDecodeError` 上，而不是死在它真正
        # 要守的断言上（stdout 必须只有 JSON、告警必须落在 stderr）。钉死 UTF-8 后，中文
        # 告警的比对在哪个平台都成立。
        encoding="utf-8",
        timeout=120,
        env=child_env,
    )

    assert proc.returncode == 0, f"退出码 {proc.returncode}，stderr：{proc.stderr[-500:]}"
    assert "隔离性弱" not in proc.stdout, "告警不该出现在 stdout"
    payload = json.loads(proc.stdout)               # 这句不能抛
    assert "repos_config" in payload
    assert "隔离性弱" in proc.stderr, "告警应当落在 stderr（否则这条用例是空转）"


# ---------------------------------------------------------------------------
# 2. 有数据时的读取
# ---------------------------------------------------------------------------


def _seed(config_file: str, env_file: str) -> str:
    """往 CLI 的库里塞一条已评测的 Issue，返回它的 key。"""
    from fissue.config import load_settings
    from fissue.store.db import build_database
    from fissue.store.repository import Repository

    settings = load_settings(config_file, env_file)
    db = build_database(settings)
    db.create_all()
    repo = Repository(db)

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=42, item_type=ItemType.ISSUE,
        title="程序崩溃", body="boom", labels=["bug"], author="reporter",
    )
    repo.upsert_item(rid, item)
    repo.save_evaluation(
        item.key,
        Evaluation(
            category=Category.BUG, priority=Priority.TIER1, model="test",
            summary="真实缺陷",
            scores=Scores(
                authenticity=DimensionScore(score=92),
                importance=DimensionScore(score=88),
                difficulty=DimensionScore(score=20),
            ),
        ),
    )
    return item.key


def test_status_shows_seeded_item(cli_env: dict[str, str]) -> None:
    _seed(cli_env["config"], cli_env["env"])
    result = _run(["status"], cli_env)

    assert result.exit_code == 0, result.output
    assert "evaluated" in result.output


def test_report_lists_seeded_item(cli_env: dict[str, str]) -> None:
    _seed(cli_env["config"], cli_env["env"])
    result = _run(["report", "--repo", "psf/requests"], cli_env)

    assert result.exit_code == 0, result.output
    assert "#42" in result.output
    assert "真实缺陷" in result.output


def test_export_json_contains_evaluation(cli_env: dict[str, str]) -> None:
    key = _seed(cli_env["config"], cli_env["env"])
    out = str(Path(cli_env["root"]) / "one.json")
    result = _run(["export", "--format", "json", "--out", out], cli_env)

    assert result.exit_code == 0, result.output
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    assert payload["count"] == 1
    row = payload["items"][0]
    assert row["key"] == key
    assert row["evaluation"]["scores"]["importance"] == 88


def test_export_category_filter(cli_env: dict[str, str]) -> None:
    _seed(cli_env["config"], cli_env["env"])
    out = str(Path(cli_env["root"]) / "feat.json")
    result = _run(["export", "--format", "json", "--category", "feature", "--out", out], cli_env)

    assert result.exit_code == 0, result.output
    assert json.loads(Path(out).read_text(encoding="utf-8"))["count"] == 0


# ---------------------------------------------------------------------------
# 3. 参数与错误处理
# ---------------------------------------------------------------------------


def test_db_init_then_check(cli_env: dict[str, str]) -> None:
    r1 = _run(["db", "init"], cli_env)
    assert r1.exit_code == 0, r1.output

    r2 = _run(["db", "check"], cli_env)
    assert r2.exit_code == 0, r2.output
    assert "连接正常" in r2.output


def test_db_check_on_missing_database_fails_cleanly(cli_env: dict[str, str]) -> None:
    """数据库连不上要给出可读提示 + 非零退出码，而不是抛栈。"""
    broken = Path(cli_env["config"])
    cfg = yaml.safe_load(broken.read_text(encoding="utf-8"))
    cfg["database"]["url"] = "postgresql+psycopg://nobody:nope@127.0.0.1:1/none"
    broken.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    result = _run(["db", "check"], cli_env)
    assert result.exit_code == 1
    assert "连接失败" in result.output


def test_fix_with_an_invalid_repo_ref_refuses_instead_of_fixing_everything(cli_env: dict[str, str]) -> None:
    """`fix --repo` 写错时必须报错，**绝不能**静默改成「配置里所有仓库」。

    `fix` 是写命令（会推分支、提 PR）。原先 `_resolve_repos_safe` 把解析异常吞掉后回退到
    `enabled_repos()`，于是 `--repo bad-spec` 变成了「在你没点名的仓库上全都修一遍」。
    """
    result = _run(["fix", "--repo", "bad-spec"], cli_env)

    assert result.exit_code != 0
    assert "解析失败" in result.output, result.output
    assert "bad-spec" in result.output, "报错要点名是哪个 spec 不合法"


def test_missing_explicit_config_fails(cli_env: dict[str, str]) -> None:
    """显式指定不存在的配置文件 → 明确报错（不静默用默认值）。"""
    result = runner.invoke(
        app, ["status", "--config", str(Path(cli_env["root"]) / "nope.yaml"), "--env", cli_env["env"]]
    )
    assert result.exit_code != 0


def test_unknown_repo_flag_still_resolves(cli_env: dict[str, str]) -> None:
    """--repo 传未登记的仓库时应临时构造配置，而不是报错。"""
    out = str(Path(cli_env["root"]) / "other.json")
    result = _run(["export", "--repo", "psf/other", "--out", out], cli_env)
    # 允许成功（空结果）或明确失败，但不该是未捕获异常
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_help_lists_all_commands(cli_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("fetch", "eval", "verify", "flush", "fix", "report", "export", "status", "serve", "web"):
        assert cmd in result.output


def test_flush_on_empty_queues_ok(cli_env: dict[str, str]) -> None:
    _run(["db", "init"], cli_env)
    result = _run(["flush", "--queue", "verify"], cli_env)
    assert result.exit_code == 0, result.output


def test_fix_dry_run_without_candidates_ok(cli_env: dict[str, str]) -> None:
    """没有候选条目时 fix 应安静通过（exit=0），不该报错。"""
    _run(["db", "init"], cli_env)
    result = _run(["fix", "--dry-run", "--limit", "1"], cli_env)
    assert result.exit_code == 0, result.output
    assert result.exception is None


def test_sandbox_check_runs(cli_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["sandbox", "check", "--config", cli_env["config"], "--env", cli_env["env"]])
    assert result.exit_code == 0, result.output
    assert "执行后端" in result.output


# ---------------------------------------------------------------------------
# 2. fix 一条候选都取不到时，必须说清卡在哪一步
# ---------------------------------------------------------------------------


def _seed_verified_tier2_issue(repo, sample_issue) -> None:
    """造出「已验证可复现、等批量 flush 提升」的 tier2 Issue。"""
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)
    repo.set_item_status(sample_issue.key, ItemStatus.QUEUED, priority=Priority.TIER2)
    entry = repo.enqueue(QueueName.VERIFY, sample_issue.key, payload={"stage": "verify"})
    repo.finish_queue_entry(entry, status="done", result={"verified": True})


def test_explain_no_fix_points_at_flush(repo, sample_issue) -> None:
    """卡在 `queued` 且验证结论尚未 flush 时，必须明确指出缺的是 flush。

    `fix` 只取 `fix_queued`，而把条目提升到该状态的是 **flush**。漏跑 flush 时
    输出只有「修复尝试 0」，用户无从判断是「没有值得修的条目」还是「漏了中间
    一步」——实测就这么白跑了一趟。
    """
    from types import SimpleNamespace

    from fissue.cli.ops import _explain_no_fix

    _seed_verified_tier2_issue(repo, sample_issue)

    msg = _explain_no_fix(SimpleNamespace(repo=repo), "psf/requests")

    assert msg, "应当给出说明"
    assert "flush" in msg
    assert "fissue flush --queue verify" in msg, msg


def test_explain_no_fix_without_tier_items(repo) -> None:
    """一条 tier1/tier2 都没有时，说的是「没有可修条目」，不该误报成缺 flush。"""
    from types import SimpleNamespace

    from fissue.cli.ops import _explain_no_fix

    msg = _explain_no_fix(SimpleNamespace(repo=repo), "psf/requests")

    assert msg and "没有" in msg
    assert "flush" not in msg, msg


def test_explain_no_fix_points_at_verify_when_only_evaluated(repo, sample_issue) -> None:
    """已评测、还没生成验证器时，指向 verify 这一步。"""
    from types import SimpleNamespace

    from fissue.cli.ops import _explain_no_fix

    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(rid, sample_issue)
    repo.set_item_status(sample_issue.key, ItemStatus.EVALUATED, priority=Priority.TIER2)

    msg = _explain_no_fix(SimpleNamespace(repo=repo), "psf/requests")

    assert msg and "verify" in msg, msg


def test_fix_without_candidates_explains_why(cli_env: dict[str, str]) -> None:
    """接线测试：空库上跑 fix，也要把「为什么没候选」打出来，而不是只报「修复尝试 0」。"""
    _run(["db", "init"], cli_env)
    result = _run(["fix", "--dry-run", "--limit", "1"], cli_env)

    assert result.exit_code == 0, result.output
    assert "修复尝试 0" in result.output
    assert "没有可自动修复的条目" in result.output, result.output


# ---------------------------------------------------------------------------
# requeue：把「只能人工放行」的条目放回流水线
# ---------------------------------------------------------------------------


def _cli_repo(env: dict[str, str]):
    """打开 CLI 用例那份库（用例里直接改状态用）。"""
    from fissue.config import load_settings
    from fissue.store.db import build_database
    from fissue.store.repository import Repository

    settings = load_settings(env["config"], env["env"])
    db = build_database(settings)
    db.create_all()
    return Repository(db)


def test_requeue_requires_a_selector(cli_env: dict[str, str]) -> None:
    """什么也不指定时要把「能选什么」说清楚，而不是闷头退出。"""
    result = _run(["requeue"], cli_env)

    assert result.exit_code != 0
    assert "--stale" in result.output and "--failed" in result.output


def test_requeue_stale_puts_item_back(cli_env: dict[str, str]) -> None:
    """`requeue --stale`：内容变了、结论过期、但修复已开工的条目，由人工放回重评。

    这类条目此前**没有任何入口**：自动回落重评会给「重复修复 / 重复建 PR」再开一道口子，
    所以那里只留了痕（`stale_reasons`，导出与 API 看得到）。
    """
    from fissue.models import ItemStatus, ItemType, Platform, RawItem, RepoRef

    key = _seed(cli_env["config"], cli_env["env"])          # 已评测（evaluated）
    repo = _cli_repo(cli_env)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.set_item_status(key, ItemStatus.FIXING)            # 修复已开工
    repo.upsert_item(                                       # 正文变了 → 结论作废，但只留痕
        rid,
        RawItem(platform=Platform.GITHUB, repo="psf/requests", number=42,
                item_type=ItemType.ISSUE, title="程序崩溃", body="boom 改过了",
                labels=["bug"], author="reporter"),
    )
    assert repo.get_item(key).stale_reasons, "前置：应当已留痕"
    assert repo.get_item_row(key).status == ItemStatus.FIXING.value, "前置：已开工不自动回落"

    result = _run(["requeue", "--stale", "--yes"], cli_env)

    assert result.exit_code == 0, result.output
    assert "已重排队 1 条" in result.output
    assert repo.get_item_row(key).status == ItemStatus.NEW.value, "应当放回重评"


def test_requeue_failed_falls_back_by_evidence(cli_env: dict[str, str]) -> None:
    """`requeue --failed`：按已有结论决定回退到哪一步。

    有评测 → `evaluated`（重跑验证）；尚无评测 → `new`（重跑评测）。此前只能逐条
    `fissue verify --key` 去捞失败条目。
    """
    from fissue.models import ItemStatus, ItemType, Platform, RawItem, RepoRef

    evaluated_key = _seed(cli_env["config"], cli_env["env"])     # 42：已有评测
    repo = _cli_repo(cli_env)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    plain = RawItem(platform=Platform.GITHUB, repo="psf/requests", number=43,
                    item_type=ItemType.ISSUE, title="另一条", body="b")
    repo.upsert_item(rid, plain)
    repo.set_item_status(evaluated_key, ItemStatus.FAILED)
    repo.set_item_status(plain.key, ItemStatus.FAILED)

    result = _run(["requeue", "--failed", "--yes"], cli_env)

    assert result.exit_code == 0, result.output
    assert repo.get_item_row(evaluated_key).status == ItemStatus.EVALUATED.value, "有结论的只重跑验证"
    assert repo.get_item_row(plain.key).status == ItemStatus.NEW.value, "没有结论的重跑评测"


def test_requeue_dry_run_changes_nothing(cli_env: dict[str, str]) -> None:
    """`--dry-run` 只列计划，不改状态。"""
    from fissue.models import ItemStatus

    key = _seed(cli_env["config"], cli_env["env"])
    repo = _cli_repo(cli_env)
    repo.set_item_status(key, ItemStatus.FAILED)

    result = _run(["requeue", "--failed", "--dry-run"], cli_env)

    assert result.exit_code == 0, result.output
    assert "未改动" in result.output
    assert repo.get_item_row(key).status == ItemStatus.FAILED.value, "dry-run 不许动状态"


# ---------------------------------------------------------------------------
# fetch / eval：成功路径与「--json 必须是纯 JSON」
# ---------------------------------------------------------------------------


def test_fetch_reports_stats(cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch 的收尾行要给出四个计数——否则「跑完了但到底有没有新东西」无从判断。"""
    from fissue.pipeline.fetcher import Fetcher, FetchStats

    async def fake_many(self: Any, repos: Any, **kwargs: Any) -> FetchStats:
        return FetchStats(repo="2 个仓库", fetched=7, created=3, updated=2, unchanged=2)

    monkeypatch.setattr(Fetcher, "fetch_many", fake_many)
    result = _run(["fetch", "--full", "--limit", "5"], cli_env)

    assert result.exit_code == 0, result.output
    assert "拉取 7" in result.output
    assert "新增 3" in result.output
    assert "更新 2" in result.output
    assert "未变 2" in result.output


def test_eval_on_empty_repo_says_nothing_to_do(cli_env: dict[str, str]) -> None:
    _run(["db", "init"], cli_env)
    result = _run(["eval"], cli_env)

    assert result.exit_code == 0, result.output
    assert "没有待评测条目" in result.output


def test_eval_json_stdout_is_pure_json(
    cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`eval --json` 的 stdout 必须能被 `json.loads` 直接吃掉。

    回归：摘要原先无条件 `emit(result.summary)`，于是 `--json` 前面多一行中文，
    消费方拿到的是字符串拼接而不是 JSON——和 `status --json` 已经钉住的口径相反。
    """
    from types import SimpleNamespace

    import fissue.pipeline.stages as stages

    repo = _cli_repo(cli_env)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(
        rid,
        RawItem(
            platform=Platform.GITHUB, repo="psf/requests", number=43,
            item_type=ItemType.ISSUE, title="另一条", body="b",
        ),
    )

    class FakeEvalStage:
        def __init__(self, ctx: Any) -> None:
            self.ctx = ctx

        async def run_batch(self, items: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(summary="完成 1 条", results=[])

    monkeypatch.setattr(stages, "EvalStage", FakeEvalStage)
    result = _run(["eval", "--json"], cli_env)

    assert result.exit_code == 0, result.output
    # `result.output` 在 Click 8.2+ 会混合 stderr（日志）；机器可读口径看 stdout。
    assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# force：旁路闸门的手动放行，必须可预览、可撤销、且不误伤 PR
# ---------------------------------------------------------------------------


def test_force_requires_a_key(cli_env: dict[str, str]) -> None:
    result = _run(["force"], cli_env)

    assert result.exit_code != 0
    assert "--key" in result.output


def test_force_dry_run_does_not_touch_item(cli_env: dict[str, str]) -> None:
    key = _seed(cli_env["config"], cli_env["env"])
    repo = _cli_repo(cli_env)
    before = repo.get_item_row(key).status

    result = _run(["force", "--key", key, "--dry-run"], cli_env)

    assert result.exit_code == 0, result.output
    assert "未改动" in result.output
    assert repo.get_item_row(key).status == before
    assert repo.get_item(key).force_fix is False, "dry-run 不许置标记"


def test_force_rejects_prs(cli_env: dict[str, str]) -> None:
    """PR 没有自动修复流程：点到了也要说清并跳过，而不是硬塞进修复队列。"""
    repo = _cli_repo(cli_env)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    pr = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=7,
        item_type=ItemType.PR, title="一个 PR", body="b",
    )
    repo.upsert_item(rid, pr)

    result = _run(["force", "--key", pr.key], cli_env)

    assert result.exit_code == 0, result.output
    assert "PR 没有自动修复流程" in result.output
    assert repo.get_item(pr.key).force_fix is False


def test_force_undo_clears_the_flag(cli_env: dict[str, str]) -> None:
    """`--undo` 是强制修复的退路：标记必须清掉，否则条目会一直绕过闸门。"""
    key = _seed(cli_env["config"], cli_env["env"])
    repo = _cli_repo(cli_env)
    repo.set_force_fix(key, True)

    result = _run(["force", "--key", key, "--undo", "--yes"], cli_env)

    assert result.exit_code == 0, result.output
    assert "已取消强制" in result.output
    assert repo.get_item(key).force_fix is False


def test_force_runs_verify_after_setting_the_flag(
    cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """标记置上后必须真的走一遍验证阶段（顺带补齐验证器），而不是只改状态就结束。"""
    from types import SimpleNamespace

    import fissue.pipeline.stages as stages

    key = _seed(cli_env["config"], cli_env["env"])
    repo = _cli_repo(cli_env)
    seen: list[str] = []

    class FakeVerifyStage:
        def __init__(self, ctx: Any) -> None:
            self.ctx = ctx

        async def run(self, item: Any, evaluation: Any) -> Any:
            seen.append(item.key)
            return SimpleNamespace(notes=[])

    monkeypatch.setattr(stages, "VerifyStage", FakeVerifyStage)
    result = _run(["force", "--key", key, "--yes"], cli_env)

    assert result.exit_code == 0, result.output
    assert seen == [key]
    assert repo.get_item(key).force_fix is True
    assert "状态 →" in result.output


def test_force_asks_before_bypassing_gates(cli_env: dict[str, str]) -> None:
    """没给 --yes 时必须先问；答「不」就一个字段都不动。"""
    key = _seed(cli_env["config"], cli_env["env"])
    repo = _cli_repo(cli_env)

    result = runner.invoke(
        app,
        ["force", "--key", key, "--config", cli_env["config"], "--env", cli_env["env"]],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "已取消" in result.output
    assert repo.get_item(key).force_fix is False


# ---------------------------------------------------------------------------
# bench：基线是「可复现跑分」的证据，写读比都不能含糊
# ---------------------------------------------------------------------------


def test_bench_capture_list_and_self_diff(cli_env: dict[str, str]) -> None:
    captured = _run(["bench", "capture", "--label", "baseline-a"], cli_env)
    assert captured.exit_code == 0, captured.output

    path = Path(cli_env["root"]) / "data" / "benchmarks" / "baseline-a.json"
    assert path.exists(), "capture 必须真的落盘"

    listed = _run(["bench", "list", "--json"], cli_env)
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert any(row["file"] == "baseline-a.json" for row in rows)

    # 同一份基线自比：可比且无变化 → 0。先把它标成「工作区干净」，
    # 否则 diff 会按「存在未提交改动」判为不可比（这是 diff 刻意的保守口径）。
    from fissue import benchmark as bm

    record = bm.load(path)
    record["provenance"]["git"]["dirty"] = False
    record["provenance"]["git"]["dirty_files"] = 0
    bm.save(record, path)

    diff = _run(["bench", "diff", str(path), str(path)], cli_env)
    assert diff.exit_code == 0, diff.output
    assert "可比" in diff.output


def test_bench_diff_exit_codes_express_comparability(cli_env: dict[str, str]) -> None:
    """退出码就是结论：0 可比无变化 / 1 不可比 / 2 可比有变化。

    把「不可比」也当成 0 或 2，下游脚本就会拿两次前提不同的测量当回归依据——
    这正是 BENCHMARK §7.2 那条方法学局限要机械表达的东西。
    """
    from fissue import benchmark as bm

    assert _run(["bench", "capture", "--label", "a"], cli_env).exit_code == 0
    base = Path(cli_env["root"]) / "data" / "benchmarks" / "a.json"
    record = bm.load(base)
    # 工作区脏会先被判成「不可比」（diff 的保守口径）；这里要单独验「可比但变化」，
    # 所以先把前提标干净，而不是指望测试环境恰好是干净的。
    record["provenance"]["git"]["dirty"] = False
    record["provenance"]["git"]["dirty_files"] = 0
    bm.save(record, base)

    incomparable = json.loads(json.dumps(record))
    incomparable["provenance"]["git"]["commit"] = "0" * 40
    p1 = base.parent / "b.json"
    bm.save(incomparable, p1)
    r1 = _run(["bench", "diff", str(base), str(p1)], cli_env)
    assert r1.exit_code == 1, r1.output
    assert "不可比" in r1.output

    changed = json.loads(json.dumps(record))
    changed["result"]["items"]["total"] = int(changed["result"]["items"]["total"]) + 1
    p2 = base.parent / "c.json"
    bm.save(changed, p2)
    r2 = _run(["bench", "diff", str(base), str(p2)], cli_env)
    assert r2.exit_code == 2, r2.output


def test_bench_list_survives_a_broken_baseline(cli_env: dict[str, str]) -> None:
    """一个坏文件不该让整个列表失败——否则「有一份基线写坏了」= 其余都看不见。"""
    directory = Path(cli_env["root"]) / "data" / "benchmarks"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.json").write_text("{not json", encoding="utf-8")

    result = _run(["bench", "list", "--json"], cli_env)

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert any(row["file"] == "broken.json" and row["label"] == "(无法解析)" for row in rows)
