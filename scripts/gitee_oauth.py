#!/usr/bin/env python
"""Gitee 第三方应用 OAuth 登录：把 Client ID/Secret 换成可调 API 的 access_token。

为什么需要这个脚本
------------------
Gitee 的「第三方应用」给的是 **OAuth2 客户端凭据**（Client ID + Client Secret），
不是可直接调 API 的令牌。两者差一步**用户授权**：

1. 浏览器打开授权页 → 用户点「同意」→ 回调带回一次性 ``code``（约 10 分钟有效）；
2. 服务端用 ``code`` + Client ID/Secret 换 ``access_token``；
3. 之后调 API 用的是 ``access_token``（本项目适配器走 ``access_token`` 查询参数）。

也就是说：**只填 Client ID 与 Client Secret 不够**——少了第 1、2 步，API 仍会 401。
本脚本把 1、2 步做成一条命令，并把结果写进 ``.env`` 的 ``FISSUE_GITEE_TOKEN``。

用法::

    # 先把应用凭据写进 .env（不要写进命令行）：
    #   FISSUE_GITEE_CLIENT_ID=...
    #   FISSUE_GITEE_CLIENT_SECRET=...
    #   FISSUE_GITEE_REDIRECT_URI=http://127.0.0.1:8765/callback   # 需与应用回调地址完全一致
    python scripts/gitee_oauth.py --login

    # 只打印授权地址（自己贴到浏览器，再手动换）：
    python scripts/gitee_oauth.py --url-only
    python scripts/gitee_oauth.py --code <回调里的 code>

    # 令牌过期后刷新（Gitee 的 access_token 约 1 天）：
    python scripts/gitee_oauth.py --refresh

默认把令牌写进仓库根的 ``.env``，终端只显示掩码；``--print-token`` 才会打印完整值
（用于往 CI Secret 里填）。``--no-write-env`` 可只看结果不落盘。

拿到令牌后，用写探测确认「建/改 Issue 是否仍然 404」::

    python scripts/probe_gitee_write.py --repo <owner>/<repo> --write
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

try:  # 本机有 HTTPS 中间人时，truststore 让 httpx 走系统信任库
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - 没有它也能跑，只是本机代理环境可能 TLS 失败
    pass

import httpx  # noqa: E402  (必须在 truststore 之后导入)
from dotenv import dotenv_values  # noqa: E402

# 证书口径只有一份：所有 httpx 客户端都显式传 `verify=httpx_verify()`（见 tests/test_net.py）。
from fissue.configio import update_env_file  # noqa: E402
from fissue.net import httpx_verify  # noqa: E402

AUTHORIZE_URL = "https://gitee.com/oauth/authorize"
TOKEN_URL = "https://gitee.com/oauth/token"
USER_URL = "https://gitee.com/api/v5/user"

#: 默认 scope：够 Fissue 的读 + 打标签 + 建 PR + 建/改 Issue。
#: 少一个 `issues`，写探测就会拿到 403 而不是 404——**别把权限问题误记成平台限制**。
DEFAULT_SCOPE = "user_info projects pull_requests issues notes"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

#: 等用户点「同意」的上限。授权页可能要求先登录，给足时间，但不无限等。
CALLBACK_TIMEOUT_SECONDS = 300


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """读 ``.env``（不存在的键就是空串）。进程环境优先——与项目其它脚本一致。"""
    import os

    file_values = {k: (v or "") for k, v in dotenv_values(path).items()}
    for key in (
        "FISSUE_GITEE_CLIENT_ID",
        "FISSUE_GITEE_CLIENT_SECRET",
        "FISSUE_GITEE_REDIRECT_URI",
        "FISSUE_GITEE_SCOPE",
        "FISSUE_GITEE_TOKEN",
        "FISSUE_GITEE_REFRESH_TOKEN",
    ):
        env_value = os.environ.get(key)
        if env_value:
            file_values[key] = env_value
    return file_values


def authorize_url(client_id: str, redirect_uri: str, scope: str, state: str = "") -> str:
    """拼授权地址。``scope`` 用 ``%20`` 而不是 ``+``：Gitee 对两种都收，但日志里更好读。"""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def mask_token(token: str) -> str:
    """终端只显示尾部四位：令牌等同密码，不该整串留在截图与日志里。"""
    if not token:
        return "(空)"
    if len(token) <= 8:
        return "***"
    return f"***{token[-4:]}"


def _token_request(data: dict[str, str]) -> dict[str, Any]:
    """POST ``/oauth/token``；非 2xx 直接抛错（把状态码与报文一起带出来）。"""
    with httpx.Client(timeout=30.0, verify=httpx_verify()) as client:
        r = client.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
    if r.status_code >= 300:
        body = " ".join(str(r.text or "").split())[:300]
        raise RuntimeError(f"换取令牌失败：HTTP {r.status_code}　{body}")
    payload: dict[str, Any] = r.json()
    if not payload.get("access_token"):
        raise RuntimeError(f"响应里没有 access_token：{json.dumps(payload, ensure_ascii=False)[:300]}")
    return payload


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    """授权码 → 令牌。``code`` 一次性，且必须与授权时的 ``redirect_uri`` 完全一致。"""
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    )


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    """刷新令牌。Gitee 的 access_token 约 1 天有效，refresh_token 约 30 天。"""
    return _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )


def fetch_login(access_token: str) -> str:
    """用令牌取一次 ``/user``：既验证令牌可用，也把「这是谁授权」打在屏幕上。"""
    with httpx.Client(timeout=30.0, verify=httpx_verify()) as client:
        r = client.get(USER_URL, params={"access_token": access_token})
    if r.status_code >= 300:
        return ""
    try:
        return str(r.json().get("login") or "")
    except ValueError:
        return ""


def wait_for_code(redirect_uri: str, *, timeout: float = CALLBACK_TIMEOUT_SECONDS) -> dict[str, str]:
    """在 ``redirect_uri`` 上起一个只有一条路由的本地服务，收回调里的 ``code``。

    为什么自己收：把 ``code`` 复制粘贴到命令行既容易带进多余字符，也会让它出现在
    shell 历史里。本地回调把这一步收进进程内，``code`` 只存在于内存。
    """
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
            request = urlparse(self.path)
            if request.path != path:
                self.send_error(404)
                return
            query = parse_qs(request.query)
            for key in ("code", "error", "error_description", "state"):
                if key in query:
                    result[key] = query[key][0]
            ok = "code" in result
            body = (
                "授权已完成，可以关闭本页回到终端。" if ok else "授权失败或已取消，请看终端输出。"
            ).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆写基类签名
            pass

    server = HTTPServer((host, port), Handler)

    def serve() -> None:
        # 浏览器可能先请求 /favicon.ico；那次 404 不能结束等待。
        while not done.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout=timeout):
            raise TimeoutError(f"等待授权回调超时（{int(timeout)} 秒）：授权页没完成或回调没打回来")
    finally:
        server.server_close()
    return result


def _resolve(env: dict[str, str], args: argparse.Namespace) -> tuple[str, str, str, str]:
    """取 Client ID/Secret、回调地址与 scope；缺关键项时给一条能照做的错误。"""
    client_id = env.get("FISSUE_GITEE_CLIENT_ID", "").strip()
    client_secret = env.get("FISSUE_GITEE_CLIENT_SECRET", "").strip()
    redirect_uri = (
        args.redirect_uri or env.get("FISSUE_GITEE_REDIRECT_URI") or DEFAULT_REDIRECT_URI
    ).strip()
    scope = (args.scope or env.get("FISSUE_GITEE_SCOPE") or DEFAULT_SCOPE).strip()
    if not client_id and not args.url_only:
        raise SystemExit(
            "✗ 缺少 FISSUE_GITEE_CLIENT_ID。请在 .env 里填上应用凭据（Client Secret 同理），"
            "不要写在命令行里。"
        )
    if not client_id:
        raise SystemExit("✗ 缺少 FISSUE_GITEE_CLIENT_ID（--url-only 也需要它来拼授权地址）。")
    return client_id, client_secret, redirect_uri, scope


def _save(env_path: Path, payload: dict[str, Any], *, no_write_env: bool, print_token: bool) -> None:
    """把令牌落到 ``.env``（默认）并显示一条人能核对的结论。"""
    token = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    scope = str(payload.get("scope") or "")
    expires = payload.get("expires_in")
    login = fetch_login(token)

    print(f"\n令牌：{mask_token(token)}　授权账号：{login or '（/user 读取失败，令牌可能不可用）'}")
    if expires:
        print(f"有效期：{expires} 秒（Gitee 的 access_token 约 1 天；过期用 --refresh）")
    if scope:
        print(f"scope：{scope}")

    updates = {"FISSUE_GITEE_TOKEN": token}
    if refresh:
        updates["FISSUE_GITEE_REFRESH_TOKEN"] = refresh
    if no_write_env:
        print("\n（--no-write-env：没有改动 .env）")
    else:
        update_env_file(env_path, updates)
        print(f"\n✓ 已写入 {env_path}（FISSUE_GITEE_TOKEN）")
    if print_token:
        print(f"\n完整令牌：{token}")
        if refresh:
            print(f"刷新令牌：{refresh}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gitee 第三方应用 OAuth 登录（Client ID/Secret → access_token）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help="启动本地回调并完成一次授权（默认）")
    mode.add_argument("--code", help="已有授权码时直接换取（不启动本地回调；需配合 --redirect-uri）")
    mode.add_argument("--refresh", action="store_true", help="用 .env 里的 refresh_token 换新令牌")
    mode.add_argument("--url-only", action="store_true", help="只打印授权地址，不做任何请求")
    parser.add_argument("--redirect-uri", help="回调地址（必须与 Gitee 应用里填的完全一致）")
    parser.add_argument("--scope", help=f"授权范围，默认：{DEFAULT_SCOPE}")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只打印地址")
    parser.add_argument("--no-write-env", action="store_true", help="不写 .env，只显示结果")
    parser.add_argument("--print-token", action="store_true", help="打印完整令牌（默认只显示掩码）")
    args = parser.parse_args(argv)

    env = load_env()
    client_id, client_secret, redirect_uri, scope = _resolve(env, args)

    if args.url_only:
        state = secrets.token_urlsafe(16)
        print("在浏览器打开下面的地址（登录并同意授权）：\n")
        print(authorize_url(client_id, redirect_uri, scope, state=state))
        print(
            "\n回调会跳到 "
            f"{redirect_uri}?code=...&state={state}\n"
            "若你没法起本地回调，把地址里的 code 复制下来，再跑：\n"
            f"  python scripts/gitee_oauth.py --code <code> --redirect-uri {redirect_uri}"
        )
        return 0

    if args.refresh:
        refresh_token = env.get("FISSUE_GITEE_REFRESH_TOKEN", "").strip()
        if not client_secret:
            raise SystemExit("✗ 缺少 FISSUE_GITEE_CLIENT_SECRET，无法刷新令牌。")
        if not refresh_token:
            raise SystemExit("✗ 缺少 FISSUE_GITEE_REFRESH_TOKEN，无法刷新令牌。")
        payload = refresh_access_token(client_id, client_secret, refresh_token)
        _save(ENV_PATH, payload, no_write_env=args.no_write_env, print_token=args.print_token)
        return 0

    if not client_secret:
        raise SystemExit("✗ 缺少 FISSUE_GITEE_CLIENT_SECRET，无法换取令牌。")

    if args.code:
        payload = exchange_code(client_id, client_secret, args.code.strip(), redirect_uri)
        _save(ENV_PATH, payload, no_write_env=args.no_write_env, print_token=args.print_token)
        return 0

    # --login（也是默认）：先打印地址，再等本地回调。
    state = secrets.token_urlsafe(16)
    url = authorize_url(client_id, redirect_uri, scope, state=state)
    print("在浏览器打开下面的地址（登录并同意授权）：\n")
    print(url)
    print(f"\n等待回调：{redirect_uri}（最多 {CALLBACK_TIMEOUT_SECONDS} 秒）")
    if not args.no_browser:
        webbrowser.open(url)
    got = wait_for_code(redirect_uri)
    if "error" in got:
        raise SystemExit(
            f"✗ 授权失败：{got.get('error')}　{got.get('error_description', '')}".strip()
        )
    if got.get("state") != state:
        # state 对不上说明回调不是这次授权发起的；code 换令牌前就该停。
        raise SystemExit("✗ state 不匹配：请重新执行 --login（不要复用旧的授权地址）。")
    payload = exchange_code(client_id, client_secret, got["code"], redirect_uri)
    _save(ENV_PATH, payload, no_write_env=args.no_write_env, print_token=args.print_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())