"""转发组件（宿主 ↔ 沙盒通道）的单元测试。

为什么单独补这一块：它是**宿主与不可信代码之间唯一的通道**，而全量覆盖率里
``sandbox/forwarder.py`` 只有 62%——全项目最低。低覆盖在别处只是「少测了几条分支」，
在这里意味着隔离边界上有一段没人验证过的代码。

测试策略：真实 AF_UNIX socket 只在 POSIX 上可用（Windows 会走 inline 模式），
所以分两层——
* **平台无关**：用假 socket 钉协议行为（按请求算超时、对端关闭、连不上）；
* **仅 POSIX**：真起监听、真发一帧、验证 chmod 0600 与 socket 文件清理。
"""

from __future__ import annotations

import os
import socket
import stat
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from fissue.config import SandboxConfig, SandboxForwarderConfig, SandboxMountConfig
from fissue.errors import SandboxError
from fissue.sandbox.forwarder import (
    ForwarderClient,
    ForwarderServer,
    SandboxManager,
    Workspace,
)
from fissue.sandbox.protocol import ExecRequest, ExecResult, send_frame

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="AF_UNIX 只在 POSIX 上可用")


def _cfg(**forwarder: Any) -> SandboxConfig:
    """一份「启用容器沙盒 + 指向临时 socket」的配置。"""
    return SandboxConfig(
        enabled=True,
        runtime="docker",
        forwarder=SandboxForwarderConfig(**forwarder),
    )


# ---------------------------------------------------------------------------
# Workspace：loopback 虚拟盘的两条失败路径
# ---------------------------------------------------------------------------


def _posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """把环境假成 POSIX，绕开「Windows 不支持 Unix socket」的守卫。

    两件事一起做：``os.name`` 改成 posix，并在缺失时补一个 ``socket.AF_UNIX``
    常量（Windows 的 socket 模块没有它，代码里 ``socket.socket(socket.AF_UNIX, ...)``
    会先 AttributeError，根本走不到后面的守卫）。

    ⚠️ 只能在**不碰 pathlib** 的用例里用：Windows 上 ``os.name`` 一变成 posix，
    ``pathlib`` 就会去实例化 ``PosixPath`` 并抛 ``UnsupportedOperation``。
    所以 loopback 那几条（要建镜像文件、必然用 Path）标了 POSIX_ONLY。
    """
    monkeypatch.setattr(os, "name", "posix")
    if not hasattr(socket, "AF_UNIX"):          # pragma: no cover - 仅 Windows
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(os, "name", "posix")


def test_loopback_skipped_off_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 POSIX 直接回退临时目录，且**不尝试**创建镜像文件。"""
    # 只把 forwarder 模块看到的 os 假成 nt：直接改全局 os.name 会让 pathlib 在同一
    # 进程里也切成 WindowsPath，POSIX 上就再也建不出真实路径（这条用例跑不起来）。
    monkeypatch.setattr("fissue.sandbox.forwarder.os", types.SimpleNamespace(name="nt"))
    cfg = SandboxConfig(enabled=True, mounts=SandboxMountConfig(type="loopback", size_mb=64))
    ws = Workspace(cfg, label="t")
    root = ws.create()
    try:
        assert root.exists()
        assert ws._loop_device is None
        assert ws._image_file is None, "非 POSIX 上不该留下镜像文件"
    finally:
        ws.destroy()


@POSIX_ONLY
def test_loopback_falls_back_when_tools_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 mkfs/losetup 时**静默回退**普通临时目录，而不是让整条链路失败。

    这是刻意的：虚拟盘的目的是「只挂载隔离盘」，而缺工具的环境（容器内、CI）
    本来就不该因此跑不了验证。
    """
    _posix(monkeypatch)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("mkfs.ext4: not found")

    monkeypatch.setattr("subprocess.run", boom)
    cfg = SandboxConfig(enabled=True, mounts=SandboxMountConfig(type="loopback", size_mb=64))
    ws = Workspace(cfg, label="t")
    root = ws.create()
    try:
        assert root.exists(), "回退后工作区依然可用"
        assert ws._loop_device is None
    finally:
        ws.destroy()


