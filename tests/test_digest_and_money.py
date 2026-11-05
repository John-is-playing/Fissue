"""成本人话化与日报：金额换算、日报聚合/渲染、自动发送开关。

两件事的共同目的：让"花了多少钱、修好了什么"变得**不用翻五页也能知道**。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fissue.config import Settings
from fissue.models import (
    BlockReason,
    FixAttempt,
    FixOutcome,
    ItemStatus,
    ItemType,
    Platform,
    RawItem,
    RepoRef,
)
from fissue.service.digest import build_digest, render_markdown
from fissue.store.db import build_database
from fissue.store.repository import Repository
from fissue.web.labels import money

# ---------------------------------------------------------------------------
# 金额人话化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "usd,rate,expected",
    [
        (0.0352, 7.2, "$0.0352（约 ¥0.25）"),
        (12.5, 7.2, "$12.50（约 ¥90.00）"),
        (0.5, 0, "$0.5000"),          # rate=0 → 只显示美元
        (None, 7.2, "$0.0000（约 ¥0.00）"),
        ("bad", 7.2, "—"),
    ],
)
def test_money_formats(usd, rate, expected):
    assert money(usd, rate) == expected


def test_money_keeps_four_decimals_for_small_amounts():
    """一次评测可能只花 $0.0001：四舍五入到分全是 ¥0.00，等于没显示。"""
    assert "$0.0001" in money(0.00012, 7.2)


# ---------------------------------------------------------------------------
# 日报聚合
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    settings = Settings()
    settings.app.data_dir = str(tmp_path)
    settings.database.url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    db = build_database(settings)
    db.create_all()
    return Repository(db)


def _seed(repo: Repository) -> None:
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="acme", name="web"))
    for n in (1, 2, 3):
        repo.upsert_item(
            rid,
            RawItem(
                platform=Platform.GITHUB,
                repo="acme/web",
                number=n,
                item_type=ItemType.ISSUE,
                title=f"条目{n}",
                body="b",
            ),
        )
    repo.set_item_status(
        "github:acme/web#1", ItemStatus.NEEDS_MANUAL, block_reason=BlockReason.MISSING_TEST.value
    )
    repo.set_item_status(
        "github:acme/web#2", ItemStatus.NEEDS_MANUAL, block_reason=BlockReason.BUDGET.value
    )
    repo.save_fix_attempt(
        FixAttempt(
            item_key="github:acme/web#3",
            outcome=FixOutcome.SUCCESS,
            branch="fissue/fix-3",
            pr_number=5,
        )
    )
    repo.record_usage(
        repo_key="acme/web",
        item_key="github:acme/web#3",
        purpose="evaluate:acme/web",
        model="m",
        prompt_tokens=1000,
        completion_tokens=100,
        cost_usd=0.05,
    )


def test_digest_counts_and_blocks(repo):
    _seed(repo)
    digest = build_digest(repo)
    assert digest.fix_success == 1
    assert digest.needs_manual == 2
    assert digest.cost_usd == pytest.approx(0.05)
    reasons = dict(digest.blocked)
    assert reasons[BlockReason.MISSING_TEST.value] == 1
    assert reasons[BlockReason.BUDGET.value] == 1


def test_digest_empty_database_is_all_zero(repo):
    """空库要产出"今天没活干"，不能抛异常。"""
    digest = build_digest(repo)
    assert digest.new_items == 0
    assert digest.needs_manual == 0
    assert digest.cost_usd == 0.0


def test_digest_window_excludes_old_rows(repo):
    """窗口之外的历史记录不计入"今天的产出"。"""
    from sqlalchemy import update

    from fissue.store.tables import FixAttemptRow

    _seed(repo)
    with repo.db.session() as session:
        session.execute(
            update(FixAttemptRow).values(
                created_at=datetime.now(timezone.utc) - timedelta(days=30)
            )
        )
    digest = build_digest(repo)
    assert digest.fix_success == 0
    # 「卡住」看的是当前状态，不看窗口——它回答"现在要处理什么"
    assert digest.needs_manual == 2


def test_digest_reversed_window_does_not_silently_zero(repo):
    """反窗口是调用方的 bug：别静默产出全 0（那看起来像"今天没活干"）。

    实现会把它兜底成「最近一天」——比全 0 更能反映"这里出错了"。
    """
    _seed(repo)
    now = datetime.now(timezone.utc)
    digest = build_digest(repo, since=now, until=now - timedelta(hours=1))
    assert digest.since < digest.until
    assert digest.until - digest.since == timedelta(days=1)
    assert digest.fix_success == 1


def test_render_markdown_has_money_and_next_steps(repo):
    _seed(repo)
    text = render_markdown(build_digest(repo), usd_to_cny=7.2)
    assert "Fissue 日报" in text
    assert "约 ¥" in text                      # 金额人话化
    assert "本月" in text and "预计" in text
    assert "缺少可执行的验证" in text            # 原因 → 文案
    assert "test_hint" in text                 # 原因 → 下一步


def test_render_markdown_without_blocked_items(repo):
    text = render_markdown(build_digest(repo))
    assert "没有卡住的条目" in text


# ---------------------------------------------------------------------------
# 汇率：配置、白名单、保存往返
# ---------------------------------------------------------------------------


def test_usd_to_cny_is_configurable_and_in_overlay_whitelist():
    from fissue.web.setup import OVERLAY_WRITABLE

    assert Settings().llm.pricing.usd_to_cny == 7.2
    assert "llm.pricing.usd_to_cny" in OVERLAY_WRITABLE


def test_settings_put_with_env_and_overlay_no_longer_pollutes(tmp_path):
    """回归：校验阶段曾把 .env 的 llm.* 原地塞进请求体，导致保存 400。

    `_validate_candidate` 会拿 `body.overlay` 做一次"合并后能否加载"的校验；如果
    合并/覆盖那一步原地改了嵌套 dict，`.env` 里的 `llm.api_key/base_url/model`
    就会被写进 overlay，`apply_overlay` 的白名单随即拒绝整次保存——
    用户什么都没填错却存不上。
    """
    from fissue.web.app import _auth_dependency, create_app
    from fissue.web.setup import register

    settings = Settings()
    settings.app.data_dir = str(tmp_path)
    settings.database.url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    settings.config_path = str(tmp_path / "config.yaml")
    settings.overlay_path = str(tmp_path / "config.local.yaml")
    (tmp_path / "config.yaml").write_text("web:\n  read_only: false\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text("{}\n", encoding="utf-8")

    app = create_app(settings)
    register(app, settings, auth=_auth_dependency())
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings",
            json={
                "env": {"FISSUE_LLM_MODEL": "deepseek-flash"},
                "overlay": {"llm": {"pricing": {"usd_to_cny": 7.35}}},
            },
        )
    assert response.status_code == 200, response.text
    written = (tmp_path / "config.local.yaml").read_text(encoding="utf-8")
    assert "usd_to_cny" in written
    # 只写了汇率：.env 的密钥不许被顺带写进覆盖层
    assert "api_key" not in written


# ---------------------------------------------------------------------------
# 自动发送开关
# ---------------------------------------------------------------------------


class _FakeNotifier:
    def __init__(self) -> None:
        self.notes: list[object] = []

    async def notify(self, note):
        self.notes.append(note)
        return {"console": True}


def _service_with(settings: Settings):
    from fissue.service.daemon import FissueService

    service = FissueService.__new__(FissueService)
    object.__setattr__(service, "settings", settings)
    return service


@pytest.mark.asyncio
async def test_digest_send_is_off_by_default(repo):
    settings = Settings()
    notifier = _FakeNotifier()
    service = _service_with(settings)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.repo = repo
    await service._maybe_send_digest(ctx, notifier)
    assert notifier.notes == []


@pytest.mark.asyncio
async def test_digest_send_when_enabled(repo):
    settings = Settings()
    settings.notify.digest_enabled = True
    notifier = _FakeNotifier()
    service = _service_with(settings)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.repo = repo
    await service._maybe_send_digest(ctx, notifier)
    assert len(notifier.notes) == 1
    note = notifier.notes[0]
    assert note.event == "digest"
    # 去重按天：同一天重复跑不会重复推
    assert note.dedup_scope == datetime.now(timezone.utc).strftime("%Y-%m-%d")
