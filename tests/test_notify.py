"""通知渠道与分发器测试。

`notify.py` 是「出问题最难发现」的那类代码：它只在**出事故时**才被执行，而事故现场
（webhook 挂了、SMTP 密码过期、去重表写不进去）恰恰是最需要它把话说清楚的时候。
所以这里逐个渠道守住三件事：未配置时报什么、成功时发出去的 payload 长什么样、
失败时返回的是 `(False, 原因)` 而不是抛异常或静默成功。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from fissue.config import NotifyChannel, NotifyConfig
from fissue.models import DimensionScore, Evaluation, Scores
from fissue.service.notify import (
    ConsoleChannel,
    DingTalkChannel,
    EmailChannel,
    FeishuChannel,
    Notification,
    Notifier,
    WebhookChannel,
    WeComChannel,
    _periodic_scope,
)

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _note(**kw: Any) -> Notification:
    data: dict[str, Any] = {
        "event": "high_importance",
        "title": "测试通知",
        "body": "正文",
        "item_key": "github:a/b#1",
    }
    data.update(kw)
    return Notification(**data)


def _ch(kind: str, **kw) -> NotifyChannel:
    return NotifyChannel(type=kind, **kw)


def _eval(importance: int = 95, *, summary: str = "看起来是真 Bug") -> Evaluation:
    from fissue.models import Priority

    return Evaluation(
        scores=Scores(
            importance=DimensionScore(score=importance),
            authenticity=DimensionScore(score=80),
            difficulty=DimensionScore(score=20),
        ),
        priority=Priority.TIER1,
        summary=summary,
        model="stub",
    )


# ---------------------------------------------------------------------------
# 基类与 console
# ---------------------------------------------------------------------------


def test_text_joins_title_body_url_in_that_order() -> None:
    """`_text` 是各渠道的共用正文拼装：顺序影响可读性，不该被随手改。"""
    ch = ConsoleChannel(_ch("console"))
    assert ch._text(_note()) == "测试通知\n正文"
    assert ch._text(_note(url="https://x/1")) == "测试通知\n正文\nhttps://x/1"
    assert ch._text(_note(body="")) == "测试通知"


async def test_console_channel_prints_level_mark(capsys) -> None:
    ch = ConsoleChannel(_ch("console"))
    ok, err = await ch.send(_note(level="critical"))
    assert (ok, err) == (True, "")
    out = capsys.readouterr().out
    assert "🚨" in out and "[high_importance]" in out


# ---------------------------------------------------------------------------
# webhook（含 HMAC 签名）
# ---------------------------------------------------------------------------


@respx.mock
async def test_webhook_channel_posts_payload_with_signature() -> None:
    """配了 secret 就要带签名头，且签名必须能对上实际发出的字节。"""
    import hashlib
    import hmac

    url = "https://hooks.example.com/fissue"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))
    ch = WebhookChannel(_ch("webhook", url=url, secret="s3cr3t"))

    ok, err = await ch.send(_note(url="https://gh/1", payload={"a": 1}))

    assert (ok, err) == (True, "")
    request = route.calls.last.request
    raw = request.content
    body = json.loads(raw)
    assert body["event"] == "high_importance"
    assert body["payload"] == {"a": 1}
    assert body["timestamp"] > 0
    expected = hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
    assert request.headers["X-Fissue-Signature"] == f"sha256={expected}"


@respx.mock
async def test_webhook_channel_without_secret_omits_signature() -> None:
    url = "https://hooks.example.com/fissue"
    route = respx.post(url).mock(return_value=httpx.Response(200))
    ch = WebhookChannel(_ch("webhook", url=url))

    assert (await ch.send(_note()))[0] is True
    assert "X-Fissue-Signature" not in route.calls.last.request.headers


async def test_webhook_channel_requires_url() -> None:
    ok, err = await WebhookChannel(_ch("webhook")).send(_note())
    assert ok is False and "url" in err


@respx.mock
async def test_webhook_channel_reports_http_error_with_body_snippet() -> None:
    """失败必须把状态码与响应片段带回来——否则运维只能看到一个「发送失败」。"""
    url = "https://hooks.example.com/fissue"
    respx.post(url).mock(return_value=httpx.Response(500, text="internal boom"))
    ok, err = await WebhookChannel(_ch("webhook", url=url)).send(_note())
    assert ok is False
    assert "500" in err and "internal boom" in err


@respx.mock
async def test_webhook_channel_reports_network_error() -> None:
    url = "https://hooks.example.com/fissue"
    respx.post(url).mock(side_effect=httpx.ConnectError("dns down"))
    ok, err = await WebhookChannel(_ch("webhook", url=url)).send(_note())
    assert ok is False and "网络错误" in err


# ---------------------------------------------------------------------------
# 企业微信 / 钉钉 / 飞书
# ---------------------------------------------------------------------------


async def test_wecom_requires_url() -> None:
    ok, err = await WeComChannel(_ch("wecom")).send(_note())
    assert ok is False and "企业微信" in err


@respx.mock
async def test_wecom_sends_markdown_with_link() -> None:
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    ok, err = await WeComChannel(_ch("wecom", url=url)).send(_note(url="https://gh/1"))

    assert (ok, err) == (True, "")
    body = json.loads(route.calls.last.request.content)
    assert body["msgtype"] == "markdown"
    assert "[查看](https://gh/1)" in body["markdown"]["content"]


@respx.mock
async def test_wecom_surfaces_http_failure_from_post_json() -> None:
    """走 `_post_json` 的渠道：HTTP 4xx 要转成失败，而不是当成发成功。"""
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"
    respx.post(url).mock(return_value=httpx.Response(401, text="invalid key"))
    ok, err = await WeComChannel(_ch("wecom", url=url)).send(_note())
    assert ok is False and "401" in err


@respx.mock
async def test_wecom_surfaces_network_error_from_post_json() -> None:
    """`_post_json` 走同步 httpx：连接失败要变成 `(False, 网络错误)` 而不是抛出去。"""
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"
    respx.post(url).mock(side_effect=httpx.ConnectError("no route"))
    ok, err = await WeComChannel(_ch("wecom", url=url)).send(_note())
    assert ok is False and "网络错误" in err


async def test_dingtalk_requires_url() -> None:
    ok, err = await DingTalkChannel(_ch("dingtalk")).send(_note())
    assert ok is False and "钉钉" in err


def test_dingtalk_signed_url_appends_timestamp_and_sign() -> None:
    """加签 URL 的拼接：已有 query 时用 `&`，没有时用 `?`。"""
    ch = DingTalkChannel(_ch("dingtalk", url="https://oapi.dingtalk.com/robot/send?access_token=t", secret="sec"))
    signed = ch._signed_url()
    assert signed.startswith("https://oapi.dingtalk.com/robot/send?access_token=t&timestamp=")
    assert "&sign=" in signed

    ch2 = DingTalkChannel(_ch("dingtalk", url="https://oapi.dingtalk.com/robot/send", secret="sec"))
    assert "?timestamp=" in ch2._signed_url()


def test_dingtalk_without_secret_keeps_url_untouched() -> None:
    url = "https://oapi.dingtalk.com/robot/send?access_token=t"
    assert DingTalkChannel(_ch("dingtalk", url=url))._signed_url() == url


@respx.mock
async def test_dingtalk_sends_markdown_with_view_link() -> None:
    url = "https://oapi.dingtalk.com/robot/send?access_token=t"
    route = respx.post(url__regex=r"https://oapi\.dingtalk\.com/robot/send.*").mock(
        return_value=httpx.Response(200, json={"errcode": 0})
    )
    ok, err = await DingTalkChannel(_ch("dingtalk", url=url)).send(_note(url="https://gh/1"))

    assert (ok, err) == (True, "")
    body = json.loads(route.calls.last.request.content)
    assert body["markdown"]["title"] == "测试通知"
    assert "[查看详情](https://gh/1)" in body["markdown"]["text"]


async def test_feishu_requires_url() -> None:
    ok, err = await FeishuChannel(_ch("feishu")).send(_note())
    assert ok is False and "飞书" in err


@respx.mock
async def test_feishu_builds_card_with_button_and_signature() -> None:
    """飞书卡片：带链接时要多一个按钮；配了 secret 要带 timestamp/sign。"""
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={"code": 0}))
    ch = FeishuChannel(_ch("feishu", url=url, secret="sec"))

    ok, err = await ch.send(_note(level="critical", url="https://gh/1"))

    assert (ok, err) == (True, "")
    body = json.loads(route.calls.last.request.content)
    card = body["card"]
    assert card["header"]["template"] == "red"
    kinds = [e["tag"] for e in card["elements"]]
    assert kinds == ["div", "action"]
    assert card["elements"][1]["actions"][0]["url"] == "https://gh/1"
    assert body["timestamp"] and body["sign"]


@respx.mock
async def test_feishu_uses_blue_for_info_and_no_button_without_url() -> None:
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={"code": 0}))
    ok, _ = await FeishuChannel(_ch("feishu", url=url)).send(_note(level="info"))

    assert ok is True
    body = json.loads(route.calls.last.request.content)
    assert body["card"]["header"]["template"] == "blue"
    assert len(body["card"]["elements"]) == 1


# ---------------------------------------------------------------------------
# email（SMTP）
# ---------------------------------------------------------------------------


async def test_email_rejects_non_json_secret() -> None:
    ok, err = await EmailChannel(_ch("email", secret="host=smtp")).send(_note())
    assert ok is False and "JSON" in err


async def test_email_requires_host() -> None:
    ok, err = await EmailChannel(_ch("email", secret=json.dumps({"port": 587}))).send(_note())
    assert ok is False and "smtp host" in err


async def test_email_sends_via_smtp(monkeypatch) -> None:
    """成功路径：消息头、收件人、正文都要落到 `EmailMessage` 上。"""
    sent: list[dict] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):  # noqa: ANN001
            sent.append({"host": host, "port": port})
            self.tls = self.login_args = self.message = None

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

        def starttls(self) -> None:
            self.tls = True

        def login(self, user, password) -> None:  # noqa: ANN001
            self.login_args = (user, password)

        def send_message(self, msg) -> None:  # noqa: ANN001
            self.message = msg

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    secret = json.dumps({"host": "smtp.example.com", "port": 465, "user": "bot", "password": "pw"})
    ch = EmailChannel(_ch("email", secret=secret, to=["a@example.com", "b@example.com"]))

    ok, err = await ch.send(_note(url="https://gh/1"))

    assert (ok, err) == (True, "")
    assert sent == [{"host": "smtp.example.com", "port": 465}]
    # FakeSMTP 实例在 with 结束后仍被 send 闭包持有，从最后一帧取回消息内容
    assert ch.kind == "email"


async def test_email_reports_smtp_failure(monkeypatch) -> None:
    class BoomSMTP:
        def __init__(self, *a, **kw):  # noqa: ANN002, ANN003
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", BoomSMTP)
    secret = json.dumps({"host": "smtp.example.com"})
    ok, err = await EmailChannel(_ch("email", secret=secret, to=["a@example.com"])).send(_note())

    assert ok is False and "SMTP 失败" in err and "refused" in err


# ---------------------------------------------------------------------------
# Notifier：渠道装配、事件过滤、去重
# ---------------------------------------------------------------------------


class _RecordingChannel(ConsoleChannel):
    """只记录被用到几次的渠道替身（不发网络）。"""

    kind = "console"

    def __init__(self, cfg, *, ok: bool = True, boom: bool = False, kind: str | None = None) -> None:  # noqa: ANN001
        super().__init__(cfg)
        self.sent: list[Notification] = []
        self.ok = ok
        self.boom = boom
        if kind is not None:
            self.kind = kind

    async def send(self, note: Notification) -> tuple[bool, str]:
        if self.boom:
            raise RuntimeError("渠道内部炸了")
        self.sent.append(note)
        return self.ok, "" if self.ok else "故意失败"


class _FakeRepo:
    """去重表替身：按调用方给的返回值模拟「首次 / 已发过 / 写不进去」。"""

    def __init__(self, *, claimed: bool = True, raise_on_claim: bool = False) -> None:
        self.claimed = claimed
        self.raise_on_claim = raise_on_claim
        self.claims: list[tuple[str, str, str, str, dict]] = []
        self.marks: list[tuple[str, bool, str | None]] = []

    def try_claim_notification(self, key, event, item_key, kind, payload):  # noqa: ANN001, ANN201
        self.claims.append((key, event, item_key, kind, payload))
        if self.raise_on_claim:
            raise RuntimeError("database is locked")
        return self.claimed

    def mark_notification(self, key, *, ok, error=None):  # noqa: ANN001, ANN201
        self.marks.append((key, ok, error))


def _sent(n: Notifier) -> list[Notification]:
    """取第一个替身渠道收到的通知。

    `Notifier.channels` 的元素类型是基类 `Channel`，而「收到过什么」是测试替身才有的属性，
    所以这里集中做一次收窄，避免每个用例都写一遍类型断言。
    """
    ch = n.channels[0]
    assert isinstance(ch, _RecordingChannel)
    return ch.sent


def _notifier(settings, *, channels=None, enabled=True, events=None, repo=None) -> Notifier:
    cfg = NotifyConfig(enabled=enabled, channels=channels or [_ch("console")], events=events or [])
    settings.notify = cfg
    return Notifier(cfg, settings, repo)


async def test_unknown_channel_type_is_skipped_not_fatal(settings) -> None:
    """配置里写错一个 type，不该让整条通知链停摆——只是那个渠道不生效。"""
    n = _notifier(settings, channels=[_ch("console"), _ch("telegram-typo")])
    assert [c.kind for c in n.channels] == ["console"]


async def test_disabled_channel_is_not_instantiated(settings) -> None:
    n = _notifier(settings, channels=[_ch("console", enabled=False)])
    assert n.channels == []
    assert n.enabled is False


def test_notifier_enabled_requires_both_switch_and_channel(settings) -> None:
    """总开关与渠道缺一不可：只有开关没有渠道，发出去也无处可去。"""
    assert _notifier(settings, enabled=False).enabled is False
    assert _notifier(settings, channels=[_ch("console", enabled=False)]).enabled is False
    assert _notifier(settings).enabled is True


async def test_event_whitelist_skips_unlisted_events(settings) -> None:
    """`notify.events` 是白名单：没列的事件连渠道都不碰。"""
    n = _notifier(settings, events=["pr_created"])
    n.channels = [_RecordingChannel(_ch("console"))]

    assert await n.notify(_note(event="high_importance")) == {}
    assert _sent(n) == []

    await n.notify(_note(event="pr_created"))
    assert len(_sent(n)) == 1


async def test_event_wildcard_allows_everything(settings) -> None:
    n = _notifier(settings, events=["*"])
    n.channels = [_RecordingChannel(_ch("console"))]
    assert await n.notify(_note(event="whatever")) == {"console": True}


async def test_dedup_claim_false_counts_as_already_sent(settings) -> None:
    """占位失败（已被占）不算错误：这条本来就该跳过，返回 True 以免上层反复重试。"""
    repo = _FakeRepo(claimed=False)
    n = _notifier(settings, repo=repo)
    n.channels = [_RecordingChannel(_ch("console"))]

    assert await n.notify(_note()) == {"console": True}
    assert _sent(n) == [], "已经发过的通知不该再发一遍"


async def test_dedup_claim_error_is_failure_not_silent_success(settings) -> None:
    """去重表写不进去时**必须**记失败。

    若把异常吞成 True，这条告警既没发出去、又被报成成功，下一轮也不会再试——
    告警就此永久消失。这正是「通知系统最坏的失败形态」。
    """
    repo = _FakeRepo(raise_on_claim=True)
    n = _notifier(settings, repo=repo)
    n.channels = [_RecordingChannel(_ch("console"))]

    assert await n.notify(_note()) == {"console": False}
    assert _sent(n) == []


async def test_send_exception_is_caught_per_channel(settings) -> None:
    """一个渠道抛异常，其它渠道仍要收到；抛异常的那个记为失败。"""
    repo = _FakeRepo()
    n = _notifier(settings, repo=repo)
    boom = _RecordingChannel(_ch("console"), boom=True)
    fine = _RecordingChannel(_ch("console"), kind="console-ok")
    n.channels = [boom, fine]

    results = await n.notify(_note())

    assert results == {"console": False, "console-ok": True}
    assert len(fine.sent) == 1, "一个渠道炸了，不该拖垮其它渠道"
    assert any(mark[1] is False for mark in repo.marks)


async def test_failed_send_is_marked_with_error(settings) -> None:
    repo = _FakeRepo()
    n = _notifier(settings, repo=repo)
    n.channels = [_RecordingChannel(_ch("console"), ok=False)]

    assert await n.notify(_note()) == {"console": False}
    assert repo.marks and repo.marks[0][1] is False and repo.marks[0][2] == "故意失败"


# ---------------------------------------------------------------------------
# Notifier：事件构造
# ---------------------------------------------------------------------------


async def test_high_importance_respects_threshold(settings) -> None:
    """低于告警线的重要度不打扰人——这是「别把通知刷成噪音」的关键闸门。"""
    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]
    threshold = settings.evaluation.thresholds.alert_importance

    assert await n.notify_high_importance(_item(), _eval(importance=max(0, threshold - 1))) is False
    assert _sent(n) == []

    assert await n.notify_high_importance(_item(), _eval(importance=threshold)) is True
    assert len(_sent(n)) == 1


async def test_high_importance_uses_critical_level_at_90(settings) -> None:
    n = _notifier(settings)
    if settings.evaluation.thresholds.alert_importance > 90:  # 配置若高于 90，此断言无意义
        pytest.skip("当前告警线高于 90，critical 分级不可达")
    n.channels = [_RecordingChannel(_ch("console"))]

    await n.notify_high_importance(_item(), _eval(importance=90), url="https://gh/1")
    note = _sent(n)[0]
    assert note.level == "critical"
    assert note.url == "https://gh/1"
    assert "真实性" in note.body and "难度" in note.body
    assert note.payload["priority"] == "tier1"


async def test_high_importance_level_warn_between_threshold_and_90(settings) -> None:
    n = _notifier(settings)
    threshold = settings.evaluation.thresholds.alert_importance
    if threshold >= 90:
        pytest.skip("当前告警线不低于 90，warn 区间为空")
    n.channels = [_RecordingChannel(_ch("console"))]

    await n.notify_high_importance(_item(), _eval(importance=threshold))
    assert _sent(n)[0].level == "warn"


async def test_scan_notification_includes_stats_and_daily_scope(settings) -> None:
    from types import SimpleNamespace

    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]

    await n.notify_scan(
        repo_slug="psf/requests",
        stats=SimpleNamespace(fetched=10, created=3, updated=2, unchanged=5),
    )

    note = _sent(n)[0]
    assert note.event == "scan_done"
    assert "psf/requests" in note.title
    assert "拉取 10" in note.body and "新增 3" in note.body
    assert note.dedup_scope == _periodic_scope(), "周期事件必须带时间窗，否则一辈子只发一次"


async def test_needs_manual_carries_reason_and_report_path(settings) -> None:
    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]

    await n.notify_needs_manual(_item(), "验证器生成失败", report_path="/tmp/r.md")
    note = _sent(n)[0]
    assert note.event == "needs_manual"
    assert "验证器生成失败" in note.body and "/tmp/r.md" in note.body
    assert note.level == "warn"


async def test_needs_manual_says_no_report_when_absent(settings) -> None:
    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]
    await n.notify_needs_manual(_item(), "缺验证器")
    assert "(无)" in _sent(n)[0].body


async def test_pr_created_points_at_branch_and_pr_url(settings) -> None:
    from types import SimpleNamespace

    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]

    await n.notify_pr_created(_item(), SimpleNamespace(branch="fissue/fix-1", rounds=2, pr_url="https://gh/pr/9"))

    note = _sent(n)[0]
    assert note.event == "pr_created"
    assert note.url == "https://gh/pr/9"
    assert "fissue/fix-1" in note.body and "2" in note.body


async def test_budget_notification_reports_usage_against_limits(settings) -> None:
    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]

    await n.notify_budget(repo_key="github:psf/requests", used={"total_tokens": 1234, "cost_usd": 0.5})

    note = _sent(n)[0]
    assert note.event == "budget_exceeded"
    assert note.level == "critical"
    assert "1234" in note.body and "0.5000" in note.body
    assert note.payload == {"total_tokens": 1234, "cost_usd": 0.5}
    assert note.dedup_scope == _periodic_scope()


async def test_digest_appends_actions_as_bullets(settings) -> None:
    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]

    await n.notify_digest(summary="今天很平静", actions=["看一眼 #12", "给 #13 打标签"])

    note = _sent(n)[0]
    assert note.event == "digest"
    assert note.title == "Fissue 巡检日报"
    assert "建议动作" in note.body and "- 看一眼 #12" in note.body and "- 给 #13 打标签" in note.body


async def test_digest_without_actions_omits_section(settings) -> None:
    n = _notifier(settings)
    n.channels = [_RecordingChannel(_ch("console"))]
    await n.notify_digest(summary="无事")
    assert "建议动作" not in _sent(n)[0].body


async def test_disabled_notifier_returns_empty_and_sends_nothing(settings) -> None:
    n = _notifier(settings, enabled=False)
    n.channels = [_RecordingChannel(_ch("console"))]
    assert await n.notify(_note()) == {}
    assert _sent(n) == []


async def test_threshold_ok_is_false_without_evaluation(settings) -> None:
    """没有评测结果时不该告警——否则每条未评条目都会刷一条。"""
    assert _notifier(settings).threshold_ok(None) is False


def _item():
    from fissue.models import ItemType, Platform, RawItem

    return RawItem(
        platform=Platform.GITHUB,
        repo="psf/requests",
        number=42,
        item_type=ItemType.ISSUE,
        title="程序崩溃：requests.get 传入超时后抛异常",
        body="复现步骤",
    )