"""配置写回 / 安全边界 / 仓库管理 / 向导 / CLI 的测试。

这一套守的是「改配置」这条链：写哪份文件、谁能触发、改完是否立刻生效。
它比「去平台上打个标签」敏感得多——里面装着密钥，所以每一条都对着一个具体的翻车方式写。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi import HTTPException
from starlette.requests import Request

from fissue.config import Settings, load_settings
from fissue.models import Platform, RepoRef
from fissue.web import setup as setup_mod
from fissue.web.app import create_app
from fissue.web.setup import (
    ENV_WRITABLE,
    apply_env,
    apply_overlay,
    env_file,
    overlay_file,
    settings_view,
    setup_guard,
)

# ---------------------------------------------------------------------------
# 夹具与助手
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 FISSUE_* 从进程环境里清掉。

    否则开发机上 ``export FISSUE_LLM_MODEL=...`` 会盖住测试写的值，
    断言就会以「本机环境」为准而不是以代码为准（``test_env.py`` 有同样的隔离）。
    """
    for key in list(os.environ):
        if key.startswith("FISSUE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def client(settings: Settings):
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as c:
        yield c


def _req(host: str = "127.0.0.1", headers: dict[str, str] | None = None) -> Request:
    """只带 client 与 headers 的最小请求对象——`setup_guard` 只读这两样。"""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": (host, 12345),
            "headers": raw,
            "query_string": b"",
        }
    )


def _cwd_env_snapshot() -> str | None:
    """当前工作目录下 .env 的内容（不存在则 None）。

    用来断言「测试没有写到项目根目录那份真实的 .env」——这条断言来自一次真实事故。
    """
    path = Path(".env")
    return path.read_text(encoding="utf-8") if path.exists() else None


def _cwd_overlay_snapshot() -> tuple[bool, str]:
    """当前工作目录下 config.local.yaml 的状态（是否存在 + 内容）。

    为什么是「快照对比」而不是「断言它不存在」：覆盖层是**用户自己的运行配置**，
    他在浏览器里点几下「监控仓库」就会合法地生成一份。断言「不存在」等于让测试
    依赖工作区的全局状态，用户一操作就红——那是误报，不是回归。
    真正要守的性质是「**我这次写入**没有碰 cwd 的那份」。
    """
    path = Path("config.local.yaml")
    return (path.exists(), path.read_text(encoding="utf-8") if path.exists() else "")


# ---------------------------------------------------------------------------
# 覆盖层：合并顺序与「不丢数据」
# ---------------------------------------------------------------------------


def test_overlay_overrides_config_yaml_but_env_still_wins(tmp_path: Path) -> None:
    """优先级：进程环境 > .env > 覆盖层 > config.yaml。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n  model: base-model\n  api_key: base-key\n"
        "repos:\n- platform: github\n  owner: a\n  name: one\n",
        encoding="utf-8",
    )
    # 只有 config.yaml
    assert load_settings(cfg, tmp_path / "missing.env").llm.model == "base-model"

    # 加上覆盖层 → 覆盖层赢
    (tmp_path / "config.local.yaml").write_text("llm:\n  model: overlay-model\n", encoding="utf-8")
    assert load_settings(cfg, tmp_path / "missing.env").llm.model == "overlay-model"

    # 再加 .env → .env 赢（它是部署项，更具体）
    env = tmp_path / ".env"
    env.write_text("FISSUE_LLM_MODEL=env-model\n", encoding="utf-8")
    assert load_settings(cfg, env).llm.model == "env-model"
    # 覆盖层里的其它键仍然生效
    assert load_settings(cfg, env).llm.api_key == "base-key"


def test_overlay_never_touches_config_yaml(tmp_path: Path) -> None:
    """写入只落在覆盖层：人手写的那份（含注释）一字不动。"""
    cfg = tmp_path / "config.yaml"
    original = "# 这条注释必须活下来\nllm:\n  model: base\n"
    cfg.write_text(original, encoding="utf-8")

    settings = load_settings(cfg, tmp_path / "missing.env")
    apply_overlay(tmp_path / "config.local.yaml", settings, {"repos": []})

    assert cfg.read_text(encoding="utf-8") == original
    assert (tmp_path / "config.local.yaml").exists()


def test_adding_one_repo_does_not_drop_the_existing_ones(tmp_path: Path) -> None:
    """接管 repos 前必须做种子导入。

    `repos` 是列表，合并时**整体替换**：若覆盖层只写新加的那一条，config.yaml 里原有的
    两条会静默消失（界面上下次就只剩一条，另外两个仓库再也不会被扫）。
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "repos:\n"
        "- platform: github\n  owner: a\n  name: one\n"
        "- platform: github\n  owner: a\n  name: two\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, tmp_path / "missing.env")
    assert [r.slug for r in settings.repos] == ["a/one", "a/two"]

    from fissue.config import RepoConfig

    setup_mod.save_repos(
        tmp_path / "config.local.yaml",
        settings,
        [*settings.repos, RepoConfig(platform=Platform.GITHUB, owner="a", name="three")],
    )
    fresh = load_settings(cfg, tmp_path / "missing.env")
    assert [r.slug for r in fresh.repos] == ["a/one", "a/two", "a/three"], "前两条被静默丢了"


