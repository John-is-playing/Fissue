"""SSH 执行层：部署链路的**唯一**远端出口。

安全边界见 ``docs/DEPLOY.md`` §1 与 §4：

* **主机指纹 TOFU**。首次连接（``host_fingerprint`` 为空）在
  ``accept_new_host_key=True`` 时接受并把实际指纹记到
  :attr:`SSHChannel.host_fingerprint`；一旦登记过，不匹配就**无条件**抛
  :class:`HostKeyMismatch`——这是中间人防线，不允许「覆盖一下继续」。
* **凭据只进不出**。``SSHTarget.credential`` 是调用方从加密存储解出来的明文，
  本模块不落盘、不记日志、不拼进异常消息。日志里只出现地址、用户名与指纹。

为什么不用 async：``paramiko`` 是同步库。阻塞调用由执行器放进
``asyncio.to_thread``，本模块保持纯粹的同步语义，避免把线程池细节扩散到每一层。
"""

from __future__ import annotations

import base64
import hashlib
import shlex
import socket
import threading
import time
from contextlib import closing
from io import StringIO
from pathlib import Path
from typing import Any

import paramiko
from pydantic import BaseModel

from ..errors import FissueError
from ..logging_setup import get_logger
from .schema import AuthMethod, HostSpec

log = get_logger(__name__)

#: 读超时在不同层会以不同身份冒出来（``socket.timeout`` 自 3.10 起就是
#: ``TimeoutError`` 的别名；paramiko 有时直接抛前者）。统一按这一组捕获。
_TIMEOUT_ERRORS = (TimeoutError, socket.timeout)

#: 依次尝试的私钥类型。不能假设只有 RSA——ed25519 早已是 OpenSSH 默认。
_KEY_TYPES = (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key)


class SSHError(FissueError):
    """SSH 连接、认证或命令执行失败。"""


class HostKeyMismatch(SSHError):
    """远端主机公钥与登记指纹不一致。"""

    def __init__(self, host: str, expected: str, actual: str) -> None:
        # 只比指纹，不比 key 本体；指纹不是秘密，进消息便于人工核对。
        self.host = host
        self.expected = expected
        self.actual = actual
        if expected:
            message = (
                f"主机 {host} 的公钥指纹与登记值不一致（中间人风险），已拒绝连接。\n"
                f"  登记：{expected}\n"
                f"  实际：{actual}\n"
                "若确认换了机器/重装了系统，请由人工更新登记指纹后再试。"
            )
        else:
            message = (
                f"主机 {host} 没有登记指纹，且当前不允许首次信任（accept_new_host_key=False）。\n"
                f"  实际：{actual}"
            )
        super().__init__(message)