@POSIX_ONLY
def test_loopback_mounts_and_unmounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工具齐备时挂上镜像，``destroy`` 必须 umount + losetup -d + 删镜像。"""
    _posix(monkeypatch)
    calls: list[list[str]] = []

    class _Done:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        if argv[0] == "losetup" and "--show" in argv:
            return _Done("/dev/loop7\n")
        return _Done()

    monkeypatch.setattr("subprocess.run", fake_run)
    cfg = SandboxConfig(enabled=True, mounts=SandboxMountConfig(type="loopback", size_mb=64))
    ws = Workspace(cfg, label="t")
    root = ws.create()
    assert ws._loop_device == "/dev/loop7"
    image = ws._image_file
    assert image is not None and image.exists(), "镜像文件应先被 truncate 出来"
    assert any(c[:1] == ["mkfs.ext4"] for c in calls)
    assert any(c[:1] == ["mount"] for c in calls)

    ws.destroy()
    assert any(c[:2] == ["umount", "/dev/loop7"] for c in calls)
    assert any(c[:2] == ["losetup", "-d"] for c in calls)
    assert not root.exists()
    assert not image.exists(), "轮转文件也要清掉，否则临时目录里会堆满大文件"


@POSIX_ONLY
def test_loopback_rejects_empty_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``losetup --show`` 不给设备名算失败——不能拿空字符串去 mount。"""
    _posix(monkeypatch)

    class _Done:
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Done())
    cfg = SandboxConfig(enabled=True, mounts=SandboxMountConfig(type="loopback", size_mb=64))
    ws = Workspace(cfg, label="t")
    ws.create()
    try:
        assert ws._loop_device is None
    finally:
        ws.destroy()


def test_materialize_rejects_escape(tmp_path: Path) -> None:
    """写进工作区的相对路径不许跑出工作区（``..`` 直接拒绝）。"""
    cfg = SandboxConfig(enabled=True, mounts=SandboxMountConfig(type="volume"))
    ws = Workspace(cfg, label="t")
    ws.create()
    try:
        ws.materialize({"ok.txt": "hi"})
        assert (ws.root / "ok.txt").read_text(encoding="utf-8") == "hi"  # type: ignore[operator]
        with pytest.raises(SandboxError):
            ws.materialize({"../escape.txt": "x"})
    finally:
        ws.destroy()


# ---------------------------------------------------------------------------
# ForwarderClient：协议与失败形态（平台无关，用假 socket）
# ---------------------------------------------------------------------------


