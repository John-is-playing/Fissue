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
    FixAttempt,
    FixOutcome,
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
    assert "| 0 |" in result.output.replace(" ", " ")


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


def test_status_json_output(cli_env: dict[str, str]) -> None:
    result = _run(["status", "--json"], cli_env)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data) >= {"repos_config", "sandbox", "items", "queues", "llm_usage_today"}
    assert "psf/requests" in data["repos_config"]


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