class SSHResult(BaseModel):
    """一次远端命令的执行结果。

    非 0 退出码**不是异常**：远端命令失败是常态（``systemctl is-active`` 返回
    3 表示没起来），要如实交给 guard/runner 判定，而不是在传输层就炸掉。
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SSHTarget(BaseModel):
    """一次连接所需的一切（凭据已解密）。"""

    name: str
    address: str
    port: int = 22
    user: str = "root"
    auth_method: AuthMethod = AuthMethod.KEY
    #: 私钥正文或密码的**明文**（由调用方从加密存储解出来再传进来，本模块不落盘不记日志）
    credential: str = ""
    #: 首次连接为空；之后是登记过的 "SHA256:xxxx" 指纹
    host_fingerprint: str | None = None
    timeout: float = 15.0

    @classmethod
    def from_host(cls, host: HostSpec, credential: str, *, timeout: float = 15.0) -> SSHTarget:
        """从只读的 :class:`HostSpec` + 解密后的凭据组装目标。"""
        return cls(
            name=host.name,
            address=host.address,
            port=host.port,
            user=host.user,
            auth_method=host.auth_method,
            credential=credential,
            host_fingerprint=host.host_fingerprint,
            timeout=timeout,
        )


def fingerprint_of(key: Any) -> str:
    """``paramiko.PKey`` → ``"SHA256:base64noPadding"``（OpenSSH 同款算法）。

    与 ``ssh-keygen -lf`` 输出一致：对公钥的 wire 编码（``get_base64`` 的内容）
    做 SHA-256，再 base64 去掉尾部 ``=``。截断/带 padding 会让同一台机器在
    OpenSSH 与 Fissue 里显示两个指纹，排障时极易误判为「两台机器」。
    """
    raw = base64.b64decode(key.get_base64())
    digest = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class SSHChannel:
    """一个可复用的 SSH 连接；上下文管理器必须保证 close()。"""

    def __init__(self, client: Any, target: SSHTarget) -> None:
        self.client = client
        self.target = target
        #: 实际连接的指纹。``connect`` 校验通过后写入；直接构造（测试/复用已建连接）
        #: 时先沿用目标里登记的值。
        self.host_fingerprint: str | None = target.host_fingerprint

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------

    def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> SSHResult:
        """执行命令；非 0 退出码**不抛异常**，如实返回 :class:`SSHResult`。

        ``cwd`` 一律经 ``shlex.quote`` 包裹：路径里一个空格或分号就能把
        ``cd /opt/my app && ...`` 变成两条命令，这是计划里的 ``cwd`` 字段
        被拿来做命令拼接时最直接的注入面。
        """
        full = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        limit = float(self.target.timeout if timeout is None else timeout)
        started = time.monotonic()
        log.debug("ssh %s: 执行 %s（超时 %.0fs）", self.target.address, full, limit)

        try:
            stdin, stdout, stderr = self.client.exec_command(full, timeout=limit)
        except _TIMEOUT_ERRORS as exc:
            raise SSHError(f"命令执行超时（{limit:g}s）：{full}") from exc
        except Exception as exc:  # 通道打不开等：不把底层细节原样上抛
            raise SSHError(
                f"在 {self.target.address} 上执行命令失败：{type(exc).__name__}"
            ) from exc

        try:
            out, err = _read_both(stdout, stderr)
        except _TIMEOUT_ERRORS as exc:
            raise SSHError(f"读取命令输出超时（{limit:g}s）：{full}") from exc
        except Exception as exc:
            raise SSHError(f"读取命令输出失败：{type(exc).__name__}") from exc
        finally:
            _close_quietly(stdin)

        result = SSHResult(
            exit_code=_exit_code(stdout),
            stdout=out,
            stderr=err,
            duration=time.monotonic() - started,
        )
        log.debug(
            "ssh %s: 退出码 %s（%.2fs）", self.target.address, result.exit_code, result.duration
        )
        return result

    # ------------------------------------------------------------------
    # 传文件
    # ------------------------------------------------------------------

    def put_text(self, text: str, dst: str, mode: str = "0644") -> None:
        """把文本写到远端 ``dst`` 并设置权限。"""
        self._write_remote(text.encode("utf-8"), dst, mode)

    def put_file(self, local: str | Path, dst: str, mode: str = "0644") -> None:
        """把本地文件内容写到远端 ``dst``。

        先整份读进内存再走 SFTP：部署产物都是配置/脚本级别的小文件，读一次换来
        更简单的语义；原子写（临时文件 + rename）由 runner 负责，不塞进传输层。
        """
        data = Path(local).read_bytes()
        self._write_remote(data, dst, mode)

    def read_text(self, path: str, *, max_bytes: int = 65536) -> str:
        """读远端文本；默认最多 64KB（日志/配置够用，防止把大文件拖回来）。"""
        sftp = self._open_sftp()
        try:
            with closing(sftp.open(path, "rb")) as fh:
                data = fh.read(max_bytes)
        except Exception as exc:
            raise SSHError(
                f"读取远端文件 {path} 失败：{type(exc).__name__}"
            ) from exc
        finally:
            _close_quietly(sftp)
        if isinstance(data, str):
            return data
        return (data or b"").decode("utf-8", errors="replace")

    def _write_remote(self, data: bytes, dst: str, mode: str) -> None:
        try:
            perm = int(str(mode), 8)
        except ValueError as exc:
            raise SSHError(f"非法的文件权限：{mode!r}") from exc
        sftp = self._open_sftp()
        try:
            with closing(sftp.open(dst, "wb")) as fh:
                fh.write(data)
            sftp.chmod(dst, perm)
            log.debug("ssh %s: 写入 %s（%d 字节，mode %s）", self.target.address, dst, len(data), mode)
        except Exception as exc:
            raise SSHError(
                f"写入远端文件 {dst} 失败：{type(exc).__name__}"
            ) from exc
        finally:
            _close_quietly(sftp)

    def _open_sftp(self) -> Any:
        try:
            return self.client.open_sftp()
        except Exception as exc:
            raise SSHError(f"打开 SFTP 失败：{type(exc).__name__}") from exc

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        _close_quietly(self.client)

    def __enter__(self) -> SSHChannel:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 连接与主机指纹
# ---------------------------------------------------------------------------


def connect(target: SSHTarget, *, accept_new_host_key: bool = True) -> SSHChannel:
    """建立连接并做主机指纹校验（TOFU）。

    顺序是「先连、后校验」：paramiko 的 ``RejectPolicy`` 会在指纹校验之前就把未知
    主机拒掉，那样 TOFU 永远无从记录。因此这里用 ``AutoAddPolicy`` 让握手完成，
    再拿 transport 上的**实际**公钥与我们登记的值比对——校验逻辑始终在我们手里。
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs: dict[str, Any] = {
        "port": target.port,
        "username": target.user,
        "timeout": target.timeout,
        # 明确关掉隐式来源：凭据只从 credential 来，不读本机任何默认材料，
        # 否则认证行为不可解释。
        "allow_agent": False,
        "look_for_keys": False,
    }
    try:
        if target.auth_method is AuthMethod.KEY:
            kwargs["pkey"] = _load_private_key(target.credential)
        else:
            kwargs["password"] = target.credential
        client.connect(target.address, **kwargs)
    except Exception as exc:
        # 私钥解析失败也要走到这里：连接没建起来，客户端就不能留在外面。
        _close_quietly(client)
        raise SSHError(_connect_failure_message(target, exc)) from exc

    try:
        transport = client.get_transport()
        if transport is None:
            raise SSHError(f"与 {target.address} 的 SSH 传输层未建立")
        actual = fingerprint_of(transport.get_remote_server_key())
    except SSHError:
        _close_quietly(client)
        raise
    except Exception as exc:
        _close_quietly(client)
        raise SSHError(f"无法读取 {target.address} 的主机公钥：{type(exc).__name__}") from exc

    expected = (target.host_fingerprint or "").strip()
    if not expected:
        if not accept_new_host_key:
            _close_quietly(client)
            raise HostKeyMismatch(target.address, "", actual)
        channel = SSHChannel(client, target)
        channel.host_fingerprint = actual
        log.info("首次连接 %s，已登记主机指纹 %s", target.address, actual)
        return channel

    if expected != actual:
        # 中间人防线：登记过就不接受覆盖，连「告警后继续」都不给。
        _close_quietly(client)
        raise HostKeyMismatch(target.address, expected, actual)

    channel = SSHChannel(client, target)
    channel.host_fingerprint = actual
    return channel


