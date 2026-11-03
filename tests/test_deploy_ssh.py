"""SSH 执行层测试：全部用假 paramiko 客户端，**不碰真实网络**。

为什么单独造 fake 而不是 `unittest.mock.MagicMock`：本模块真正依赖的只有
`exec_command` / `open_sftp` / `get_transport` 这几个面，显式的假对象能把
「我们到底调了什么」写死在测试里，读起来比一串 `mock.assert_called` 直观。
"""

from __future__ import annotations

import base64
import io
import shlex

import pytest

from fissue.deploy import ssh as ssh_mod
from fissue.deploy.schema import AuthMethod
from fissue.deploy.ssh import (
    HostKeyMismatch,
    SSHChannel,
    SSHError,
    SSHTarget,
    connect,
    fingerprint_of,
)

# ---------------------------------------------------------------------------
# 假 paramiko 对象
# ---------------------------------------------------------------------------


class FakeKey:
    """假的 paramiko.PKey：只需要 get_base64()。"""

    def __init__(self, raw: bytes = b"fake-host-public-key") -> None:
        self.raw = raw

    def get_base64(self) -> str:
        return base64.b64encode(self.raw).decode("ascii")


class FakeTransport:
    def __init__(self, key: FakeKey) -> None:
        self._key = key

    def get_remote_server_key(self) -> FakeKey:
        return self._key


class FakeStdin:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChannel:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def recv_exit_status(self) -> int:
        return self._exit_code


class FakeOutput(io.BytesIO):
    """模拟 paramiko 的 ChannelFile：读字节，且能回带退出码。"""

    def __init__(self, data: bytes, exit_code: int) -> None:
        super().__init__(data)
        self.channel = FakeChannel(exit_code)


class FakeSFTPFile(io.BytesIO):
    def __enter__(self) -> FakeSFTPFile:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakeSFTP:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.modes: dict[str, int] = {}
        self.closed = False

    def open(self, path: str, mode: str = "r"):
        if "w" in mode:
            sftp = self

            class _Writer(FakeSFTPFile):
                def close(self) -> None:
                    if not self.closed:
                        sftp.files[path] = self.getvalue()
                    super().close()

            return _Writer()
        return FakeSFTPFile(self.files.get(path, b""))

    def chmod(self, path: str, mode: int) -> None:
        self.modes[path] = mode

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """假 SSHClient：可配置命令结果、主机公钥与连接期异常。"""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        key: FakeKey | None = None,
        connect_error: BaseException | None = None,
        exec_error: BaseException | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.key = key or FakeKey()
        self.connect_error = connect_error
        self.exec_error = exec_error
        self.commands: list[str] = []
        self.connect_kwargs: dict[str, object] = {}
        self.sftp = FakeSFTP()
        self.closed = False

    # -- SSHClient 用到的面 -------------------------------------------------
    def set_missing_host_key_policy(self, policy) -> None:  # noqa: ANN001
        self.policy = policy

    def connect(self, address: str, **kwargs) -> None:  # noqa: ANN003
        self.address = address
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def get_transport(self) -> FakeTransport:
        return FakeTransport(self.key)

    def exec_command(self, command: str, timeout: float | None = None):
        self.commands.append(command)
        if self.exec_error is not None:
            raise self.exec_error
        return FakeStdin(), FakeOutput(self.stdout, self.exit_code), FakeOutput(self.stderr, 0)

    def open_sftp(self) -> FakeSFTP:
        return self.sftp

    def close(self) -> None:
        self.closed = True


def _target(**kwargs) -> SSHTarget:
    base = {
        "name": "web-1",
        "address": "10.0.0.5",
        "user": "deploy",
        "auth_method": AuthMethod.PASSWORD,
        "credential": "s3cret",
    }
    base.update(kwargs)
    return SSHTarget(**base)


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------


def test_run_returns_nonzero_without_raising() -> None:
    """远端命令失败很常见（is-active 返回 3），传输层必须如实返回而不是抛。"""
    client = FakeClient(exit_code=3, stdout=b"inactive\n", stderr=b"boom\n")
    channel = SSHChannel(client, _target())

    result = channel.run("systemctl is-active app")

    assert result.exit_code == 3
    assert result.ok is False
    assert result.stdout == "inactive\n"
    assert result.stderr == "boom\n"
    assert result.duration >= 0


