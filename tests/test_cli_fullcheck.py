"""``fissue doctor --full``（全栈自检）的单元测试。

体检命令的价值全在「说真话 + 说下一步」上，所以这里断言的重点不是措辞，而是：

* 七项一个不少、每项都带 ``next_step``（没有下一步的警告等于没说）；
* 判据的**等级**对不对（没配 key 是 warn 还是 bad，直接决定退出码会不会把人吓到）；
* 探测不落到真实网络 / 真实 Docker 上（本文件全程打桩，conftest 另有网络守卫）；
* 有 bad 时退出码非 0。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from fissue.cli import fullcheck
from fissue.cli.fullcheck import BAD, OK, WARN, CheckResult, collect_checks, render
from fissue.config import Settings

#: 七个检查项，顺序即输出顺序。
CHECKS = ("配置加载", "数据库", "模型", "沙盒", "Web", "磁盘与数据目录", "时区与语言环境")


@pytest.fixture(autouse=True)
def _offline_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「碰外部世界」的探测换成确定值：DNS/TCP、docker、unix socket。

    端口那一项**不打桩**：它只在本机 bind 一个端口，而那正是被测行为本身。
    """
    monkeypatch.setattr(fullcheck, "_tcp_probe", lambda base_url: (True, "TCP 模拟可达（未发请求）"))
    monkeypatch.setattr(fullcheck, "_docker_probe", lambda: (True, "Docker 24.0（模拟）"))
    monkeypatch.setattr(fullcheck, "_probe_unix_socket", lambda path: (True, ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_settings(
    tmp_path: Path, *, port: int | None = None, api_key: str = "sk-live-8f3a91c2d4e5"
) -> Settings:
    """一份完全离线的配置：临时 SQLite + 临时数据目录 + 一个空闲端口。"""
    return Settings(
        database={"url": f"sqlite:///{(tmp_path / 'fissue.db').as_posix()}"},
        llm={"api_key": api_key, "base_url": "https://llm.invalid/v1", "model": "test-model"},
        sandbox={"runtime": "docker"},
        app={"data_dir": str(tmp_path / "data")},
        web={"port": port if port is not None else free_port()},
    )


def find(results: list[CheckResult], name: str) -> CheckResult:
    for item in results:
        if item.name == name:
            return item
    raise AssertionError(f"没有这项检查：{name}")


# ---------------------------------------------------------------------------
# 覆盖面
# ---------------------------------------------------------------------------


def test_collect_checks_covers_all_seven(tmp_path: Path) -> None:
    results = collect_checks(make_settings(tmp_path))
    assert [item.name for item in results] == list(CHECKS)
    for item in results:
        assert item.level in {OK, WARN, BAD}
        assert item.detail.strip(), f"{item.name} 没有 detail"
        assert item.next_step.strip(), f"{item.name} 没有 next_step（没有下一步的结论等于没说）"


def test_one_broken_probe_does_not_kill_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> tuple[bool | None, str]:
        raise RuntimeError("探测自己炸了")

    monkeypatch.setattr(fullcheck, "_docker_probe", boom)
    results = collect_checks(make_settings(tmp_path))
    assert len(results) == 7
    sandbox = find(results, "沙盒")
    assert sandbox.level == BAD
    assert "检查本身出错" in sandbox.detail


# ---------------------------------------------------------------------------
# 配置加载 / 密钥
# ---------------------------------------------------------------------------


def test_placeholder_platform_token_is_bad(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.tokens.github = "ghp_REPLACE-ME"
    result = find(collect_checks(settings), "配置加载")
    assert result.level == BAD
    assert "FISSUE_GITHUB_TOKEN" in result.detail
    assert "FISSUE_GITHUB_TOKEN" in result.next_step


def test_secret_masking_and_placeholder_detection() -> None:
    masked = fullcheck._mask_secret("sk-live-8f3a91c2d4e5")
    assert "8f3a91c2" not in masked
    assert fullcheck._is_placeholder("sk-REPLACE-ME")
    assert not fullcheck._is_placeholder("sk-live-8f3a91c2d4e5")
    assert "sup3rsecret" not in fullcheck._mask_dsn("postgresql+psycopg://u:sup3rsecret@h:5432/db")


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------


def test_database_unwritable_path_is_bad(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite:///{(blocker / 'sub' / 'fissue.db').as_posix()}"
    result = find(collect_checks(settings), "数据库")
    assert result.level == BAD
    assert "FISSUE_DATABASE_URL" in result.next_step


def test_database_failure_never_leaks_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    secret = "sup3rsecret"
    settings.database.url = f"postgresql+psycopg://fissue:{secret}@db.internal:5432/fissue"

    def boom(_settings: Settings) -> object:
        # 真实驱动也这么干：把整条 DSN 原样塞进异常文本。
        raise RuntimeError(f"could not connect: {_settings.database.url}")

    monkeypatch.setattr("fissue.store.db.build_database", boom)
    result = find(collect_checks(settings), "数据库")
    assert result.level == BAD
    assert secret not in result.detail
    assert "db.internal" in result.detail


def test_database_ok_path_reports_schema_and_admin_state(tmp_path: Path) -> None:
    from fissue.store.db import Database

    settings = make_settings(tmp_path)
    db = Database(settings.database.url)
    db.create_all()
    results = collect_checks(settings)
    database = find(results, "数据库")
    assert database.level == OK
    assert "schema v" in database.detail
    # 表建好了但还没有管理员账号 → Web 那项要给 warn 并指到引导流程。
    web = find(results, "Web")
    assert web.level == WARN
    assert "还没有管理员账号" in web.detail


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


def test_llm_key_missing_is_warn_other_key_is_bad(tmp_path: Path) -> None:
    missing = find(collect_checks(make_settings(tmp_path, api_key="")), "模型")
    assert missing.level == WARN, "没配 key 是合法配置，不能算失败"

    placeholder = find(collect_checks(make_settings(tmp_path, api_key="sk-REPLACE-ME")), "模型")
    assert placeholder.level == BAD


# ---------------------------------------------------------------------------
# Web 端口
# ---------------------------------------------------------------------------


def test_web_port_busy_is_bad(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = int(busy.getsockname()[1])
        result = find(collect_checks(make_settings(tmp_path, port=port)), "Web")
    assert result.level == BAD
    assert str(port) in result.detail
    assert "FISSUE_WEB_PORT" in result.next_step
    assert "taskkill" in result.next_step or "netstat" in result.next_step


# ---------------------------------------------------------------------------
# 渲染与退出码
# ---------------------------------------------------------------------------


def test_render_exit_code(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert render(settings, []) == 0
    assert render(settings, [CheckResult("配置加载", OK, "都正常", "无需处理")]) == 0
    good = CheckResult("配置加载", OK, "都正常", "无需处理")
    bad = CheckResult("数据库", BAD, "连不上", "检查 FISSUE_DATABASE_URL")
    assert render(settings, [good, bad]) == 1


def test_render_summary_and_ordered_steps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    results = [
        CheckResult("配置加载", OK, "都正常", "无需处理"),
        CheckResult("模型", WARN, "key 没配", "配上 FISSUE_LLM_API_KEY"),
        CheckResult("数据库", BAD, "连不上", "第一步：检查 FISSUE_DATABASE_URL"),
        CheckResult("沙盒", BAD, "docker 没起", "第二步：装 Docker"),
    ]
    assert render(settings, results) == 1
    out = capsys.readouterr().out
    assert "1 项通过 / 1 项警告 / 2 项失败" in out
    assert "按顺序执行下面几步" in out


def test_ordered_steps_dedupes_and_keeps_check_order() -> None:
    """失败项的「下一步」要按检查顺序给，且同一句只出现一次；warn 不混进来。"""
    failed = [
        CheckResult("数据库", BAD, "连不上", "先看库"),
        CheckResult("沙盒", BAD, "docker 没起", "再装 Docker"),
        CheckResult("Web", BAD, "端口被占", "先看库"),
    ]
    assert fullcheck._ordered_steps(failed) == ["先看库", "再装 Docker"]
    assert fullcheck._ordered_steps([]) == []


def test_bounded_gives_up_on_slow_probe() -> None:
    def slow() -> str:
        time.sleep(5)
        return "太晚了"

    value, note = fullcheck._bounded(slow, 0.05, timeout_note="超时了（已跳过）")
    assert value is None
    assert "跳过" in note
