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

from .errors import ConfigError
from .logging_setup import get_logger

log = get_logger(__name__)

ENV_CA_BUNDLE = "FISSUE_CA_BUNDLE"
ENV_INSECURE = "FISSUE_INSECURE_SKIP_VERIFY"

#: 从 .env 文件读到的值，由 :func:`fissue.config.load_settings` 登记。
#:
#: 为什么不直接写进 ``os.environ``：本进程里会跑模型生成的验证器与不可信 PR 代码
#: （虽然沙盒现在按白名单传环境变量），但「有人在 .env 里写了一行 CA 路径」不该
#: 顺带把 LLM key、平台 token 也散进进程环境。所以只登记、不导出。
#:
#: 修的是这个坑：``net`` 原本只用 ``os.getenv`` 读，而 .env 是用 ``dotenv_values``
#: 读成字典的（不导出）——于是文档里写明的「设 FISSUE_CA_BUNDLE 就能解决 TLS」
#: 写进 .env 完全无效，只有 ``export`` 到 shell 才行。
_env_file_values: dict[str, str] = {}


def register_env_file_values(values: dict[str, str]) -> None:
    """登记 .env 里的值（``load_settings`` 读取 .env 后调用）。"""
    _env_file_values.clear()
    _env_file_values.update(values)


def _lookup(name: str) -> str | None:
    """按与配置加载一致的优先级取值：**进程环境变量 > .env 文件**。"""
    return os.environ.get(name) or _env_file_values.get(name)

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
    """给出可操作的 TLS 排查建议——**按当前实际生效的信任链**来说。

    原先是写死的三条建议，不看用户处在哪种情形。实测最坑的一种是：truststore
    已经装好、也确实在生效，但中间人的根证书**连操作系统信任库都没有**——此时
    「请 pip install truststore」纯属误导，用户会去装一个早就装好的包。
    """
    lines = [
        "TLS 证书校验失败。常见原因是环境里有 HTTPS 中间人"
        "（企业网关 / 公司自签 CA / 本机抓包代理）。",
        f"  当前生效的信任链：{ca_bundle_hint()}",
    ]

    if _lookup(ENV_CA_BUNDLE):
        lines.append(
            "  已指定 FISSUE_CA_BUNDLE 仍失败 → 确认这个 PEM 就是签发该证书的那个根；"
            "代理套代理时不止一张根证书，需要把它们**合并**进同一个文件"
        )
    elif _truststore_available():
        lines.append(
            "  truststore 已在生效，说明这张根证书**连操作系统信任库也没有**"
            "（浏览器打得开，只是因为浏览器装过它）。"
            "做法：把该根证书导出为 PEM，再设 FISSUE_CA_BUNDLE=/path/to/root.pem"
            "（写进 .env 即可，不必每次 export）"
        )
    else:
        lines.append(
            "  方案 1（推荐）：pip install truststore —— 改用操作系统信任库，"
            "「浏览器能打开的站点，Fissue 也能打开」"
        )
        lines.append(
            "  方案 2：把根证书导出为 PEM，设 FISSUE_CA_BUNDLE=/path/to/root.pem"
        )

    lines.append("  备选：把根证书装进操作系统信任库（需管理员权限），此后无需任何配置")
    lines.append("  仅排查用（有中间人风险）：临时设 FISSUE_INSECURE_SKIP_VERIFY=1")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def build_ssl_context() -> ssl.SSLContext | bool:
    """构建 httpx 的 ``verify`` 参数。

    返回 ``ssl.SSLContext``（正常）或 ``False``（显式关闭校验）。
    """
    # 1) 显式关闭校验（仅排查）
    if _truthy(_lookup(ENV_INSECURE)):
        log.warning(
            "已通过 %s 关闭 TLS 证书校验——存在中间人攻击风险，仅限本地排查使用",
            ENV_INSECURE,
        )
        return False

    # 2) 显式指定 CA 文件
    bundle = _lookup(ENV_CA_BUNDLE)
    if bundle:
        # `~` 必须展开：在 .env 里写 `~/ca.pem` 是常见写法，不展开就等于「文件不存在」。
        path = os.path.expanduser(bundle)
        if not os.path.isfile(path):
            raise ConfigError(
                f"{ENV_CA_BUNDLE} 指向的文件不存在：{bundle}（解析为 {path}）。"
                "显式指定了 CA 却不可用**绝不静默回退**到系统信任库——那样运维以为只信"
                "某一张根证书，实际用的是 OS 信任库，属于「安全配置被无声忽略」。"
                "请修正路径，或删掉这个变量改用系统信任库（默认行为）。"
            )
        try:
            ctx = ssl.create_default_context(cafile=path)
            log.debug("使用 %s 指定的 CA：%s", ENV_CA_BUNDLE, path)
            return ctx
        except Exception as exc:
            raise ConfigError(
                f"加载 {ENV_CA_BUNDLE}={bundle} 失败：{exc}。"
                "显式指定的 CA 加载不了就不静默回退——请修正该路径，或删掉这个变量。"
            ) from exc

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
    if _truthy(_lookup(ENV_INSECURE)):
        return "已关闭校验（FISSUE_INSECURE_SKIP_VERIFY）"
    if bundle := _lookup(ENV_CA_BUNDLE):
        return f"自定义 CA：{bundle}"
    if _truststore_available():
        return "操作系统信任库（truststore）"
    return "certifi 默认 CA 列表（建议 pip install truststore）"


def _truststore_available() -> bool:
    """truststore 是否可用（可用即意味着走操作系统信任库）。"""
    try:
        import truststore  # noqa: F401
    except ImportError:
        return False
    return True


def reset_cache() -> None:
    """清缓存（测试用：改环境变量后需重算）。"""
    build_ssl_context.cache_clear()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "y")
