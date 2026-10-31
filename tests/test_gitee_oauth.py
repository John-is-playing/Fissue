"""``scripts/gitee_oauth.py`` 的离线测试。

这个脚本要回答的是「第三方应用能不能替代个人令牌」，而它最容易被想当然的一步是：
**Client ID/Secret 不能直接调 API**，中间必须有一次用户授权换 ``access_token``。
所以这里钉住的都是「错了会把人引向错误结论」的性质：

* 授权地址必须带 ``response_type=code`` 与完整 scope（少 ``issues`` 会把 403 误读成 404）；
* ``code``/``refresh_token`` 换令牌的**表单字段名**不能写错（写错只会得到一句含糊的 400）；
* 令牌落盘时只能出现一次（默认终端只显示掩码），别把整串打进日志；
* 本地回调真的能把 ``code`` 从查询串里捡回来——这是 ``--login`` 的核心。

加载方式与 ``test_probe_gitee_write.py`` 同理：脚本不在可 import 的包内，按文件路径加载。
"""

from __future__ import annotations

import contextlib
import importlib.util
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "gitee_oauth.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gitee_oauth", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gitee = _load()


# ---------------------------------------------------------------------------
# 1. 授权地址：参数一个都不能少
# ---------------------------------------------------------------------------


def test_authorize_url_carries_code_flow_and_scope() -> None:
    url = gitee.authorize_url(
        "cid", "http://127.0.0.1:8765/callback", "user_info projects issues", state="s1"
    )
    assert url.startswith(gitee.AUTHORIZE_URL + "?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    # scope 用 %20 而不是 +：日志与文档里更好读，Gitee 两种都收。
    assert "scope=user_info%20projects%20issues" in url
    assert "state=s1" in url


def test_default_scope_covers_issue_writes() -> None:
    """少 ``issues`` 时写探测会得到 403 而不是 404——别把权限问题误记成平台限制。"""
    assert "issues" in gitee.DEFAULT_SCOPE.split()
    assert "projects" in gitee.DEFAULT_SCOPE.split()


# ---------------------------------------------------------------------------
# 2. 掩码：终端里不该出现整串令牌
# ---------------------------------------------------------------------------


def test_mask_token_keeps_only_tail() -> None:
    assert gitee.mask_token("") == "(空)"
    assert gitee.mask_token("short") == "***"
    masked = gitee.mask_token("abcdefgh1234")
    assert masked.endswith("1234")
    assert "abcdefgh" not in masked


# ---------------------------------------------------------------------------
# 3. 换令牌：表单字段必须与 Gitee 文档一致
# ---------------------------------------------------------------------------


@respx.mock
def test_exchange_code_posts_expected_form() -> None:
    route = respx.post(gitee.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok", "refresh_token": "ref", "scope": "issues"}
        )
    )
    payload = gitee.exchange_code("cid", "sec", "code-1", "http://127.0.0.1:8765/callback")
    assert payload["access_token"] == "tok"

    body = route.calls.last.request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=code-1" in body
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fcallback" in body


@respx.mock
def test_refresh_uses_refresh_grant() -> None:
    route = respx.post(gitee.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new"})
    )
    payload = gitee.refresh_access_token("cid", "sec", "old-refresh")
    assert payload["access_token"] == "new"
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=old-refresh" in body


@respx.mock
def test_token_error_carries_status_and_body() -> None:
    respx.post(gitee.TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(RuntimeError) as excinfo:
        gitee.exchange_code("cid", "sec", "expired", "http://127.0.0.1:8765/callback")
    # 只报「换令牌失败」会让人无从下手；状态码与报文要一起带出来。
    assert "400" in str(excinfo.value)
    assert "invalid_grant" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. 本地回调：真的能从查询串里捡回 code
# ---------------------------------------------------------------------------


def test_wait_for_code_captures_callback() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    captured: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: captured.update(gitee.wait_for_code(redirect, timeout=5)), daemon=True
    )
    thread.start()

    deadline = time.time() + 5
    while True:
        try:
            # 先发一次 /favicon.ico：真实浏览器会这么干，它不能让等待提前结束。
            with contextlib.suppress(Exception):  # 404 是预期
                urllib.request.urlopen(redirect.replace("/callback", "/favicon.ico"), timeout=1)
            urllib.request.urlopen(f"{redirect}?code=abc&state=xyz", timeout=2).read()
            break
        except Exception:  # noqa: BLE001 - 服务还没起来，重试
            if time.time() > deadline:
                raise
            time.sleep(0.05)

    thread.join(timeout=5)
    assert captured == {"code": "abc", "state": "xyz"}


# ---------------------------------------------------------------------------
# 5. 落盘：写进 .env，且终端只显示掩码
# ---------------------------------------------------------------------------


@respx.mock
def test_save_writes_env_and_masks_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(gitee.USER_URL).mock(return_value=httpx.Response(200, json={"login": "tester"}))
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nFISSUE_GITEE_TOKEN=old\n", encoding="utf-8")

    gitee._save(
        env,
        {
            "access_token": "abcdefgh1234",
            "refresh_token": "refresh-xyz",
            "expires_in": 86400,
            "scope": "issues",
        },
        no_write_env=False,
        print_token=False,
    )

    text = env.read_text(encoding="utf-8")
    assert "FISSUE_GITEE_TOKEN=abcdefgh1234" in text
    assert "FISSUE_GITEE_REFRESH_TOKEN=refresh-xyz" in text
    # 无关行必须原样保留——update_env_file 的契约。
    assert "OTHER=1" in text

    out = capsys.readouterr().out
    assert "abcdefgh1234" not in out
    assert "tester" in out


# ---------------------------------------------------------------------------
# 6. --url-only：不碰网络、不写盘，只给地址
# ---------------------------------------------------------------------------


def test_url_only_needs_no_secret_and_prints_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FISSUE_GITEE_CLIENT_ID", "cid")
    monkeypatch.delenv("FISSUE_GITEE_CLIENT_SECRET", raising=False)
    assert gitee.main(["--url-only", "--no-write-env"]) == 0
    out = capsys.readouterr().out
    assert "client_id=cid" in out
    assert "response_type=code" in out