def test_run_quotes_cwd() -> None:
    """cwd 直接拼进 shell，必须引号包裹——否则空格/分号就是命令注入。"""
    client = FakeClient()
    channel = SSHChannel(client, _target())

    evil_cwd = "/opt/my app; rm -rf /tmp/x"
    channel.run("ls -la", cwd=evil_cwd)

    executed = client.commands[-1]
    assert executed == f"cd {shlex.quote(evil_cwd)} && ls -la"
    # 裸拼的话这条会变成两条命令；引号包裹后整个路径仍在同一对单引号里。
    assert "cd /opt/my app;" not in executed


# ---------------------------------------------------------------------------
# 主机指纹
# ---------------------------------------------------------------------------


def test_connect_rejects_mismatched_fingerprint(monkeypatch) -> None:
    """登记过指纹后不匹配**无条件**拒绝——这是中间人防线，不能覆盖继续。"""
    client = FakeClient(key=FakeKey(b"attacker-key"))
    monkeypatch.setattr(ssh_mod.paramiko, "SSHClient", lambda: client)
    target = _target(host_fingerprint="SHA256:registered-but-different")

    with pytest.raises(HostKeyMismatch) as info:
        connect(target)

    assert info.value.expected == "SHA256:registered-but-different"
    assert info.value.actual == fingerprint_of(FakeKey(b"attacker-key"))
    assert client.closed, "校验失败必须关掉已建立的连接"


def test_connect_accepts_and_records_new_fingerprint(monkeypatch) -> None:
    """首次连接（TOFU）：接受并记录实际指纹，供之后比对。"""
    key = FakeKey(b"brand-new-host-key")
    client = FakeClient(key=key)
    monkeypatch.setattr(ssh_mod.paramiko, "SSHClient", lambda: client)
    target = _target(host_fingerprint=None)

    channel = connect(target, accept_new_host_key=True)

    assert channel.host_fingerprint == fingerprint_of(key)
    assert not client.closed


def test_connect_without_fingerprint_and_no_tofu(monkeypatch) -> None:
    """明确不允许 TOFU 时，缺指纹同样拒绝，而不是悄悄接受。"""
    client = FakeClient()
    monkeypatch.setattr(ssh_mod.paramiko, "SSHClient", lambda: client)
    target = _target(host_fingerprint=None)

    with pytest.raises(HostKeyMismatch) as info:
        connect(target, accept_new_host_key=False)

    assert info.value.expected == ""
    assert client.closed


def test_fingerprint_format() -> None:
    """指纹用 OpenSSH 同款格式：SHA256: + base64（无 padding）。"""
    fp = fingerprint_of(FakeKey(b"abc"))

    assert fp.startswith("SHA256:")
    assert not fp.endswith("=")


# ---------------------------------------------------------------------------
# 传文件
# ---------------------------------------------------------------------------


def test_put_text_writes_content_and_mode() -> None:
    """put_text 走 SFTP 写正文并 chmod；不依赖本地文件系统语义。"""
    client = FakeClient()
    channel = SSHChannel(client, _target())

    channel.put_text("server {\n  listen 80;\n}\n", "/etc/nginx/conf.d/app.conf", "0640")

    sftp = client.sftp
    assert sftp.files["/etc/nginx/conf.d/app.conf"] == b"server {\n  listen 80;\n}\n"
    assert sftp.modes["/etc/nginx/conf.d/app.conf"] == 0o640
    assert sftp.closed, "用完要关掉 SFTP 句柄"


# ---------------------------------------------------------------------------
# 异常包装
# ---------------------------------------------------------------------------


def test_auth_error_wrapped_without_credential(monkeypatch) -> None:
    """认证失败包成 SSHError，且消息里绝不能出现密码明文。"""
    secret = "hunter2-very-secret"
    client = FakeClient(connect_error=RuntimeError(f"Authentication failed: {secret}"))
    monkeypatch.setattr(ssh_mod.paramiko, "SSHClient", lambda: client)

    target = _target(credential=secret, auth_method=AuthMethod.PASSWORD)
    with pytest.raises(SSHError) as info:
        connect(target)

    assert secret not in str(info.value)
    assert "<已隐藏>" in str(info.value)
    assert client.closed


def test_connect_error_wrapped_without_key_material(monkeypatch) -> None:
    """私钥解析/连接失败同样包成 SSHError，消息里不带私钥正文。"""
    key_body = "-----BEGIN OPENSSH PRIVATE KEY-----\nTOPSECRET\n-----END OPENSSH PRIVATE KEY-----"
    client = FakeClient()
    monkeypatch.setattr(ssh_mod.paramiko, "SSHClient", lambda: client)
    target = _target(credential=key_body, auth_method=AuthMethod.KEY)

    with pytest.raises(SSHError) as info:
        connect(target)

    assert "TOPSECRET" not in str(info.value)
    assert client.closed