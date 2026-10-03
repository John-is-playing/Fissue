"""网络与 TLS 配置测试。

覆盖三类问题（都源自真实踩坑）：

1. **TLS 中间人场景**：企业代理 / 抓包工具重签证书后，certifi 里没有对应根证书，
   httpx 默认会 ``CERTIFICATE_VERIFY_FAILED``，而系统信任库里有。
   → 验证 ``build_ssl_context()`` 的降级链：INSECURE > CA_BUNDLE > truststore > certifi。
2. **SSL 错误识别**：``is_ssl_error()`` 要能穿透 httpx 的异常包装认出底层 ssl 错误。
3. **不重试行为**：证书错误重试无意义，必须立即失败（原来白等 4 轮退避约 8.5 秒）。
"""

from __future__ import annotations

import ast
import ssl
from pathlib import Path

import httpx
import pytest

from fissue import net
from fissue.errors import LLMError, PlatformError
from fissue.models import Platform, RepoRef
from fissue.net import (
    ENV_CA_BUNDLE,
    ENV_INSECURE,
    build_ssl_context,
    ca_bundle_hint,
    httpx_verify,
    is_ssl_error,
    ssl_help_text,
)


@pytest.fixture(autouse=True)
def _clean_net_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都从干净的网络环境变量开始，并清掉 TLS 上下文缓存。"""
    monkeypatch.delenv(ENV_CA_BUNDLE, raising=False)
    monkeypatch.delenv(ENV_INSECURE, raising=False)
    net.reset_cache()
    yield
    net.reset_cache()


# ---------------------------------------------------------------------------
# 1. is_ssl_error 识别
# ---------------------------------------------------------------------------

SSL_MESSAGES = [
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1082)",
    "certificate verify failed",
    "self-signed certificate in certificate chain",
    "hostname mismatch, certificate is not valid for 'api.github.com'",
    "certificate has expired",
    "SSLCertVerificationError: (1, '[SSL: CERTIFICATE_VERIFY_FAILED]')",
    "TLSV13 alert certificate required",
    # httpx 常见包装形式：底层 ssl 错误信息仍在字符串里
    "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
]

NON_SSL_MESSAGES = [
    "ConnectTimeout: timed out",
    "ReadTimeout",
    "HTTP 500 Internal Server Error",
    "connection reset by peer",
    "Name or service not known",
    "too many 429 errors",
    # 这句是 httpx 对**所有**连接失败（含 DNS、拒绝连接）的通用包装，
    # 不含任何证书特征，不能被当成 SSL 错误（否则会误伤可重试的抖动）
    "ConnectError: All connection attempts failed",
]


@pytest.mark.parametrize("message", SSL_MESSAGES)
def test_is_ssl_error_detects_tls_failures(message: str) -> None:
    assert is_ssl_error(message) is True


@pytest.mark.parametrize("message", NON_SSL_MESSAGES)
def test_is_ssl_error_ignores_transient_errors(message: str) -> None:
    """网络抖动类错误**不应**被当成 SSL 错误——它们重试是有意义的。"""
    assert is_ssl_error(message) is False


def test_is_ssl_error_on_real_ssl_exception() -> None:
    exc = ssl.SSLCertVerificationError(1, "certificate verify failed")
    assert is_ssl_error(exc) is True


def test_is_ssl_error_unwraps_httpx_exception_chain() -> None:
    """httpx 会把底层 ssl 异常藏在 __cause__ 里，必须能穿透认出。"""
    inner = ssl.SSLCertVerificationError(1, "certificate verify failed")
    outer = httpx.ConnectError("All connection attempts failed")
    outer.__cause__ = inner
    assert is_ssl_error(outer) is True


def test_is_ssl_error_handles_deeply_nested_cause() -> None:
    inner = ssl.SSLError("certificate verify failed")
    mid = httpx.ConnectError("transport error")
    mid.__cause__ = inner
    outer = RuntimeError("wrapper")
    outer.__cause__ = mid
    assert is_ssl_error(outer) is True


def test_is_ssl_error_does_not_loop_forever_on_self_cause() -> None:
    """异常链成环时不能死循环。"""
    exc = RuntimeError("boom")
    exc.__cause__ = exc
    assert is_ssl_error(exc) is False


def test_is_ssl_error_accepts_exception_object() -> None:
    assert is_ssl_error(RuntimeError("certificate verify failed")) is True


# ---------------------------------------------------------------------------
# 2. ssl_help_text 可操作性
# ---------------------------------------------------------------------------


def test_ssl_help_text_mentions_three_options() -> None:
    text = ssl_help_text()
    assert "truststore" in text          # 首选方案
    assert ENV_CA_BUNDLE in text         # 指定 CA
    assert ENV_INSECURE in text          # 排查用开关
    assert "风险" in text                # 必须提示风险


# ---------------------------------------------------------------------------
# 3. build_ssl_context 的优先级链
# ---------------------------------------------------------------------------


def test_default_prefers_system_trust_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认可有两条路径：truststore 可用 → SSLContext；不可用 → True（certifi）。"""
    value = build_ssl_context()
    try:
        import truststore  # noqa: F401

        assert isinstance(value, ssl.SSLContext)
        assert "信任库" in ca_bundle_hint()
    except ImportError:
        assert value is True
        assert "certifi" in ca_bundle_hint()