# ---------------------------------------------------------------------------
# .env 逐行改写
# ---------------------------------------------------------------------------


def test_env_rewrite_keeps_comments_order_and_other_keys() -> None:
    from fissue.configio import update_env_text

    text = (
        "# 顶部说明：这一行必须活下来\n"
        "FISSUE_LLM_API_KEY=sk-old\n"
        "\n"
        "FISSUE_GITHUB_TOKEN=\n"
        "# 结尾注释\n"
    )
    after = update_env_text(
        text,
        {"FISSUE_GITHUB_TOKEN": "ghp_new", "FISSUE_LLM_API_KEY": "sk-new"},
    )
    assert "# 顶部说明：这一行必须活下来" in after
    assert "# 结尾注释" in after
    assert "FISSUE_LLM_API_KEY=sk-new" in after
    assert "FISSUE_GITHUB_TOKEN=ghp_new" in after
    assert "sk-old" not in after


def test_env_rewrite_quotes_values_that_need_it() -> None:
    from fissue.configio import update_env_text

    after = update_env_text("", {"FISSUE_API_TOKEN": "has space", "FISSUE_WEB_HOST": ""})
    assert 'FISSUE_API_TOKEN="has space"' in after
    assert "FISSUE_WEB_HOST=\n" in after


def test_env_whitelist_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        apply_env(tmp_path / ".env", {"FISSUE_EVIL": "x"})
    assert exc.value.status_code == 400
    assert "FISSUE_EVIL" in str(exc.value.detail)
    assert not (tmp_path / ".env").exists()


