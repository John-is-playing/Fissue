"""通知渠道（Q4D）。

支持：webhook / 邮件 / 企业微信 / 钉钉 / 飞书 / 控制台。

设计
----
* 每个渠道实现 :class:`Channel.send`，统一接收 :class:`Notification`。
* **去重**由仓储层的 ``notifications`` 表保证（``dedup_key`` 唯一）——同一事件
  同一渠道只发一次，重启服务也不会重复轰炸。
* 发送失败只记日志，绝不影响主流程。
* 阈值过滤在这里做：只有重要度 ≥ ``alert_importance`` 的条目才推送。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from ..config import NotifyChannel, NotifyConfig, Settings
from ..logging_setup import get_logger
from ..models import Evaluation, RawItem
from ..net import httpx_verify

log = get_logger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=8.0)


@dataclass
class Notification:
    """一条待发送的通知。"""

    event: str                              # scan_done / eval_done / verify_done / fix_done / pr_created …
    title: str
    body: str = ""
    item_key: str = ""
    url: str | None = None
    level: str = "info"                     # info | warn | critical
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """去重键：事件 + 条目 + 标题摘要。"""
        raw = f"{self.event}|{self.item_key}|{self.title[:80]}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Channel:
    """通知渠道基类。"""

    kind = "base"

    def __init__(self, cfg: NotifyChannel) -> None:
        self.cfg = cfg

    async def send(self, note: Notification) -> tuple[bool, str]:
        raise NotImplementedError

    # -- 共用 -------------------------------------------------------------

    @staticmethod
    def _text(note: Notification) -> str:
        parts = [note.title]
        if note.body:
            parts.append(note.body)
        if note.url:
            parts.append(note.url)
        return "\n".join(parts)

    @staticmethod
    def _post_json(url: str, data: dict[str, Any]) -> tuple[bool, str]:
        try:
            resp = httpx.post(url, json=data, timeout=TIMEOUT, verify=httpx_verify())
        except httpx.HTTPError as exc:
            return False, f"网络错误：{exc}"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}：{resp.text[:200]}"
        return True, ""


class ConsoleChannel(Channel):
    kind = "console"

    async def send(self, note: Notification) -> tuple[bool, str]:
        mark = {"info": "ℹ️", "warn": "⚠️", "critical": "🚨"}.get(note.level, "ℹ️")
        print(f"{mark} [{note.event}] {self._text(note)}")
        return True, ""


class WebhookChannel(Channel):
    """通用 webhook：POST JSON，带可选 HMAC 签名。"""

    kind = "webhook"

    async def send(self, note: Notification) -> tuple[bool, str]:
        if not self.cfg.url:
            return False, "未配置 webhook url"
        payload = {
            "event": note.event,
            "title": note.title,
            "body": note.body,
            "item_key": note.item_key,
            "url": note.url,
            "level": note.level,
            "payload": note.payload,
            "timestamp": int(time.time()),
        }
        headers = {"Content-Type": "application/json"}
        if self.cfg.secret:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            sig = hmac.new(self.cfg.secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            headers["X-Fissue-Signature"] = f"sha256={sig}"
        try:
            resp = await _async_post(self.cfg.url, payload, headers)
        except Exception as exc:
            return False, f"网络错误：{exc}"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}：{resp.text[:200]}"
        return True, ""


class WeComChannel(Channel):
    """企业微信机器人。"""

    kind = "wecom"

    async def send(self, note: Notification) -> tuple[bool, str]:
        if not self.cfg.url:
            return False, "未配置企业微信 webhook url"
        content = self._text(note)
        if note.url:
            content = f"{content}\n[查看]({note.url})"
        ok, err = self._post_json(self.cfg.url, {"msgtype": "markdown", "markdown": {"content": content}})
        return ok, err


class DingTalkChannel(Channel):
    """钉钉机器人（支持加签）。"""

    kind = "dingtalk"

    def _signed_url(self) -> str:
        url = self.cfg.url or ""
        if not self.cfg.secret:
            return url
        ts = str(round(time.time() * 1000))
        raw = f"{ts}\n{self.cfg.secret}".encode("utf-8")
        sign = base64.b64encode(hmac.new(self.cfg.secret.encode("utf-8"), raw, hashlib.sha256).digest())
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timestamp={ts}&sign={sign.decode()}"

    async def send(self, note: Notification) -> tuple[bool, str]:
        if not self.cfg.url:
            return False, "未配置钉钉 webhook url"
        text = self._text(note)
        if note.url:
            text = f"{text}\n[查看详情]({note.url})"
        return self._post_json(
            self._signed_url(),
            {"msgtype": "markdown", "markdown": {"title": note.title, "text": text}},
        )


class FeishuChannel(Channel):
    """飞书机器人（支持签名）。"""

    kind = "feishu"

    async def send(self, note: Notification) -> tuple[bool, str]:
        if not self.cfg.url:
            return False, "未配置飞书 webhook url"
        text = self._text(note)
        body: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": note.title[:100]},
                           "template": {"info": "blue", "warn": "orange", "critical": "red"}.get(note.level, "blue")},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
            },
        }
        if note.url:
            body["card"]["elements"].append(
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "查看"},
                     "url": note.url, "type": "primary"}
                ]}
            )
        if self.cfg.secret:
            ts = str(int(time.time()))
            raw = f"{ts}\n{self.cfg.secret}".encode("utf-8")
            body["timestamp"] = ts
            body["sign"] = base64.b64encode(
                hmac.new(raw, b"", hashlib.sha256).digest()
            ).decode()
        return self._post_json(self.cfg.url, body)


class EmailChannel(Channel):
    """邮件通知（SMTP，配置需含 host/port/user/pass 于 secret 字段的 JSON 中）。"""

    kind = "email"

    async def send(self, note: Notification) -> tuple[bool, str]:
        import smtplib
        from email.message import EmailMessage

        try:
            conf = json.loads(self.cfg.secret or "{}")
        except ValueError:
            return False, "email 渠道的 secret 需为 JSON（host/port/user/password）"
        if not conf.get("host"):
            return False, "email 渠道缺少 smtp host"

        msg = EmailMessage()
        msg["Subject"] = f"[Fissue] {note.title}"
        msg["From"] = conf.get("sender") or conf.get("user", "")
        msg["To"] = ", ".join(self.cfg.to)
        msg.set_content(self._text(note))

        def _send() -> None:
            with smtplib.SMTP(conf["host"], int(conf.get("port", 587)), timeout=20) as server:
                server.starttls()
                if conf.get("user"):
                    server.login(conf["user"], conf.get("password", ""))
                server.send_message(msg)

        try:
            import asyncio

            await asyncio.to_thread(_send)
        except Exception as exc:
            return False, f"SMTP 失败：{exc}"
        return True, ""


CHANNELS: dict[str, type[Channel]] = {
    "console": ConsoleChannel,
    "webhook": WebhookChannel,
    "wecom": WeComChannel,
    "dingtalk": DingTalkChannel,
    "feishu": FeishuChannel,
    "email": EmailChannel,
}


async def _async_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=httpx_verify()) as client:
        return await client.post(url, json=payload, headers=headers)


# ---------------------------------------------------------------------------
# 通知器
# ---------------------------------------------------------------------------


class Notifier:
    """把事件按配置分发到各渠道（带去重与阈值过滤）。"""

    def __init__(self, cfg: NotifyConfig, settings: Settings, repo: Any = None) -> None:
        self.cfg = cfg
        self.settings = settings
        self.repo = repo                     # Repository，用于去重
        self.channels: list[Channel] = []
        for ch in cfg.channels:
            if not ch.enabled:
                continue
            cls = CHANNELS.get(ch.type)
            if cls is None:
                log.warning("未知的通知渠道类型：%s", ch.type)
                continue
            self.channels.append(cls(ch))
        if not self.channels and cfg.enabled:
            log.info("未配置任何启用的通知渠道")

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled and bool(self.channels)

    # -- 发送 -------------------------------------------------------------

    async def notify(self, note: Notification) -> dict[str, bool]:
        """发送通知；返回 ``{渠道类型: 是否成功}``。"""
        if not self.enabled:
            return {}
        if self.cfg.events and note.event not in self.cfg.events and "*" not in self.cfg.events:
            log.debug("事件 %s 未在 notify.events 中，跳过", note.event)
            return {}

        results: dict[str, bool] = {}
        for ch in self.channels:
            # 去重：同一 dedup_key + 渠道只发一次
            if self.repo is not None:
                claimed = self.repo.try_claim_notification(
                    f"{note.dedup_key}:{ch.kind}", note.event, note.item_key, ch.kind, note.payload
                )
                if not claimed:
                    log.debug("通知已发过，跳过：%s/%s", note.event, ch.kind)
                    results[ch.kind] = True
                    continue
            try:
                ok, err = await ch.send(note)
            except Exception as exc:
                ok, err = False, f"{type(exc).__name__}: {exc}"
            results[ch.kind] = ok
            if not ok:
                log.warning("通知发送失败 [%s] %s：%s", ch.kind, note.title[:40], err)
            if self.repo is not None:
                self.repo.mark_notification(f"{note.dedup_key}:{ch.kind}", ok=ok, error=err or None)
        return results

    # -- 事件构造 ---------------------------------------------------------

    def threshold_ok(self, evaluation: Evaluation | None) -> bool:
        """阈值过滤：重要度是否达到告警线。"""
        if evaluation is None:
            return False
        return evaluation.importance >= self.settings.evaluation.thresholds.alert_importance

    async def notify_scan(self, *, repo_slug: str, stats: Any) -> None:
        await self.notify(
            Notification(
                event="scan_done",
                title=f"扫描完成：{repo_slug}",
                body=f"拉取 {stats.fetched}，新增 {stats.created}，更新 {stats.updated}，未变 {stats.unchanged}",
                level="info",
            )
        )

    async def notify_high_importance(
        self, item: RawItem, evaluation: Evaluation, *, url: str | None = None
    ) -> bool:
        """高重要性条目告警（Q4C）。"""
        if not self.threshold_ok(evaluation):
            return False
        scores = evaluation.scores.as_dict()
        body = (
            f"重要性 {evaluation.importance}｜真实性 {evaluation.authenticity}｜"
            f"难度 {evaluation.difficulty}\n"
            f"{evaluation.summary[:400]}"
        )
        await self.notify(
            Notification(
                event="high_importance",
                title=f"[高重要性] {item.repo}#{item.number} {item.title[:60]}",
                body=body,
                item_key=item.key,
                url=url,
                level="critical" if evaluation.importance >= 90 else "warn",
                payload={"scores": scores, "priority": evaluation.priority.value},
            )
        )
        return True

    async def notify_needs_manual(self, item: RawItem, reason: str, *, report_path: str | None = None) -> None:
        await self.notify(
            Notification(
                event="needs_manual",
                title=f"[需人工] {item.repo}#{item.number} {item.title[:60]}",
                body=f"{reason}\n报告：{report_path or '(无)'}",
                item_key=item.key,
                level="warn",
            )
        )

    async def notify_pr_created(self, item: RawItem, attempt: Any) -> None:
        await self.notify(
            Notification(
                event="pr_created",
                title=f"[已提 PR] {item.repo}#{item.number}",
                body=f"分支 {attempt.branch}（轮次 {attempt.rounds}）",
                item_key=item.key,
                url=attempt.pr_url,
                level="info",
            )
        )

    async def notify_budget(self, *, repo_key: str, used: dict[str, Any]) -> None:
        await self.notify(
            Notification(
                event="budget_exceeded",
                title=f"[预算] {repo_key} 超出当日预算",
                body=(
                    f"tokens {used.get('total_tokens')}／"
                    f"上限 {self.settings.llm.budget.daily_tokens_per_repo}；"
                    f"花费 ${used.get('cost_usd', 0):.4f}／"
                    f"上限 ${self.settings.llm.budget.daily_usd_per_repo}"
                ),
                level="critical",
                payload=used,
            )
        )

    async def notify_digest(self, *, summary: str, actions: Sequence[str] = ()) -> None:
        body = summary
        if actions:
            body += "\n\n建议动作：\n" + "\n".join(f"- {a}" for a in actions)
        await self.notify(
            Notification(event="digest", title="Fissue 巡检日报", body=body, level="info")
        )
