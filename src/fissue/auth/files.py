"""``data/`` 下的两个敏感文件：服务器签名密钥与一次性引导口令。

为什么密钥**不进 config.yaml / .env**（docs/AUTH.md §4）：那两份是会被设置页整体重写、
被用户随手复制粘贴、被 commit 的文件。签名密钥要的是「生成一次、之后只读」，
它的轮换语义是「让所有会话失效」——放进通用配置写入器里，等于给了它一个被误伤的机会。
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger(__name__)

#: 会话 cookie 签名密钥（HMAC）。
SERVER_SECRET_FILE = "auth-secret.key"
#: 首次启动时生成的一次性引导口令（用后即焚）。
BOOTSTRAP_TOKEN_FILE = "auth-bootstrap-token"

_PRIVATE_MODE = 0o600


def _data_dir(settings: Settings) -> Path:
    """数据目录（``Settings.data_dir`` 顺手会创建它）。"""
    return Path(settings.data_dir)


def server_secret_path(settings: Settings) -> Path:
    return _data_dir(settings) / SERVER_SECRET_FILE


def bootstrap_token_path(settings: Settings) -> Path:
    return _data_dir(settings) / BOOTSTRAP_TOKEN_FILE


def _write_private(path: Path, content: str) -> None:
    """以 0600 落盘。

    用 ``os.open(..., mode)`` 而不是「先 open 再 chmod」：后者在两次调用之间有一小段
    文件是「按 umask 可读」的窗口。Windows 上 mode 基本被忽略（它用 ACL 而不是 POSIX 位），
    所以额外补一次 ``chmod`` 尽力而为——不成功也不该让启动失败。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    with contextlib.suppress(OSError):
        os.chmod(path, _PRIVATE_MODE)


# ---------------------------------------------------------------------------
# 服务器签名密钥
# ---------------------------------------------------------------------------


def read_or_create_server_secret(settings: Settings) -> bytes:
    """读取签名密钥；不存在就生成一个（32 字节，十六进制存盘）。

    并发启动两个进程时可能各自生成一份、后者覆盖前者——后果是先前那批会话 cookie
    验不过（用户重新登录即可），**不会**造成「谁能进来」的漏洞：签名只用来快速打回伪造串，
    真正的身份判定始终是数据库里的会话记录。这个取舍比在启动路径上加文件锁更划算。
    """
    path = server_secret_path(settings)
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                return bytes.fromhex(raw)
            except ValueError:
                log.warning("签名密钥文件内容不是十六进制，将重新生成：%s", path)
    secret = secrets.token_bytes(32)
    _write_private(path, secret.hex())
    log.info("已生成会话签名密钥：%s", path)
    return secret


# ---------------------------------------------------------------------------
# 一次性引导口令
# ---------------------------------------------------------------------------


def issue_bootstrap_token(settings: Settings) -> str:
    """生成引导口令并写入文件，返回明文（调用方负责打印到控制台）。"""
    token = secrets.token_urlsafe(24)
    _write_private(bootstrap_token_path(settings), token)
    return token


def read_bootstrap_token(settings: Settings) -> str:
    path = bootstrap_token_path(settings)
    if not path.exists():
        return ""
    with contextlib.suppress(OSError):
        return path.read_text(encoding="utf-8").strip()
    return ""


def consume_bootstrap_token(settings: Settings) -> None:
    """删掉引导口令文件 —— **用后即焚**。

    找不到文件不算错：两次并发引导里后到的那次会走到这里，它本来就该是空操作。
    但**必须真的删**——留着等于给「已经建好管理员的系统」永久留了一把初始化钥匙。
    """
    path = bootstrap_token_path(settings)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
        log.info("引导口令已使用并销毁：%s", path)
