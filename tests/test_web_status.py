"""状态聚合层（``web/status.py``）的测试。

这一层回答的是「**这台机器现在配置对不对**」。它最容易出两类问题：

* **误报**：报了一堆其实没问题的告警，用户就不看了；
* **漏报**：真缺了东西却不说，新手会卡在「功能不 work」而找不到原因。

所以每条告警都钉住「**在什么条件下出现**」——既钉住它该出现，也钉住它**不该**出现
（后者更关键：一味乱报的告警比没有告警更糟）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fissue.web.status import _alerts, _backend_of, mask_url

# ---------------------------------------------------------------------------
# mask_url：口令与查询串值要遮住，其余逐字节保留
# ---------------------------------------------------------------------------


def test_mask_url_hides_password_but_keeps_host() -> None:
    assert mask_url("postgresql://u:pw@h:5432/db") == "postgresql://u:***@h:5432/db"


def test_mask_url_hides_query_values_but_keeps_keys() -> None:
    """查询串的值整体遮掉：`?key=xxx` 这种写法真的有人用（LLM 端点）。"""
    assert mask_url("https://api.example.com/v1?api_key=sk-secret") == (
        "https://api.example.com/v1?api_key=***"
    )


def test_mask_url_keeps_bare_query_flags() -> None:
    """没有 `=` 的查询参数是开关，不是值——遮掉它反而看不出端点配置。"""
    assert mask_url("https://x/v1?debug") == "https://x/v1?debug"


def test_mask_url_keeps_sqlite_path_intact() -> None:
    """SQLite 的前导双斜杠不能被吞——规范化会把库路径显示坏。"""
    assert mask_url("sqlite:///E:/x.db?mode=rw") == "sqlite:///E:/x.db?mode=***"
    assert mask_url("sqlite:///E:/Fissue/fissue.db") == "sqlite:///E:/Fissue/fissue.db"


def test_mask_url_without_scheme_is_returned_as_is() -> None:
    assert mask_url("not a url") == "not a url"


def test_mask_url_empty() -> None:
    assert mask_url("") == ""


# ---------------------------------------------------------------------------
# _backend_of：数据库后端名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///E:/x.db", "sqlite"),
        ("sqlite+aiosqlite:///x.db", "sqlite"),
        ("postgresql+psycopg://u:pw@h/db", "postgresql"),
        ("postgres://h/db", "postgresql"),
        ("mysql://h/db", "mysql"),
        ("mysql+pymysql://u:pw@h:3306/db", "mysql"),   # 驱动名不该混进「用的是哪种库」的答案
        ("", "unknown"),
    ],
)
def test_backend_of(url: str, expected: str) -> None:
    """后端名要认得出驱动前缀——用户据此判断「我连的是哪个库」。"""
    assert _backend_of(url) == expected


# ---------------------------------------------------------------------------
# _alerts：每条告警都要「可操作」
# ---------------------------------------------------------------------------


def _settings(**kw):
    """一份默认「配置齐全」的 Settings，再按用例覆盖。"""
    from fissue.config import Settings

    s = Settings()
    s.llm.api_key = "sk-present"
    s.sandbox.enabled = True
    s.web.auth.enabled = True
    s.web.host = "127.0.0.1"
    s.web.trusted_networks = []
    s.repos = []
    s.tokens.github = "tok"
    for key, value in kw.items():
        setattr(s, key, value)
    return s


def _messages(alerts) -> str:
    return " | ".join(a["message"] for a in alerts)


def test_alerts_are_empty_for_a_healthy_setup() -> None:
    """配置齐全时**不该**有告警——一味乱报比不报更糟。"""
    alerts = _alerts(
        _settings(repos=[SimpleNamespace(enabled=True)]),
        usage={"calls": 0},
        locks_ok=True,
    )
    assert alerts == [], _messages(alerts)


def test_alerts_report_missing_llm_key() -> None:
    s = _settings()
    s.llm.api_key = ""
    alerts = _alerts(s, usage={"calls": 0}, locks_ok=True)
    assert any("LLM API Key" in a["message"] for a in alerts)
    assert any(a["level"] == "error" for a in alerts)


def test_alerts_report_missing_repos() -> None:
    alerts = _alerts(_settings(), usage={"calls": 0}, locks_ok=True)
    assert any("监控仓库" in a["message"] for a in alerts)


def test_alerts_report_missing_platform_tokens() -> None:
    s = _settings()
    s.tokens.github = ""
    s.tokens.gitee = ""
    s.tokens.atomgit = ""
    s.tokens.gitlab = ""
    alerts = _alerts(s, usage={"calls": 0}, locks_ok=True)
    assert any("平台 Token" in a["message"] for a in alerts)


def test_alerts_report_disabled_sandbox() -> None:
    s = _settings()
    s.sandbox.enabled = False
    alerts = _alerts(s, usage={"calls": 0}, locks_ok=True)
    assert any("沙盒已关闭" in a["message"] for a in alerts)


def test_alerts_report_stale_conclusions() -> None:
    """结论过期是「内容改过、当时在执行」——要告诉用户怎么放回流水线。"""
    alerts = _alerts(_settings(), usage={"calls": 0}, locks_ok=True, stale_count=3)
    hit = [a for a in alerts if "结论已过期" in a["message"]]
    assert hit and "3" in hit[0]["message"]
    assert "requeue" in hit[0]["hint"], "每条告警都要带「怎么办」"


def test_alerts_report_over_budget() -> None:
    """超出单仓库日预算要报出来，并给出可操作的下一步。"""
    s = _settings()
    s.llm.budget.daily_usd_per_repo = 1.0
    alerts = _alerts(
        s,
        usage={"calls": 3, "total_tokens": 100, "cost_usd": 2.5},
        locks_ok=True,
    )
    assert any("花费" in a["message"] for a in alerts)


def test_alert_every_entry_carries_a_hint() -> None:
    """每条告警都要有 hint（怎么办）——只说「未配置」等于没说。"""
    s = _settings()
    s.llm.api_key = ""
    s.sandbox.enabled = False
    s.tokens.github = ""
    alerts = _alerts(s, usage={"calls": 0}, locks_ok=True, stale_count=1)
    assert alerts
    for a in alerts:
        assert a["hint"], f"这条没有 hint：{a['message']}"
        assert a["level"] in {"error", "warn", "info"}


def test_alerts_report_missing_flock_support() -> None:
    """本平台没有 flock 时要提醒——否则会重复评测、重复计费。"""
    alerts = _alerts(_settings(), usage={"calls": 0}, locks_ok=False)
    assert any("flock" in a["message"] for a in alerts)


def test_alerts_report_exposed_host_without_trusted_networks() -> None:
    """绑到非回环地址却没声明可信网段：内网浏览器得先登录。"""
    s = _settings()
    s.web.host = "0.0.0.0"
    s.web.auth.enabled = True
    alerts = _alerts(s, usage={"calls": 0}, locks_ok=True)
    assert any("可信网段" in a["message"] for a in alerts)


def test_alerts_report_auth_disabled_and_no_trusted_network() -> None:
    """鉴权全关且没有可信网段：除回环外一律被拒。"""
    s = _settings()
    s.web.auth.enabled = False
    s.web.trusted_networks = []
    alerts = _alerts(s, usage={"calls": 0}, locks_ok=True)
    assert any("会话鉴权已关闭" in a["message"] for a in alerts)


def test_alerts_do_not_report_auth_problem_when_trusted_network_exists() -> None:
    """声明了可信网段就不该再报「鉴权全关」——那条这时不成立。"""
    s = _settings()
    s.web.auth.enabled = False
    s.web.trusted_networks = ["192.168.1.0/24"]
    alerts = _alerts(s, usage={"calls": 0}, locks_ok=True)
    assert not any("会话鉴权已关闭" in a["message"] for a in alerts), _messages(alerts)


# ---------------------------------------------------------------------------
# _sandbox_section：探测与缓存
# ---------------------------------------------------------------------------


def _app():
    """一个最小的 app 替身：只需要 state 能放属性。"""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.sandbox_probe = None
    return app


def _sandbox_settings(*, enabled: bool = True):
    from fissue.config import Settings

    s = Settings()
    s.sandbox.enabled = enabled
    return s


def test_sandbox_section_is_not_probed_by_default() -> None:
    """默认不探测：每 2 秒的轮询不该去敲 Docker。"""
    from fissue.web.status import _sandbox_section

    out = _sandbox_section(_app(), _sandbox_settings(), probe=False)
    assert out["ready"] is None
    assert "未探测" in out["reason"]


def test_sandbox_section_reuses_the_cached_probe() -> None:
    """普通轮询要**沿用**上一次探测结果。

    探测后紧接着的下一轮普通轮询若把结果丢回「未探测」，用户刚看到的结果就被抹掉了
    （真机实炸过）。
    """
    from fissue.web.status import _sandbox_section

    app = _app()
    app.state.sandbox_probe = {"ready": True, "reason": "镜像就绪", "at": "2026-10-29T10:00:00+00:00"}

    out = _sandbox_section(app, _sandbox_settings(), probe=False)
    assert out["ready"] is True
    assert out["reason"] == "镜像就绪"
    assert out["probed_at"] == "2026-10-29T10:00:00+00:00"


def test_sandbox_section_reports_disabled_sandbox() -> None:
    """沙盒关掉时要说明「验证器无法运行」——那是最直接的因果。"""
    from fissue.web.status import _sandbox_section

    out = _sandbox_section(_app(), _sandbox_settings(enabled=False), probe=True)
    assert out["ready"] is None
    assert "sandbox.enabled=false" in out["reason"]


def test_sandbox_section_probe_failure_is_part_of_the_status(monkeypatch) -> None:
    """探测抛异常不是崩溃——「探测失败」本身就是状态的一部分，要显示出来。"""
    from fissue.web.status import _sandbox_section

    class _Boom:
        def __init__(self, sb):  # noqa: ANN001, ANN204
            pass

        def available(self):  # noqa: ANN201
            raise RuntimeError("docker 守护进程没起来")

    monkeypatch.setattr("fissue.sandbox.runtime.DockerRuntime", _Boom)

    app = _app()
    out = _sandbox_section(app, _sandbox_settings(), probe=True)

    assert out["ready"] is False
    assert "RuntimeError" in out["reason"] and "docker 守护进程没起来" in out["reason"]
    assert out["probed_at"], "探测时间要记下来（结果可能过期）"
    # 结果要缓存住：下一轮普通轮询直接复用，不再敲 Docker。
    assert app.state.sandbox_probe["ready"] is False