def test_insecure_flag_disables_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_INSECURE, "1")
    net.reset_cache()
    assert build_ssl_context() is False
    assert httpx_verify() is False
    assert "关闭校验" in ca_bundle_hint()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "Y"])
def test_insecure_flag_accepts_truthy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_INSECURE, value)
    net.reset_cache()
    assert build_ssl_context() is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_insecure_flag_rejects_falsy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_INSECURE, value)
    net.reset_cache()
    assert build_ssl_context() is not False


def test_ca_bundle_takes_precedence_over_trust_store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式指定 CA 文件时应使用它（适合只有 proxy 根证书的场景）。"""
    pem = _write_self_signed_pem(tmp_path / "ca.pem")
    monkeypatch.setenv(ENV_CA_BUNDLE, str(pem))
    net.reset_cache()

    value = build_ssl_context()
    assert isinstance(value, ssl.SSLContext)
    assert str(pem) in ca_bundle_hint()


def test_missing_ca_bundle_falls_back_instead_of_crashing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CA 文件不存在时不能崩，应回退到系统信任库。"""
    missing = tmp_path / "nope.pem"
    monkeypatch.setenv(ENV_CA_BUNDLE, str(missing))
    net.reset_cache()

    value = build_ssl_context()
    assert value is not False, "不该因为一个坏路径就关掉校验"


