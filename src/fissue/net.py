"""网络与 TLS 配置。

为什么需要这个模块
------------------
httpx 默认只信 **certifi** 的 CA 列表。但很多环境会做 HTTPS 中间人：

* 企业代理 / 安全网关（Zscaler、Netskope、深信服等）
* 本机代理工具（如 SteamTools、Fiddler、Charles）
* 公司自签的内部 CA

这些场景下，**系统信任库**里有正确的根证书（浏览器能正常访问），
但 certifi 里没有 → httpx 报 ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``。

解决办法：优先用 `truststore <https://pypi.org/project/truststore/>`_ 把 TLS 后端
切到操作系统信任库（Windows CryptoAPI / macOS Security / Linux 系统 CA），
这样「浏览器能打开的站点，Fissue 也能打开」。

优先级（高 → 低）：

1. ``FISSUE_INSECURE_SKIP_VERIFY=1`` —— 关闭校验（**仅供排查**，有中间人风险）
2. ``FISSUE_CA_BUNDLE=/path/to/ca.pem`` —— 显式指定 CA 文件
3. ``truststore`` 可用 —— 读操作系统信任库（推荐，默认）
4. certifi —— httpx 默认行为
"""

from __future__ import annotations

import os
import ssl
from functools import lru_cache
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)

ENV_CA_BUNDLE = "FISSUE_CA_BUNDLE"
ENV_INSECURE = "FISSUE_INSECURE_SKIP_VERIFY"

# SSL 证书校验失败的典型特征（用于快速失败，避免无意义重试）
_SSL_ERROR_HINTS = (
    "certificate verify failed",
    "certificate_verify_failed",
    "unable to get local issuer certificate",
    "self-signed certificate",
    "self signed certificate",
    "hostname mismatch",
    "certificate has expired",
    "sslcertverificationerror",
    "tlsv13 alert certificate",
)


def is_ssl_error(exc: BaseException | str) -> bool:
    """判断是否是 TLS/证书类错误。

    这类错误**重试没有意义**（证书不会自己变好），应当立即失败并给用户可操作的提示。
    """
    if isinstance(exc, ssl.SSLError):
        return True
    text = str(exc).lower()
    if any(hint in text for hint in _SSL_ERROR_HINTS):
        return True
    # httpx 会把底层 ssl 异常包一层，检查 cause 链
    seen: set[int] = set()
    cur = getattr(exc, "__cause__", None) or (exc if not isinstance(exc, str) else None)
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        cur = getattr(cur, "__cause__", None)
    return False


def ssl_help_text() -> str:
    """给出可操作的 TLS 排查建议。"""
    return (
        "TLS 证书校验失败。常见原因是环境里有 HTTPS 中间人代理"
        "（企业网关 / 本机抓包工具），其根证书在系统信任库里但不在 certifi 里。\n"
        "  方案 1（推荐）：pip install truststore —— Fissue 会自动改用系统信任库\n"
        "  方案 2：把代理的根证书导出为 PEM，然后设 FISSUE_CA_BUNDLE=/path/to/ca.pem\n"
        "  方案 3（仅排查用，有中间人风险）：临时设 FISSUE_INSECURE_SKIP_VERIFY=1"
    )


@lru_cache(maxsize=1)
def build_ssl_context() -> ssl.SSLContext | bool:
    """构建 httpx 的 ``verify`` 参数。

    返回 ``ssl.SSLContext``（正常）或 ``False``（显式关闭校验）。
    """
    # 1) 显式关闭校验（仅排查）
    if _truthy(os.getenv(ENV_INSECURE)):
        log.warning(
            "已通过 %s 关闭 TLS 证书校验——存在中间人攻击风险，仅限本地排查使用",
            ENV_INSECURE,
        )
        return False

    # 2) 显式指定 CA 文件
    bundle = os.getenv(ENV_CA_BUNDLE)
    if bundle:
        if not os.path.exists(bundle):
            log.error("%s 指向的文件不存在：%s（将回退到系统信任库）", ENV_CA_BUNDLE, bundle)
        else:
            try:
                ctx = ssl.create_default_context(cafile=bundle)
                log.debug("使用 %s 指定的 CA：%s", ENV_CA_BUNDLE, bundle)
                return ctx
            except Exception as exc:  # pragma: no cover
                log.error("加载 %s 失败（%s），将回退到系统信任库", bundle, exc)

    # 3) 操作系统信任库（推荐）
    ctx = _system_trust_context()
    if ctx is not None:
        return ctx

    # 4) 交回 httpx 默认（certifi）
    return True


def _system_trust_context() -> ssl.SSLContext | None:
    """用 truststore 接入操作系统信任库；不可用时返回 None。"""
    try:
        import truststore
    except ImportError:
        log.debug("未安装 truststore，使用 certifi 默认 CA 列表")
        return None
    try:
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        log.debug("已切换为操作系统信任库（truststore）")
        return ctx
    except Exception as exc:  # pragma: no cover
        log.warning("初始化 truststore 失败（%s），回退 certifi", exc)
        return None


def httpx_verify() -> Any:
    """给 ``httpx.AsyncClient(verify=...)`` 用的值。"""
    return build_ssl_context()


def ca_bundle_hint() -> str:
    """当前生效的 CA 来源描述（日志/诊断用）。"""
    if _truthy(os.getenv(ENV_INSECURE)):
        return "已关闭校验（FISSUE_INSECURE_SKIP_VERIFY）"
    if bundle := os.getenv(ENV_CA_BUNDLE):
        return f"自定义 CA：{bundle}"
    try:
        import truststore  # noqa: F401

        return "操作系统信任库（truststore）"
    except ImportError:
        return "certifi 默认 CA 列表（建议 pip install truststore）"


def reset_cache() -> None:
    """清缓存（测试用：改环境变量后需重算）。"""
    build_ssl_context.cache_clear()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "y")