def _load_private_key(credential: str) -> Any:
    """依次按 RSA / ECDSA / Ed25519 解析私钥正文。

    解析失败的消息不带任何正文片段——凭据泄露往往就是从一条「看起来很贴心」的
    错误日志开始的。
    """
    if not credential.strip():
        raise SSHError("私钥为空（credential 未提供）")
    for key_type in _KEY_TYPES:
        try:
            return key_type.from_private_key(StringIO(credential))
        except Exception:
            continue
    raise SSHError("无法解析 SSH 私钥（已尝试 RSA / ECDSA / Ed25519）")


def _connect_failure_message(target: SSHTarget, exc: BaseException) -> str:
    """组装连接失败信息，并**确保**不含 credential 明文。

    paramiko 的认证失败消息通常只有 ``Authentication failed.``，但第三方异常、
    URL 形式的地址等仍可能把输入回显出来。这里统一做一次替换，且只在凭据非空时
    替换——空串 ``replace("", ...)`` 会在每个字符间插东西。
    """
    text = f"{type(exc).__name__}: {exc}"
    if target.credential:
        text = text.replace(target.credential, "<已隐藏>")
    return f"连接 {target.user}@{target.address}:{target.port} 失败：{text}"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _read_stream(stream: Any) -> str:
    if stream is None:
        return ""
    data = stream.read()
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _read_both(stdout: Any, stderr: Any) -> tuple[str, str]:
    """并发读 stdout/stderr。

    顺序读会死锁：paramiko 的 stdout/stderr 共用同一个窗口，只读 stdout 时
    stderr 一旦把窗口塞满，远端写不进去、stdout 也就等不到 EOF。``pip install``
    之类的命令恰恰是 stderr 输出大户。所以 stderr 放线程里读。
    """
    if stderr is None:
        return _read_stream(stdout), ""
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["stderr"] = _read_stream(stderr)
        except BaseException as exc:  # noqa: BLE001 - 原样交给主线程处理
            box["error"] = exc

    worker = threading.Thread(target=_worker, name="fissue-ssh-stderr", daemon=True)
    worker.start()
    try:
        out = _read_stream(stdout)
    finally:
        worker.join()
    if "error" in box:
        raise box["error"]
    return out, box.get("stderr", "")


def _exit_code(stdout: Any) -> int:
    """取退出码。

    ``paramiko.ChannelFile`` 会把底层 channel 挂在 ``.channel`` 上；用 ``getattr``
    是为了假客户端（测试里用 ``BytesIO`` 顶替）也能走同一条路径——解析失败时按
    「没有退出码」处理，而不是让整次执行炸在传输层。
    """
    channel = getattr(stdout, "channel", None)
    code = getattr(channel, "recv_exit_status", None)
    if callable(code):
        return int(code())
    return 0


def _close_quietly(obj: Any) -> None:
    """关掉可能不存在的句柄；关闭失败不该盖住真正要报的错。"""
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # pragma: no cover - 关闭失败无补救价值
            log.debug("关闭 SSH 资源失败", exc_info=True)