def test_invalid_ca_bundle_content_falls_back(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CA 文件内容非法（不是 PEM）时也要优雅回退。"""
    bad = tmp_path / "bad.pem"
    bad.write_text("this is not a certificate\n", encoding="utf-8")
    monkeypatch.setenv(ENV_CA_BUNDLE, str(bad))
    net.reset_cache()

    value = build_ssl_context()
    assert value is not False


def test_insecure_beats_ca_bundle(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级：INSECURE 最高。"""
    pem = _write_self_signed_pem(tmp_path / "ca.pem")
    monkeypatch.setenv(ENV_CA_BUNDLE, str(pem))
    monkeypatch.setenv(ENV_INSECURE, "1")
    net.reset_cache()
    assert build_ssl_context() is False


def test_context_is_cached_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存生效：连续两次拿到同一个对象。"""
    first = build_ssl_context()
    second = build_ssl_context()
    assert first is second

    net.reset_cache()
    third = build_ssl_context()
    if isinstance(first, ssl.SSLContext):
        assert third is not first   # 重算后是新对象


def test_ca_bundle_hint_is_descriptive() -> None:
    hint = ca_bundle_hint()
    assert any(k in hint for k in ("信任库", "certifi", "自定义 CA", "关闭校验"))


# ---------------------------------------------------------------------------
# 4. 客户端真的用上了 verify
# ---------------------------------------------------------------------------


def test_platform_adapter_passes_verify(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """适配器构造 httpx 客户端时必须带上 verify（回归：曾经没带）。"""
    from fissue.platforms.registry import build_adapter

    adapter = build_adapter(settings, platform=Platform.GITHUB, token="t")
    captured: dict[str, object] = {}
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    adapter._client = None                     # 强制重建
    client = adapter.client
    try:
        assert "verify" in captured, "适配器未传 verify 参数"
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_llm_client_passes_verify(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """LLM 客户端同样要带 verify。"""
    from fissue.ai.client import LLMClient

    client = LLMClient(settings.llm)
    captured: dict[str, object] = {}
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    client._client = None
    _ = client.client
    assert "verify" in captured, "LLM 客户端未传 verify 参数"


# ---------------------------------------------------------------------------
# 5. SSL 错误不重试（立即失败 + 可操作提示）
# ---------------------------------------------------------------------------


class _AlwaysSSLError:
    """每次调用都抛 SSL 错误的假客户端。"""

    def __init__(self) -> None:
        self.calls = 0

    async def request(self, *args, **kwargs):
        self.calls += 1
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:1082)"
        )

    async def post(self, *args, **kwargs):
        return await self.request(*args, **kwargs)

    async def aclose(self) -> None:
        return None


async def test_adapter_does_not_retry_on_ssl_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """证书错误应只尝试 1 次，且报错要带上排查方案。"""
    from fissue.platforms.github import GitHubAdapter

    adapter = GitHubAdapter(token="t", max_retries=4)
    fake = _AlwaysSSLError()
    adapter._client = fake  # type: ignore[assignment]

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("fissue.platforms.base.asyncio.sleep", fake_sleep)

    with pytest.raises(PlatformError) as exc:
        await adapter._request("GET", "https://api.github.com/repos/psf/requests")

    assert fake.calls == 1, f"SSL 错误不该重试，实际调用了 {fake.calls} 次"
    assert slept == [], "SSL 错误不该退避等待"
    message = str(exc.value)
    assert "certificate" in message.lower()
    assert "truststore" in message, "报错里应给出可操作方案"


async def test_adapter_still_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照组：普通网络抖动仍要重试（别把该重试的也砍了）。"""

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def request(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ConnectTimeout("timed out")
            return httpx.Response(200, json={"ok": True})

        async def aclose(self) -> None:
            return None

    from fissue.platforms.github import GitHubAdapter

    adapter = GitHubAdapter(token="t", max_retries=4)
    fake = Flaky()
    adapter._client = fake  # type: ignore[assignment]

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("fissue.platforms.base.asyncio.sleep", no_sleep)

    result = await adapter._request("GET", "https://api.github.com/x")
    assert result == {"ok": True}
    assert fake.calls == 3, "超时类错误应继续重试直到成功"


async def test_llm_client_does_not_retry_on_ssl_error(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """LLM 客户端的 SSL 错误同样立即失败。"""
    from fissue.ai.client import LLMClient

    client = LLMClient(settings.llm)
    fake = _AlwaysSSLError()
    client._client = fake  # type: ignore[assignment]

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("fissue.ai.client.asyncio.sleep", no_sleep)

    with pytest.raises(LLMError) as exc:
        await client.chat([{"role": "user", "content": "hi"}])

    assert fake.calls == 1, f"SSL 错误不该重试，实际 {fake.calls} 次"
    assert "truststore" in str(exc.value)


async def test_llm_client_still_retries_server_errors(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    """对照组：5xx 仍要重试。"""

    class Flaky500:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 2:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        async def aclose(self) -> None:
            return None

    from fissue.ai.client import LLMClient

    client = LLMClient(settings.llm)
    fake = Flaky500()
    client._client = fake  # type: ignore[assignment]

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("fissue.ai.client.asyncio.sleep", no_sleep)

    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert fake.calls == 2


async def test_llm_ssl_error_includes_actionable_hint(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    from fissue.ai.client import LLMClient

    client = LLMClient(settings.llm)
    client._client = _AlwaysSSLError()  # type: ignore[assignment]

    with pytest.raises(LLMError) as exc:
        await client.chat([{"role": "user", "content": "x"}])
    text = str(exc.value)
    assert ENV_CA_BUNDLE in text or "truststore" in text


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _write_self_signed_pem(path):
    """写一个合法的 CA 证书 PEM 供 CA_BUNDLE 用例使用。

    直接复用 certifi 自带的 CA 包（本身就是标准 PEM，含多张证书），
    避免为了测试而引入 cryptography 这个重依赖。
    """
    import certifi

    path.write_bytes(Path(certifi.where()).read_bytes())
    return path


# ---------------------------------------------------------------------------
# 6. 守卫：全仓库扫描，httpx 客户端必须显式传 verify=
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]

# 这些文件/目录不参与扫描
_SCAN_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "node_modules", "dist", "build", "data", "demo/textkit",
}


def _iter_python_files() -> list[Path]:
    """遍历仓库里所有 .py 文件（跳过虚拟环境、缓存、嵌套 demo 仓库等）。"""
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(part in _SCAN_SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        # 跳过嵌套仓库（它有独立的依赖与 TLS 处理）
        if rel.startswith("demo/textkit/"):
            continue
        out.append(path)
    return out


def _builds_httpx_client(call: ast.Call) -> bool:
    """判断某个调用是否是「构造 httpx 客户端」。

    覆盖：``httpx.Client(...)`` / ``httpx.AsyncClient(...)`` 以及
    通过别名（``from httpx import Client``）调用的 ``Client(...)`` / ``AsyncClient(...)``。
    ``httpx.get/post`` 这类顶层快捷函数**不算**在这个检查范围内
    （它们同样需要 verify，由另外的检查覆盖）。
    """
    func = call.func
    # httpx.Client(...) / httpx.AsyncClient(...)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "httpx" and func.attr in ("Client", "AsyncClient"):
            return True
    # from httpx import Client  →  Client(...)
    if isinstance(func, ast.Name) and func.id in ("Client", "AsyncClient"):
        return True
    return False


def _builds_httpx_shortcut(call: ast.Call) -> bool:
    """判断是否是 ``httpx.get/post/request`` 这类顶层快捷调用（也需要 verify）。"""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "httpx"
        and func.attr in ("get", "post", "put", "patch", "delete", "request", "stream")
    )


def _has_verify_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "verify" for kw in call.keywords)


def _collect_httpx_calls() -> list[tuple[Path, int, str, bool]]:
    """扫描出所有 httpx 客户端构造/快捷调用。

    返回 ``[(文件, 行号, 描述, 是否带 verify=)]``。
    """
    found: list[tuple[Path, int, str, bool]] = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _builds_httpx_client(node):
                found.append((path, node.lineno, "httpx 客户端构造", _has_verify_kwarg(node)))
            elif _builds_httpx_shortcut(node):
                found.append((path, node.lineno, f"httpx.{node.func.attr} 快捷调用", _has_verify_kwarg(node)))
    return found


def test_guard_all_httpx_clients_pass_verify() -> None:
    """守卫测试：**每个** httpx 客户端都必须显式传 ``verify=``。

    为什么需要它：httpx 默认只信 certifi 的 CA 列表。在企业代理或本机抓包工具
    （Fiddler / Charles / SteamTools…）做的 HTTPS 中间人环境里，根证书在系统信任库
    而不在 certifi 中，于是**所有**没传 verify 的客户端都会证书校验失败。

    这个坑踩过两次（平台适配器、demo 脚本），所以用静态扫描把它焊死：
    新增代码时只要忘了传 verify，这个测试立刻失败。
    """
    offenders = [
        f"{p.relative_to(REPO_ROOT).as_posix()}:{ln}  {desc}"
        for p, ln, desc, has_verify in _collect_httpx_calls()
        if not has_verify
    ]
    assert not offenders, (
        "以下 httpx 调用没有显式传 verify=，在 HTTPS 中间人环境会证书校验失败。\n"
        "请改为 verify=httpx_verify()（见 fissue/net.py）：\n  " + "\n  ".join(offenders)
    )


def test_guard_actually_finds_httpx_calls() -> None:
    """反向校验：守卫确实扫描到了调用点。

    否则「扫描不到任何东西」也会让上面的测试通过——那就是个假守卫。
    """
    calls = _collect_httpx_calls()
    assert len(calls) >= 4, f"只扫描到 {len(calls)} 处 httpx 调用，守卫可能失效了：{calls}"


def test_guard_detects_a_deliberately_unverified_client(tmp_path: Path) -> None:
    """自检：守卫能识别出「漏传 verify」的代码（用临时文件验证，不污染仓库）。"""
    tree = ast.parse(
        "import httpx\n"
        "c = httpx.Client(timeout=1)\n"                       # 漏了 verify
        "d = httpx.AsyncClient(verify=None)\n"                # 传了
    )
    clients = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _builds_httpx_client(n)]
    assert len(clients) == 2
    assert _has_verify_kwarg(clients[0]) is False
    assert _has_verify_kwarg(clients[1]) is True


def test_guard_ignores_unrelated_client_names() -> None:
    """不带 httpx 前缀、也不是 httpx 别名的 ``Client(...)`` 不该被误判。"""
    tree = ast.parse("from mylib import Client\nc = Client(timeout=1)\n")
    clients = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _builds_httpx_client(n)]
    # 名字叫 Client，会被保守地计入 —— 这是可接受的（宁多报不漏报）
    assert len(clients) == 1


