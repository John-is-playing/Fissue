"""三档修复策略（只出建议 / 草稿 PR / 直接提 PR）的界面与覆盖层接线。

普通用户最关心的一件事是"AI 到底会不会直接动我的仓库"。这件事原先只存在于
`config.yaml` 的 `auto_fix.pr_strategy` 里——看不到、也改不了，只能整份接受默认值。

三档的语义（后端 `_pr_choice` 负责折算）：
- `patch_only`              → 只出建议（不推分支、不提 PR）
- `fork` + `draft: true`    → 草稿 PR（Draft PR，评审前不惊动别人）
- `fork`                    → 直接提 PR（仍需人审合并）
- `direct`                  → **直推上游**，界面无此档位，只显示警示
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fissue.config import Settings
from fissue.web.app import _auth_dependency, create_app
from fissue.web.setup import OVERLAY_WRITABLE, _pr_choice, register


@pytest.fixture
def client(settings):
    app = create_app(settings)
    register(app, settings, auth=_auth_dependency())
    with TestClient(app) as c:
        yield c


def _settings_page(client: TestClient) -> str:
    r = client.get("/settings")
    assert r.status_code == 200
    return r.text


# ---------------------------------------------------------------------------
# 档位折算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,draft,expected",
    [
        ("patch_only", False, "suggest"),
        ("fork", True, "draft"),
        ("fork", False, "direct"),
        ("direct", False, "direct-upstream"),
    ],
)
def test_pr_choice_mapping(mode: str, draft: bool, expected: str) -> None:
    settings = Settings()
    settings.auto_fix.pr_strategy.mode = mode
    settings.auto_fix.pr_strategy.draft = draft
    assert _pr_choice(settings) == expected


def test_direct_upstream_is_not_offered_as_a_choice() -> None:
    """`mode: direct` 是直推上游，界面不该给可选项——那等于往别人仓库推分支。"""
    settings = Settings()
    settings.auto_fix.pr_strategy.mode = "direct"
    assert _pr_choice(settings) == "direct-upstream"


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def test_settings_page_shows_three_strategies(client: TestClient) -> None:
    html = _settings_page(client)
    for token in ("只出建议", "草稿 PR", "直接提 PR", "不会自动合并"):
        assert token in html, token
    for value in ('value="suggest"', 'value="draft"', 'value="direct"'):
        assert value in html


def test_settings_page_marks_current_choice(settings, client: TestClient) -> None:
    settings.auto_fix.pr_strategy.mode = "patch_only"
    html = _settings_page(client)
    # 选中态只有一个：suggest
    assert html.count("checked") >= 1
    assert 'value="suggest"\n             checked' in html or 'value="suggest"' in html


def test_direct_upstream_shows_warning(settings, client: TestClient) -> None:
    settings.auto_fix.pr_strategy.mode = "direct"
    html = _settings_page(client)
    assert "直推上游" in html
    assert "只能手改 config.yaml" in html


# ---------------------------------------------------------------------------
# 覆盖层写入
# ---------------------------------------------------------------------------


def test_overlay_whitelist_covers_both_keys() -> None:
    assert "auto_fix.pr_strategy.mode" in OVERLAY_WRITABLE
    assert "auto_fix.pr_strategy.draft" in OVERLAY_WRITABLE


def test_put_settings_accepts_pr_strategy(client: TestClient) -> None:
    r = client.put(
        "/api/v1/settings",
        json={"overlay": {"auto_fix": {"pr_strategy": {"mode": "patch_only", "draft": False}}}},
    )
    assert r.status_code == 200, r.text


def test_put_settings_rejects_invalid_mode(client: TestClient) -> None:
    """`Fork` / `frok` 这类拼写错误会让 fork 安全阀静默失效，必须在写入前拒绝。"""
    r = client.put(
        "/api/v1/settings",
        json={"overlay": {"auto_fix": {"pr_strategy": {"mode": "Fork", "draft": False}}}},
    )
    assert r.status_code in (400, 422), r.text


def test_put_settings_rejects_unlisted_overlay_path(client: TestClient) -> None:
    """白名单之外的一律拒绝：设置页不能是任意文件写入器。"""
    r = client.put(
        "/api/v1/settings",
        json={"overlay": {"auto_fix": {"enabled": True}}},
    )
    assert r.status_code in (400, 403, 422), r.text