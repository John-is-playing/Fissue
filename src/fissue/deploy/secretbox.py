"""部署凭据的可逆加密。

为什么不能像 API Key 那样只存哈希：SSH 私钥与主机密码是**要用**的凭据，服务
必须还原出原文才能拿去认证。所以这里的形态是「可逆加密 + 密钥不进数据库」。

密钥来源：``auth/files.py::read_or_create_server_secret`` 产出的
``data/server.secret``（首次运行自动生成，权限 0600）。它已经用于签名会话
cookie，这里**用 HKDF 派生出另一把专用密钥**，而不是直接拿它当对称密钥：
同一个秘密被两处不同用途直接使用，一旦其中一处的用法被削弱（比如某天改成
低熵派生），另一处跟着一起弱掉。派生时带 ``info`` 标签，两把密钥在数学上
互不可推。
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..errors import FissueError

#: 落库密文前缀。带前缀是为了让「这是密文还是明文」在数据库里一眼可辨，
#: 也让未来的密钥轮换能按前缀区分新旧格式。
TOKEN_PREFIX = "fissuebox:v1:"

_HKDF_SALT = b"fissue-deploy-secretbox"
_HKDF_INFO = b"deploy-credential"


class SecretBoxError(FissueError):
    """加解密失败（密钥换了、密文被改过、或数据不是本模块产出的）。"""


def derive_fernet_key(server_secret: bytes) -> bytes:
    """从服务器秘密派生 Fernet 密钥（32 字节 → urlsafe base64）。"""
    if not server_secret:
        raise SecretBoxError("服务器秘密为空，无法派生凭据密钥")
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(server_secret)
    return base64.urlsafe_b64encode(raw)


def encrypt(server_secret: bytes, plaintext: str) -> str:
    """加密并返回带前缀的密文。"""
    if plaintext == "":
        raise SecretBoxError("凭据内容为空")
    token = Fernet(derive_fernet_key(server_secret)).encrypt(plaintext.encode("utf-8"))
    return TOKEN_PREFIX + token.decode("ascii")


def decrypt(server_secret: bytes, sealed: str) -> str:
    """解密。格式不对或被篡改一律抛 :class:`SecretBoxError`，**不返回空串**。"""
    if not sealed.startswith(TOKEN_PREFIX):
        raise SecretBoxError("密文格式不认识（缺少 fissuebox:v1: 前缀）")
    token = sealed[len(TOKEN_PREFIX) :].encode("ascii", "strict")
    try:
        return Fernet(derive_fernet_key(server_secret)).decrypt(token).decode("utf-8")
    except InvalidToken as exc:  # 密钥不对，或密文被改动过
        raise SecretBoxError("凭据解密失败：密钥不匹配或密文已损坏") from exc


class CredentialBox:
    """绑定一台服务器秘密的加解密门面（调用方不必反复传 secret）。"""

    def __init__(self, server_secret: bytes) -> None:
        self._secret = server_secret

    @classmethod
    def from_settings(cls, settings) -> CredentialBox:
        from ..auth.files import read_or_create_server_secret

        return cls(read_or_create_server_secret(settings))

    def seal(self, plaintext: str) -> str:
        return encrypt(self._secret, plaintext)

    def open(self, sealed: str) -> str:
        return decrypt(self._secret, sealed)


def redact(sealed: str) -> str:
    """给日志/API 回显用的脱敏串——只暴露「这里有个凭据」。"""
    return f"{TOKEN_PREFIX}***" if sealed.startswith(TOKEN_PREFIX) else "***"