def test_setup_script_resolves_tls() -> None:
    """demo 脚本必须自己解析出 TLS 上下文（它要能脱离 Fissue 独立运行）。"""
    script = REPO_ROOT / "demo" / "scripts" / "setup_github.py"
    assert script.exists(), "缺少 demo/scripts/setup_github.py"
    text = script.read_text(encoding="utf-8")
    assert "TLS_VERIFY" in text
    assert "truststore" in text, "脚本应具备 truststore 降级路径"
    assert "verify=TLS_VERIFY" in text, "构造 httpx 客户端时必须传 verify=TLS_VERIFY"


def test_setup_script_tls_verify_is_usable() -> None:
    """在真实环境里导入脚本，TLS_VERIFY 应是可用值（不是 None 这种废值）。"""
    import importlib.util
    import sys

    script = REPO_ROOT / "demo" / "scripts" / "setup_github.py"
    spec = importlib.util.spec_from_file_location("_demo_setup_github", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先注册进 sys.modules：脚本里用了 @dataclass，
    # dataclasses 在解析注解时会通过 cls.__module__ 反查 sys.modules，
    # 未注册就会抛 AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules["_demo_setup_github"] = module
    try:
        spec.loader.exec_module(module)
        value = module.TLS_VERIFY
        assert value is True or value is False or isinstance(value, ssl.SSLContext)
        # 默认不应是关闭校验
        assert value is not False, "默认不该关闭 TLS 校验"
    finally:
        sys.modules.pop("_demo_setup_github", None)