class _FakeSocket:
    """够用的假 socket：记录超时，按脚本返回数据。"""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.timeouts: list[float] = []
        self.sent = bytearray()
        self.closed = False
        self._chunks = list(chunks or [])
        self.connect_error: OSError | None = None

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def connect(self, _path: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


def test_client_uses_per_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _posix(monkeypatch)  # 绕开 Windows 守卫；本条不碰 pathlib
    """超时**按本次请求**算，不是构造时那次。

    这是一条曾经的真缺陷：客户端被复用，而「单测验证器 60s」与「回归门 900s」
    差一个数量级；把第一个请求的超时固化下来，长任务会在半途假超时，
    表现为「回归门没跑通/放行」——而真相是客户端自己先断了。
    """
    fake = _FakeSocket(chunks=[b""])
    monkeypatch.setattr("socket.socket", lambda *a, **k: fake)
    client = ForwarderClient("/tmp/whatever.sock", timeout=60.0)

    with pytest.raises(SandboxError):
        client.submit(ExecRequest(command="true", timeout_seconds=900))

    assert fake.timeouts == [pytest.approx(900 + ForwarderClient.TIMEOUT_SLACK_SECONDS)]
    assert fake.closed is True


def test_client_raises_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 上不给「连个不存在的 socket」这种含糊报错，直接说清该用 inline。"""
    monkeypatch.setattr(os, "name", "nt")
    client = ForwarderClient("/var/run/fissue-runner.sock")
    with pytest.raises(SandboxError, match="inline"):
        client.submit(ExecRequest(command="true"))


def test_client_connect_failure_names_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    _posix(monkeypatch)  # 绕开 Windows 守卫；本条不碰 pathlib
    """连不上时要把**路径**与「请先起转发组件」一起说出来，否则用户只看到 Errno。"""
    fake = _FakeSocket()
    fake.connect_error = ConnectionRefusedError(111, "Connection refused")
    monkeypatch.setattr("socket.socket", lambda *a, **k: fake)
    with pytest.raises(SandboxError) as exc:
        ForwarderClient("/var/run/fissue-runner.sock").submit(ExecRequest(command="true"))
    assert "/var/run/fissue-runner.sock" in str(exc.value)
    assert "sandbox serve" in str(exc.value)
    assert fake.closed is True, "连不上也要关掉 socket，别泄漏 fd"


def test_client_reports_closed_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _posix(monkeypatch)  # 绕开 Windows 守卫；本条不碰 pathlib
    """对端不发结果就关闭 → 明确报「连接被关闭」，而不是返回一个空结果当成功。"""
    fake = _FakeSocket(chunks=[b""])
    monkeypatch.setattr("socket.socket", lambda *a, **k: fake)
    with pytest.raises(SandboxError, match="未返回结果"):
        ForwarderClient("/tmp/x.sock").submit(ExecRequest(command="true"))


def test_client_roundtrips_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _posix(monkeypatch)  # 绕开 Windows 守卫；本条不碰 pathlib
    """正常一帧往返：客户端发出的请求能被服务端解析，回来的结果能还原。"""
    from fissue.sandbox.protocol import encode

    payload = encode(
        {"ok": True, "exit_code": 0, "stdout": "hello", "stderr": "", "duration_seconds": 0.1}
    )
    fake = _FakeSocket(chunks=[payload])
    monkeypatch.setattr("socket.socket", lambda *a, **k: fake)
    result = ForwarderClient("/tmp/x.sock").submit(
        ExecRequest(command="echo hello", timeout_seconds=30)
    )
    assert result.ok is True
    assert result.stdout == "hello"
    assert fake.sent, "请求必须真的发出去"


# ---------------------------------------------------------------------------
# SandboxManager：后端选择与两条降级路径
# ---------------------------------------------------------------------------


def test_manager_decide_inline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """选 inline 还是转发组件，取决于显式强制、开关、平台与 socket 是否存在。"""
    sock = tmp_path / "runner.sock"
    cfg = _cfg(socket=str(sock))

    assert SandboxManager(cfg, force_inline=True).inline is True
    assert SandboxManager(cfg, force_inline=False).inline is False

    # 开关关掉 → inline
    assert SandboxManager(_cfg(enabled=False, socket=str(sock))).inline is True

    # socket 不存在 → 没人监听，inline（否则每次执行都连一个不存在的路径）
    assert SandboxManager(_cfg(socket=str(sock))).inline is True

    if os.name != "nt":
        sock.touch()
        assert SandboxManager(_cfg(socket=str(sock))).inline is False


def test_manager_inline_on_windows_even_if_socket_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 不支持 Unix socket：即使 socket 文件存在也必须走 inline。"""
    monkeypatch.setattr(os, "name", "nt")
    sock = tmp_path / "runner.sock"
    sock.touch()
    assert SandboxManager(_cfg(socket=str(sock))).inline is True


def test_manager_image_ready_skips_when_no_container(tmp_path: Path) -> None:
    """不需要镜像的三种情形都要放行，否则 `fissue verify` 会被前置闸门挡住。"""
    local = SandboxConfig(enabled=True, runtime="local")
    assert SandboxManager(local).image_ready("any:tag")[0] is True

    disabled = SandboxConfig(enabled=False)
    assert SandboxManager(disabled).image_ready("any:tag")[0] is True

    # 非 inline（转发组件侧提供镜像）：宿主无从判断，也放行
    forwarded = SandboxManager(
        _cfg(socket=str(tmp_path / "nope.sock")), force_inline=False
    )
    assert forwarded.image_ready("any:tag")[0] is True


def test_manager_run_short_circuits_when_disabled() -> None:
    """``sandbox.enabled=false`` 时两次入口都返回「未启用」，而不是去 exec 一个空命令。"""
    manager = SandboxManager(SandboxConfig(enabled=False))
    for result in (manager.run_sync(ExecRequest(command="true")),):
        assert result.ok is False
        assert "未启用" in (result.error or "")


@pytest.mark.asyncio
async def test_manager_run_async_short_circuits_when_disabled() -> None:
    manager = SandboxManager(SandboxConfig(enabled=False))
    result = await manager.run(ExecRequest(command="true"))
    assert result.ok is False
    assert "未启用" in (result.error or "")


def test_manager_forwards_through_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 inline 时**必须**走客户端，而不是在本机直接跑——那是隔离承诺的落点。"""
    seen: list[ExecRequest] = []

    def fake_submit(self: ForwarderClient, req: ExecRequest) -> ExecResult:
        seen.append(req)
        return ExecResult(ok=True, exit_code=0, stdout="from-forwarder")

    monkeypatch.setattr(ForwarderClient, "submit", fake_submit)
    manager = SandboxManager(_cfg(socket="/tmp/x.sock"), force_inline=False)
    result = manager.run_sync(ExecRequest(command="true"))
    assert result.stdout == "from-forwarder"
    assert len(seen) == 1
    # 客户端复用：第二次不该重新构造（构造会丢「按请求算超时」的意义）
    manager.run_sync(ExecRequest(command="true"))
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# 真起服务端（POSIX）：权限、协议、坏请求与清理
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_server_creates_private_socket_and_cleans_up(tmp_path: Path) -> None:
    """监听 socket 必须只有本用户可连（0600），停止后文件要消失。"""
    sock_path = tmp_path / "runner.sock"
    server = ForwarderServer(_cfg(socket=str(sock_path)), socket_path=str(sock_path))
    server.start()
    try:
        assert sock_path.exists(), "应当真的 bind 出 socket 文件"
        mode = stat.S_IMODE(sock_path.stat().st_mode)
        assert mode == 0o600, f"socket 权限应为 0600，实际 {oct(mode)}"
    finally:
        server.stop()
    assert not sock_path.exists(), "停止后必须清理 socket 文件，否则下次 bind 会 EADDRINUSE"


@POSIX_ONLY
def test_server_executes_and_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：客户端发一帧 → 服务端执行 → 结果回传。"""
    sock_path = tmp_path / "runner.sock"
    server = ForwarderServer(_cfg(socket=str(sock_path)), socket_path=str(sock_path))

    def fake_execute(req: ExecRequest, *, workspace: str | None = None) -> ExecResult:
        return ExecResult(ok=True, exit_code=0, stdout=f"ran:{req.command}")

    monkeypatch.setattr(server.executor, "execute", fake_execute)
    server.start()
    try:
        result = ForwarderClient(str(sock_path)).submit(ExecRequest(command="pytest -q"))
        assert result.ok is True
        assert result.stdout == "ran:pytest -q"
    finally:
        server.stop()


@POSIX_ONLY
def test_server_reports_unparsable_request(tmp_path: Path) -> None:
    """坏请求要**回一帧错误**，而不是让客户端等到超时。

    直连原始 socket 发一帧结构不对的数据（mounts 里塞非法字段），
    服务端应把它翻成 ``ok=False`` 的结果帧。
    """
    sock_path = tmp_path / "runner.sock"
    server = ForwarderServer(_cfg(socket=str(sock_path)), socket_path=str(sock_path))
    server.start()
    try:
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(5)
        raw.connect(str(sock_path))
        try:
            send_frame(raw, {"command": "true", "mounts": [{"bogus_field": 1}]})
            from fissue.sandbox.protocol import recv_frame

            payload = recv_frame(raw)
        finally:
            raw.close()
        assert payload is not None, "必须回一帧，不能沉默"
        assert payload["ok"] is False
        assert "请求解析失败" in (payload.get("error") or "")
    finally:
        server.stop()


@POSIX_ONLY
def test_server_survives_bad_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个连接直接断开，不该带走整个服务（后续请求照常可用）。"""
    sock_path = tmp_path / "runner.sock"
    server = ForwarderServer(_cfg(socket=str(sock_path)), socket_path=str(sock_path))

    def ok_execute(req: ExecRequest, *, workspace: str | None = None) -> ExecResult:
        return ExecResult(ok=True, stdout="ok")

    monkeypatch.setattr(server.executor, "execute", ok_execute)
    server.start()
    try:
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.connect(str(sock_path))
        dead.close()          # 什么都没发就断
        time.sleep(0.1)

        result = ForwarderClient(str(sock_path)).submit(ExecRequest(command="true"))
        assert result.ok is True
    finally:
        server.stop()


@POSIX_ONLY
def test_server_handles_concurrent_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个连接一份线程：短请求不该被排在长任务后面等到假超时。"""
    sock_path = tmp_path / "runner.sock"
    server = ForwarderServer(_cfg(socket=str(sock_path)), socket_path=str(sock_path))

    def slow_first(req: ExecRequest, *, workspace: str | None = None) -> ExecResult:
        if "slow" in req.command:
            time.sleep(0.8)
            return ExecResult(ok=True, stdout="slow")
        return ExecResult(ok=True, stdout="fast")

    monkeypatch.setattr(server.executor, "execute", slow_first)
    server.start()
    try:
        results: dict[str, ExecResult] = {}

        def run(cmd: str) -> None:
            results[cmd] = ForwarderClient(str(sock_path)).submit(ExecRequest(command=cmd))

        slow = threading.Thread(target=run, args=("slow",))
        slow.start()
        time.sleep(0.15)      # 让慢请求先进去
        fast = threading.Thread(target=run, args=("fast",))
        fast.start()
        fast.join(timeout=5)
        slow.join(timeout=5)

        assert results["fast"].stdout == "fast", "短请求应在慢任务结束前就返回"
        assert results["slow"].stdout == "slow"
    finally:
        server.stop()