def test_overlay_whitelist_rejects_unknown_paths(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(HTTPException) as exc:
        apply_overlay(tmp_path / "config.local.yaml", settings, {"llm": {"api_key": "x"}})
    assert exc.value.status_code == 400
    assert "llm.api_key" in str(exc.value.detail)
    assert not (tmp_path / "config.local.yaml").exists()


# ---------------------------------------------------------------------------
# 安全边界
# ---------------------------------------------------------------------------


def test_guard_allows_loopback(settings: Settings) -> None:
    setup_guard(_req("127.0.0.1"), settings)          # 不该抛
    setup_guard(_req("::1"), settings)


def test_guard_allows_unparseable_peer(settings: Settings) -> None:
    """测试客户端 / UDS 拿不到可解析地址。「不知道对端是谁」不等于「可疑」。"""
    setup_guard(_req("testclient"), settings)


def test_guard_rejects_remote_when_unauthenticated(settings: Settings) -> None:
    with pytest.raises(HTTPException) as exc:
        setup_guard(_req("192.168.1.50"), settings)
    assert exc.value.status_code == 403
    assert "FISSUE_API_TOKEN" in str(exc.value.detail)


def test_guard_rejects_proxied_requests(settings: Settings) -> None:
    """带反代转发头 = 这个控制面可能已经对外了，即使来源看着是回环。"""
    with pytest.raises(HTTPException) as exc:
        setup_guard(_req("127.0.0.1", {"x-forwarded-for": "203.0.113.7"}), settings)
    assert exc.value.status_code == 403
    assert "反向代理" in str(exc.value.detail)


def test_guard_allows_remote_when_authenticated(settings: Settings) -> None:
    s = settings.model_copy(deep=True)
    s.api.auth_required = True
    setup_guard(_req("203.0.113.9"), s)               # 口令已经核对过（auth 依赖）


def test_guard_allows_remote_when_force_switch_is_on(settings: Settings) -> None:
    s = settings.model_copy(deep=True)
    s.web.allow_remote_config_write = True
    setup_guard(_req("192.168.1.50"), s)


# ---------------------------------------------------------------------------
# 写入路径（一次真实事故的回归）
# ---------------------------------------------------------------------------


def test_writes_go_to_the_resolved_paths_never_to_cwd(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入必须落在**解析出的** .env / 覆盖层，绝不回落到 cwd 下的同名文件。

    回归一次真实事故：``settings.env_path`` 在「.env 不存在」时是 None，而写入曾拿它回落成
    ``Path(".env")``（cwd）。于是用一份临时 ``--env <tmp>/missing.env`` 起服务时，向导把
    ``FISSUE_LLM_API_KEY`` 写进了**项目根目录那份真实的 .env**，覆盖了里面原有的密钥。
    「往我没打算写的那份文件里写密钥」是最不该发生的一类错误，所以这条必须钉住。
    """
    before = _cwd_env_snapshot()
    before_overlay = _cwd_overlay_snapshot()
    assert settings.env_path is None, "夹具的前提：那份 .env 不存在（所以 env_path 是 None）"
    assert env_file(settings) == Path(settings.env_source), "写入目标应当是解析出的路径"

    from fissue.config import RepoConfig

    apply_env(env_file(settings), {"FISSUE_LLM_API_KEY": "sk-should-land-in-tmp"})
    setup_mod.save_repos(
        overlay_file(settings),
        settings,
        [RepoConfig(platform=Platform.GITHUB, owner="a", name="b")],
    )

    assert Path(settings.env_source).exists(), "应当写进传入的那份 .env"
    assert "sk-should-land-in-tmp" in Path(settings.env_source).read_text(encoding="utf-8")
    assert _cwd_env_snapshot() == before, "cwd 下的 .env 被改动了！"
    assert _cwd_overlay_snapshot() == before_overlay, "cwd 下的覆盖层被改动了！"


def test_apply_endpoint_writes_to_the_resolved_env_and_keeps_cwd_clean(
    settings: Settings, client: Any
) -> None:
    """端到端版本：走 HTTP 的向导写入同样不能碰 cwd。"""
    before = _cwd_env_snapshot()
    before_overlay = _cwd_overlay_snapshot()
    r = client.post(
        "/api/v1/setup/apply",
        json={
            "env": {"FISSUE_LLM_API_KEY": "sk-e2e"},
            "repos": [{"platform": "github", "owner": "a", "name": "b"}],
            "first_scan": False,
        },
    )
    assert r.status_code == 200, r.text
    assert "sk-e2e" in Path(settings.env_source).read_text(encoding="utf-8")
    assert _cwd_env_snapshot() == before, "cwd 下的 .env 被改动了！"
    assert _cwd_overlay_snapshot() == before_overlay, "cwd 下的覆盖层被改动了！"


def test_reload_keeps_the_same_config_and_db(settings: Settings, client: Any) -> None:
    """热重载必须复现同一次加载：拿着 None 再加载一次会回落到 cwd 的 config.yaml。"""
    before_db = client.app.state.settings.database.url
    after = setup_mod.reload_state(client.app)
    assert after.database.url == before_db
    assert after.config_source == settings.config_source


# ---------------------------------------------------------------------------
# 热重载与「立刻生效」
# ---------------------------------------------------------------------------


def test_changing_a_value_takes_effect_without_restart(settings: Settings, client: Any) -> None:
    r = client.put("/api/v1/settings", json={"env": {"FISSUE_LLM_MODEL": "hot-model"}})
    assert r.status_code == 200, r.text
    assert client.app.state.settings.llm.model == "hot-model"
    assert r.json()["written_env"] == ["FISSUE_LLM_MODEL"]


def test_host_and_port_changes_report_restart_required(settings: Settings, client: Any) -> None:
    """监听地址/端口是 uvicorn 已经绑好的 socket：只能如实说「要重启」。"""
    r = client.put("/api/v1/settings", json={"env": {"FISSUE_WEB_PORT": "8123"}})
    assert r.status_code == 200
    assert r.json()["restart_required"] == ["FISSUE_WEB_PORT"]
    assert "重启" in r.json()["note"]


def test_settings_page_masks_secrets_and_never_echoes_them(
    settings: Settings, client: Any
) -> None:
    client.put("/api/v1/settings", json={"env": {"FISSUE_LLM_API_KEY": "sk-super-secret-value"}})
    data = client.get("/api/v1/settings").json()
    item = next(i for i in data["env_items"] if i["key"] == "FISSUE_LLM_API_KEY")
    assert item["set"] is True
    assert "sk-super-secret-value" not in str(data)
    assert item["value"].startswith("sk-s")

    page = client.get("/settings").text
    assert "sk-super-secret-value" not in page


def test_blank_secret_means_leave_unchanged(settings: Settings, client: Any) -> None:
    """留空 = 不改动。

    没有这条，「保存设置」会把用户没碰过的密钥一起写成空——页面上输入框本来就是空的
    （只显示掩码占位符）。
    """
    client.put("/api/v1/settings", json={"env": {"FISSUE_LLM_API_KEY": "sk-keep-me"}})
    client.put("/api/v1/settings", json={"env": {"FISSUE_LLM_MODEL": "another"}})
    assert client.app.state.settings.llm.api_key == "sk-keep-me"


def test_clearing_a_value_returns_to_the_lower_layer(settings: Settings, client: Any) -> None:
    """「清空」= 把这一层**交还给下一层**，不等于「这个配置变成空串」。

    夹具的 config.yaml 里有 ``llm.api_key: test-key``。把 .env 里那行清成 ``FISSUE_LLM_API_KEY=``
    之后，生效值回到 config.yaml 的 ``test-key``——因为 ``=``（空值）在环境变量层被视为
    「没设」，这是本项目既有的、有测试守着的优先级语义（见 ``test_env.py`` 的
    ``test_empty_value_does_not_override``）。

    页面据实显示来源（「设置」页的 sources 表），所以这条不会让人困惑：
    想知道「清空之后到底谁在生效」，看那一列。
    """
    client.put("/api/v1/settings", json={"env": {"FISSUE_LLM_API_KEY": "sk-upper-layer"}})
    assert client.app.state.settings.llm.api_key == "sk-upper-layer"

    client.put("/api/v1/settings", json={"env": {"FISSUE_LLM_API_KEY": ""}})
    assert client.app.state.settings.llm.api_key == "test-key", "应当回落到 config.yaml 那一层"
    # 而这一层的设置确实被清掉了（.env 里那行变成空）
    env_text = Path(settings.env_source).read_text(encoding="utf-8")
    assert "FISSUE_LLM_API_KEY=\n" in env_text
    assert "sk-upper-layer" not in env_text


# ---------------------------------------------------------------------------
# 仓库管理
# ---------------------------------------------------------------------------


def test_repo_crud_and_listing(settings: Settings, client: Any) -> None:
    initial = client.get("/api/v1/repos").json()
    assert initial["writable"] is True
    assert {r["slug"] for r in initial["repos"]} == {"psf/requests"}

    r = client.post(
        "/api/v1/repos",
        json={"platform": "github", "owner": "o", "name": "n", "test_hint": "python -m pytest -q"},
    )
    assert r.status_code == 201, r.text
    assert {x["slug"] for x in r.json()["repos"]} == {"psf/requests", "o/n"}

    r = client.patch("/api/v1/repos/github/o/n", json={"enabled": False, "since_days": 7})
    assert r.status_code == 200
    row = next(x for x in r.json()["repos"] if x["slug"] == "o/n")
    assert row["enabled"] is False and row["since_days"] == 7

    r = client.delete("/api/v1/repos/github/o/n")
    assert r.status_code == 200
    assert {x["slug"] for x in r.json()["repos"]} == {"psf/requests"}
    assert "历史数据" in r.json()["note"]


def test_repo_delete_keeps_history_by_default(settings: Settings, client: Any) -> None:
    """移出监控**不删数据**：条目与结论是花钱（token）换来的。"""
    from fissue.models import ItemType, RawItem

    repo = client.app.state.repo
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(
        rid,
        RawItem(
            platform=Platform.GITHUB, repo="psf/requests", number=1,
            item_type=ItemType.ISSUE, title="t", body="b",
        ),
    )
    r = client.delete("/api/v1/repos/github/psf/requests")
    assert r.status_code == 200
    assert r.json()["kept_items"] == 1
    assert r.json()["purged_items"] == 0

    purged = client.post("/api/v1/repos", json={"platform": "github", "owner": "psf", "name": "requests"})
    assert purged.status_code == 201
    r2 = client.delete("/api/v1/repos/github/psf/requests", params={"purge_data": "true"})
    assert r2.json()["purged_items"] == 1


def test_repo_validation_errors(settings: Settings, client: Any) -> None:
    assert client.post("/api/v1/repos", json={"platform": "github", "owner": "a", "name": "b/c"}).status_code == 400
    assert client.post("/api/v1/repos", json={"platform": "github", "owner": "", "name": "b"}).status_code == 400
    assert client.post("/api/v1/repos", json={"platform": "github", "owner": "a", "name": "c", "collect": ["wiki"]}).status_code == 400
    assert client.post("/api/v1/repos", json={"platform": "github", "owner": "psf", "name": "requests"}).status_code == 409
    assert client.patch("/api/v1/repos/github/a/b", json={"enabled": False}).status_code == 404
    assert client.patch("/api/v1/repos/nope/a/b", json={"enabled": False}).status_code == 400


def test_repos_page_renders(settings: Settings, client: Any) -> None:
    html = client.get("/repos").text
    assert "监控仓库" in html
    assert "psf/requests" in html
    assert "覆盖层" in html and "config.yaml" in html


# ---------------------------------------------------------------------------
# 页面与状态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/items", "/queues", "/jobs", "/repos", "/status", "/settings", "/setup"])
def test_every_page_renders(settings: Settings, client: Any, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, f"{path} 挂了：{r.text[:200]}"
    assert r.headers["content-type"].startswith("text/html")
    # 侧栏导航在每个页面上都在（否则用户一旦进入某页就回不去）
    assert "状态监测" in r.text and "初始化向导" in r.text


def test_nav_highlights_the_current_page(settings: Settings, client: Any) -> None:
    html = client.get("/status").text
    assert 'class="on"' in html.split("状态监测")[0].rsplit("<a", 1)[-1] or "on" in html


# ---------------------------------------------------------------------------
# 行内事件属性被双引号截断（一次真实 bug 的护栏）
# ---------------------------------------------------------------------------
#
# 症状：按钮点了毫无反应，页面上**也不报错**。
# 根因：`onclick="fn({{ x | tojson }})"` 渲染成 `onclick="fn("x")"`——JSON 的双引号在第一个
#       `"` 处就把属性截断，浏览器只看到 `fn(`，语法错误被静默吞掉。
# Jinja 的 `tojson` 会转义 `'`（好让它能放进单引号属性），但**不会**转义 `"`。
# 修法是「值走 data-*」：data 属性由 Jinja 自动转义，引号问题根本不存在。


def test_templates_never_interpolate_into_inline_event_attributes() -> None:
    """源码级护栏：比逐页渲染更早、更全地拦住这一类写法。

    查两条：
    1. `tojson` 落进双引号属性 —— 它的输出带双引号，属性会被截断；
    2. **任何**模板插值落进行内事件属性（onclick 等）—— 值一旦含引号就同样截断，
       而「值里有没有引号」取决于运行时的数据，不该靠运气。

    注释行跳过：注释里举例说明坏写法是正常的，不该被判违规。只跳过**以注释开头**的行，
    不做整段剥离——否则会把正常代码行里的 `//`（比如 URL）也切掉，漏掉真问题。
    """
    import re
    from pathlib import Path

    templates = Path(__file__).resolve().parents[1] / "src" / "fissue" / "web" / "templates"
    comment_starts = ("{#", "//", "/*", "*")
    offenders: list[str] = []
    for path in sorted(templates.glob("*.html")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(comment_starts):
                continue
            if re.search(r'="[^"]*\| tojson', line):
                offenders.append(f"{path.name}:{lineno} tojson 落进双引号属性：{stripped[:80]}")
            if re.search(r'on\w+="[^"]*\{\{', line):
                offenders.append(f"{path.name}:{lineno} 事件属性里有插值：{stripped[:80]}")
    assert not offenders, (
        "行内事件属性的值走 data-*，插值一律放 <script> 里；"
        "否则值一旦含引号，属性就会被截断，按钮点了毫无反应且页面不报错。\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "path", ["/", "/items", "/queues", "/jobs", "/repos", "/status", "/settings", "/setup"]
)
def test_no_page_renders_a_truncated_event_attribute(
    settings: Settings, client: Any, path: str
) -> None:
    """渲染级护栏：事件属性以 `(` 结尾 = 被截断了（那种写法的典型症状）。"""
    import re

    html = client.get(path).text
    broken = re.findall(r'on\w+="[^"]*\(["\s>]', html)
    assert not broken, f"{path} 上有被截断的事件属性：{broken[:2]}"


def test_repo_row_buttons_are_wired_through_data_attributes(
    settings: Settings, client: Any
) -> None:
    """三个行内按钮（保存参数 / 启停 / 移出）必须真的接上。

    回归：它们原先写的是 ``onclick="saveRepo({{ ... | tojson }})"``，渲染后被截断成
    ``onclick="saveRepo("`` —— 用户点下去什么都没发生，而页面不报错。
    """
    html = client.get("/repos").text
    for act in ("save", "toggle", "remove"):
        assert f'data-act="{act}"' in html, f"缺少 data-act={act} 的按钮"
    assert 'data-platform="github"' in html
    assert 'data-owner="psf"' in html and 'data-name="requests"' in html
    # 旧的截断写法不该回来
    for old in ('onclick="saveRepo(', 'onclick="toggleRepo(', 'onclick="removeRepo('):
        assert old not in html


def test_queue_flush_buttons_carry_the_queue_name(settings: Settings, client: Any) -> None:
    """队列页的按行 flush 按钮走 data-*，值从按钮自己身上读。

    「旧写法不复存在」由源码护栏负责（`test_templates_never_interpolate_into_inline_event_attributes`）：
    页面文本里可能因为注释/脚本而出现相近的字符串，在渲染结果上做**否定断言**只会误伤自己。
    """
    html = client.get("/queues").text
    assert 'data-act="flush"' in html
    assert 'data-queue="verify"' in html and 'data-queue="fix_bug"' in html
    assert 'data-force="true"' in html and 'data-force="false"' in html


def test_item_page_action_buttons_call_named_functions(settings: Settings, client: Any) -> None:
    """详情页的「重新评测 / 重新验证」调用具名函数：KEY 放在脚本里，不插进属性。"""
    from urllib.parse import quote

    from fissue.models import ItemType, RawItem, RepoRef

    key = "github:psf/requests#42"
    repo = client.app.state.repo
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    repo.upsert_item(
        rid,
        RawItem(
            platform=Platform.GITHUB, repo="psf/requests", number=42,
            item_type=ItemType.ISSUE, title="带 key 的条目", body="b",
        ),
    )
    html = client.get(f"/item/{quote(key, safe='')}").text
    assert 'onclick="reEval()"' in html
    assert 'onclick="reVerify()"' in html
    assert "function reEval()" in html and "function reVerify()" in html
    # KEY 必须在脚本作用域里（按钮靠它），而不是被插进属性
    assert 'var KEY = "github:psf/requests#42"' in html


def test_status_snapshot_has_the_sections_the_page_renders(settings: Settings, client: Any) -> None:
    data = client.get("/api/v1/status").json()
    for key in (
        "app", "writer", "database", "llm", "sandbox", "queues", "jobs", "repos", "scans", "alerts",
    ):
        assert key in data, f"状态快照缺 {key}"
    assert data["app"]["pid"] > 0
    assert data["app"]["uptime_seconds"] is not None
    assert any(a["level"] == "warn" for a in data["alerts"]), "没配平台 token 时应当有一条 warn"


def test_status_can_probe_the_sandbox_on_demand(settings: Settings, client: Any) -> None:
    """默认不探（每 2 秒敲一次 Docker 太吵），显式传 sandbox=1 才探。"""
    assert client.get("/api/v1/status").json()["sandbox"]["ready"] is None
    probed = client.get("/api/v1/status", params={"sandbox": "true"}).json()["sandbox"]
    assert probed["ready"] is not None


def test_settings_view_lists_sources(settings: Settings, client: Any) -> None:
    data = client.get("/api/v1/settings").json()
    assert set(data["writable_keys"]) == set(ENV_WRITABLE)
    # 覆盖层能改的是**路径**（不是文件名）：仓库列表与那个强制跳过开关
    assert set(data["overlay_writable"]) == {"repos", "web.allow_remote_config_write"}
    assert "FISSUE_WEB_PORT" in data["restart_keys"]
    assert data["paths"]["overlay_expected"].endswith("config.local.yaml")


def test_settings_view_reports_blocked_reason(settings: Settings) -> None:
    """被锁时页面要能说清「为什么不让改」，而不是只给一个禁用的按钮。"""
    view = settings_view(_req("192.168.1.50"), settings)
    assert view["writable"] is False
    assert "回环" in view["blocked_reason"] or "鉴权" in view["blocked_reason"]


def test_settings_page_keeps_the_save_button_always_visible(settings: Settings, client: Any) -> None:
    """保存按钮必须常驻在**顶部**，并且有「未保存」提示与离页拦截。

    回归一次真实的「保存不了」：这个页面有四个面板，而保存按钮原先只出现在最后一个
    （「Web 与访问控制」）里——用户改的是最上面那个（「AI 模型」），屏幕上根本看不到按钮，
    改完就离开页面。服务日志里**一次 PUT 都没有**，看着像后端不保存，其实是界面没告诉他
    还有一步。所以这条钉住三件事：按钮的位置、脏标记、离页拦截。
    """
    html = client.get("/settings").text
    assert 'id="save-bar"' in html, "保存条必须常驻"
    assert 'id="dirty-hint"' in html
    assert "beforeunload" in html, "带着未保存的改动离开时应当拦一下"
    assert "markDirty" in html
    # 只留**一个**保存按钮：两个会让人以为各面板能分别保存
    assert html.count("saveSettings()") >= 1
    assert html.count(">保存设置</button>") == 1, "保存按钮不该出现两次"
    # 关键：保存条必须排在第一个面板**之前**，否则又变成「埋在下面」
    assert html.index('id="save-bar"') < html.index("AI 模型"), "保存条要在最上面"


# ---------------------------------------------------------------------------
# 探活（LLM）：一次真实请求 + 可诊断的报错
# ---------------------------------------------------------------------------
#
# 这一节守着一串真实踩过的坑：探活曾经因为 `max_retries=0` **一次请求都没发出去**，
# 永远报「重试耗尽」；又因为继承了 .env 里的备用模型，降级后报错指向的是 qwen3.6-plus，
# 让用户以为「我填的模型被环境变量覆盖了」。


@respx.mock
async def test_probe_llm_actually_calls_the_endpoint() -> None:
    """探活必须真的打出去，并且带上被测的模型与 key。"""
    import httpx

    from fissue.web.setup import probe_llm

    route = respx.post("https://probe.invalid/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )
    )

    result = await probe_llm(
        base_url="https://probe.invalid/v1", model="deepseek-flash", api_key="sk-x"
    )

    assert result["ok"] is True, result
    assert route.called, "探活没发请求就等于没测任何东西"
    assert result["model"] == "deepseek-flash"
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "deepseek-flash"
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-x"


@respx.mock
async def test_probe_llm_reports_the_model_being_tested() -> None:
    """失败时点名的必须是**被测**的那个模型。

    回归：探活曾继承配置里的备用模型，主模型失败后静默降级，最后报错写成
    「模型调用失败（qwen3.6-plus）」——用户以为自己填的 deepseek-flash 被覆盖了。
    """
    import httpx

    from fissue.web.setup import probe_llm

    respx.post("https://probe.invalid/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "Model Not Exist"}})
    )

    result = await probe_llm(
        base_url="https://probe.invalid/v1", model="deepseek-flash", api_key="sk-x"
    )

    assert result["ok"] is False
    assert "deepseek-flash" in result["error"]
    assert "qwen3.6-plus" not in result["error"], "不该冒出别的模型名把原因带偏"
    assert "Model Not Exist" in result["error"], "平台给的原因要原样带出来"


def test_probe_llm_takes_no_fallback_model() -> None:
    """探活**不接受**备用模型——降级会把报错指向另一个模型（见上一条）。"""
    import inspect

    from fissue.web.setup import probe_llm

    assert "fallback_model" not in inspect.signature(probe_llm).parameters


@respx.mock
async def test_test_llm_endpoint_surfaces_the_real_reason(settings: Settings, client: Any) -> None:
    """接口层：报错要给得出原因（状态码 + 平台原话），不能只说「重试耗尽」。

    用户就是靠这句话判断「是 key 错、模型名错，还是地址错」——含糊的报错会把人引到
    错误的方向（这次就被引到「环境变量覆盖了我的配置」上）。
    """
    import httpx

    respx.post(f"{settings.llm.base_url}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "Authentication Fails"}})
    )

    r = client.post(
        "/api/v1/setup/test-llm", json={"model": "deepseek-flash", "api_key": "sk-bad"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "401" in body["error"] and "Authentication Fails" in body["error"]
    assert "deepseek-flash" in body["error"]
    assert "重试耗尽" not in body["error"]


@respx.mock
async def test_probe_platform_actually_calls_the_api(settings: Settings) -> None:
    """「校验」必须真的问一次平台，并把登录名带回来。

    回归：它曾因为 `platforms.max_retries=0` 一次请求都没发出，报
    「请求最终失败：GET https://api.github.com/user（None）」——而同一把 token 直连是 200。
    """
    import httpx

    from fissue.models import Platform
    from fissue.web.setup import probe_platform

    route = respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "John-is-playing"})
    )

    result = await probe_platform(settings, Platform.GITHUB, "ghp_x")

    assert result["ok"] is True, result
    assert result["login"] == "John-is-playing"
    assert route.called, "校验没发请求就等于没测任何东西"
    assert "ghp_x" in route.calls.last.request.headers["authorization"]


@respx.mock
async def test_probe_platform_reports_the_platform_reason(settings: Settings) -> None:
    """令牌无效时要把平台的 401 与它自己的话带回来，而不是一个含糊的失败。"""
    import httpx

    from fissue.models import Platform
    from fissue.web.setup import probe_platform

    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )

    result = await probe_platform(settings, Platform.GITHUB, "bad-token")

    assert result["ok"] is False
    assert "401" in result["error"] and "Bad credentials" in result["error"]


@respx.mock
async def test_test_platform_endpoint_surfaces_the_result(settings: Settings, client: Any) -> None:
    """接口层：页面上的「校验」拿到的是真实结果（登录名），不是一句「失败」。"""
    import httpx

    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "John-is-playing"})
    )

    r = client.post("/api/v1/setup/test-platform", json={"platform": "github", "token": "ghp_x"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["login"] == "John-is-playing"
    assert body["platform"] == "github"


# ---------------------------------------------------------------------------
# CLI：`fissue onboard`
# ---------------------------------------------------------------------------


def test_onboard_cli_non_interactive_writes_to_given_paths(tmp_path: Path) -> None:
    """`--yes` 路径：把值写进**传入的**那两份文件，且不碰 cwd 下的 .env。"""
    from typer.testing import CliRunner

    from fissue.cli.main import app as cli_app

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "database:\n  url: sqlite:///" + (tmp_path / "t.db").as_posix() + "\n"
        "llm:\n  model: cfg-model\n"
        "sandbox:\n  runtime: local\n  enabled: true\n"
        "app:\n  data_dir: " + (tmp_path / "data").as_posix() + "\n  log_level: error\n"
        "repos: []\n",
        encoding="utf-8",
    )
    before = _cwd_env_snapshot()

    result = CliRunner().invoke(
        cli_app,
        [
            "onboard",
            "--config", str(cfg),
            "--env", str(tmp_path / "missing.env"),
            "--yes", "--no-check", "--no-scan",
            "--llm-api-key", "sk-cli",
            "--llm-model", "cli-model",
            "--repo", "owner/name",
            "--test-hint", "python -m pytest -q",
        ],
    )
    assert result.exit_code == 0, result.output
    written_env = tmp_path / "missing.env"
    assert written_env.exists(), result.output
    env_text = written_env.read_text(encoding="utf-8")
    assert "FISSUE_LLM_API_KEY=sk-cli" in env_text
    assert "FISSUE_LLM_MODEL=cli-model" in env_text

    overlay = tmp_path / "config.local.yaml"
    assert overlay.exists(), result.output
    assert "owner/name" in overlay.read_text(encoding="utf-8").replace("  ", " ").replace("\n", " ") or "name: name" in overlay.read_text(encoding="utf-8")

    assert _cwd_env_snapshot() == before, "CLI 也不能写 cwd 下的 .env"


def test_onboard_cli_refuses_to_prompt_without_a_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非交互终端下要明确告诉用户加 --yes，而不是卡在提示上等输入。"""
    from typer.testing import CliRunner

    from fissue.cli.main import app as cli_app

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("repos: []\n", encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["onboard", "--config", str(cfg), "--env", str(tmp_path / "m.env")])
    assert result.exit_code != 0
    assert "yes" in result.output.lower() or "--yes" in result.output
