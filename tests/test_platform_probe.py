"""`tools/platform_probe.py` 的关键分支：结果记账、缺令牌/令牌无效的早停。

为什么值得测：这个脚本是「某个平台到底能不能提 PR」的唯一实测工具，而它**至今没有
成功跑通过一次**（两枚令牌都失效）。没有令牌就无法端到端验证，但至少要把「跑起来
会崩」和「失败说不清原因」这两类问题挡住——它们与令牌无关，纯粹是脚本自己的逻辑。

真实网络与 git 操作一律不碰：需要 settings 与 adapter 的地方都用替身。
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "platform_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("platform_probe_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_module()


# ---------------------------------------------------------------------------
# 结果记账
# ---------------------------------------------------------------------------


def test_render_marks_each_status() -> None:
    report = probe.Probe()
    report.ok("只读抓取", "读到 3 条")
    report.fail("提 PR", "PlatformError: 403")
    report.skip("评论", "仓库里没有条目")

    text = report.render()
    assert "✅" in text and "❌" in text and "⏭" in text
    assert "只读抓取" in text and "提 PR" in text
    assert report.failed == 1


def test_fail_truncates_and_flattens_detail() -> None:
    """失败原因是多行堆栈时不能把整张表撑散。"""
    report = probe.Probe()
    report.fail("克隆", "line1\nline2\n" + "x" * 500)
    _name, _status, detail = report.rows[0]
    assert "\n" not in detail
    assert len(detail) <= 160


@pytest.mark.asyncio
async def test_run_records_exception_instead_of_raising() -> None:
    """探针的意义是一次暴露所有问题——单项异常必须就地记账，不能中断整轮。"""

    async def boom():
        raise RuntimeError("network down")

    report = probe.Probe()
    ok = await report.run("克隆仓库", boom())
    assert ok is False
    assert report.failed == 1
    assert "network down" in report.rows[0][2]


@pytest.mark.asyncio
async def test_run_then_continue_after_failure() -> None:
    async def boom():
        raise RuntimeError("x")

    async def fine():
        return "ok"

    report = probe.Probe()
    await report.run("第一项", boom())
    await report.run("第二项", fine())
    assert [status for _, status, _ in report.rows] == ["FAIL", "PASS"]


def test_probe_item_is_minimal_issue_stand_in() -> None:
    item = probe._ProbeItem(7)
    assert item.number == 7
    assert item.item_type.value == "issue"


# ---------------------------------------------------------------------------
# 早停：令牌缺失 / 令牌无效
# ---------------------------------------------------------------------------


class _Tokens:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self, platform):  # noqa: ARG002 - 只需要形状
        return self._value


class _Settings:
    def __init__(self, token: str | None) -> None:
        self.tokens = _Tokens(token)


def _args(**kwargs) -> argparse.Namespace:
    base = {
        "platform": "gitee",
        "owner": None,
        "repo": "fissue-platform-probe",
        "create": False,
        "config": None,
        "env": None,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_main_exits_2_when_token_missing(monkeypatch, capsys) -> None:
    """没有令牌时要说清楚该配哪个键——而不是抛一堆无关报错。"""
    monkeypatch.setattr(probe, "load_settings", lambda *a, **k: _Settings(None))
    rc = probe.main(["--platform", "gitee"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "FISSUE_GITEE_TOKEN" in out


@pytest.mark.asyncio
async def test_token_invalid_stops_early_with_actionable_hint(monkeypatch, capsys) -> None:
    """令牌无效时只报那一项就停：后面所有项都依赖它，继续跑只会刷屏。"""

    class _Adapter:
        api_base = "https://example.invalid/api/v5"

        async def whoami(self):
            raise RuntimeError("401 Unauthorized: Access token is expired")

    monkeypatch.setattr(probe, "load_settings", lambda *a, **k: _Settings("t"))
    monkeypatch.setattr(probe, "build_adapter", lambda *a, **k: _Adapter())

    rc = await probe.probe_platform(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "令牌有效" in out
    assert "expired" in out
    # 必须只报这一项：没有继续往建仓库/克隆那条路上跑
    assert "创建探针仓库" not in out
    assert "FISSUE_GITEE_TOKEN" in out


@pytest.mark.asyncio
async def test_no_repo_configured_explains_next_step(monkeypatch, capsys) -> None:
    """令牌有效但没指定仓库：要告诉用户用 --create 或 --owner，而不是静默返回。"""

    class _Adapter:
        api_base = "https://example.invalid/api/v5"

        async def whoami(self):
            return "someone"

    monkeypatch.setattr(probe, "load_settings", lambda *a, **k: _Settings("t"))
    monkeypatch.setattr(probe, "build_adapter", lambda *a, **k: _Adapter())

    rc = await probe.probe_platform(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "--create" in out and "--owner" in out
