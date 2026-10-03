"""转发组件（Forwarder）。

架构
----
::

    调用方（流水线 / 验证器）
        │  ExecRequest
        ▼
    ForwarderClient ── 本地 socket ──► ForwarderServer
                                            │
                                            ▼
                                      DockerRuntime          ← 唯一接触 Docker 的地方
                                            │
                                            ▼
                                    隔离容器（禁网/非 root/只读根/限额）

* 调用方**永远不直接持有 Docker 句柄**，也不接触宿主路径。
* 工作区是转发组件为每次执行单独创建的临时目录（可选 loopback 镜像），执行完即销毁。
* 支持两种模式：
  - ``server``：常驻进程监听 socket，宿主上跑一次即可（生产）。
  - ``inline``：进程内直接调用 DockerRuntime（本机开发 / 测试，免去起服务）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..config import SandboxConfig
from ..errors import SandboxError
from ..logging_setup import get_logger
from .protocol import (
    ExecRequest,
    ExecResult,
    MountSpec,
    decode_from,
    encode,
    recv_frame,
    request_from_dict,
    request_to_dict,
    result_from_dict,
    result_to_dict,
    send_frame,
)
from .runtime import DockerRuntime

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# 工作区管理（隔离虚拟盘）
# ---------------------------------------------------------------------------


class Workspace:
    """一次执行的工作区。

    * ``type=volume``：普通临时目录（默认）。
    * ``type=loopback``：创建固定大小的镜像文件并挂载为 loop 设备，实现「隔离虚拟硬盘」；
      需要 Linux + root 权限，失败时回退到临时目录并告警。
    """

    def __init__(self, cfg: SandboxConfig, label: str = "") -> None:
        self.cfg = cfg
        self.label = label
        self.root: Path | None = None
        self._loop_device: str | None = None
        self._image_file: Path | None = None

    def __enter__(self) -> "Workspace":
        self.create()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.destroy()

    def create(self) -> Path:
        self.root = Path(tempfile.mkdtemp(prefix=f"fissue-{self.label or 'ws'}-"))
        if self.cfg.mounts.type == "loopback":
            self._try_loopback()
        return self.root

    def _try_loopback(self) -> None:
        """尝试创建 loopback 虚拟盘；不支持时静默回退。"""
        if os.name != "posix":
            log.debug("非 POSIX 系统，loopback 虚拟盘不可用，回退临时目录")
            return
        size_mb = max(64, self.cfg.mounts.size_mb)
        image = self.root.parent / f"{self.root.name}.img" if self.root else None
        if image is None:
            return
        try:
            import subprocess

            with open(image, "wb") as fh:
                fh.truncate(size_mb * 1024 * 1024)
            subprocess.run(["mkfs.ext4", "-q", "-F", str(image)], check=True, capture_output=True)
            device = subprocess.run(
                ["losetup", "--find", "--show", str(image)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.strip()
            if not device:
                raise SandboxError("losetup 未返回设备名")
            subprocess.run(["mount", device, str(self.root)], check=True, capture_output=True)
            self._loop_device = device
            self._image_file = image
            log.info("已挂载 loopback 隔离虚拟盘 %s -> %s（%dMB）", device, self.root, size_mb)
        except Exception as exc:
            log.warning("loopback 虚拟盘创建失败（回退普通临时目录）：%s", exc)

    def destroy(self) -> None:
        if self._loop_device:
            try:
                import subprocess

                subprocess.run(["umount", self._loop_device], check=False, capture_output=True)
                subprocess.run(["losetup", "-d", self._loop_device], check=False, capture_output=True)
            except Exception as exc:  # pragma: no cover
                log.warning("卸载 loopback 失败：%s", exc)
        if self.root and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        if self._image_file and self._image_file.exists():
            self._image_file.unlink(missing_ok=True)

    def materialize(self, files: dict[str, str]) -> Path:
        """把请求里的初始文件写进工作区。"""
        if self.root is None:
            raise SandboxError("工作区尚未创建")
        for rel, content in files.items():
            target = (self.root / rel).resolve()
            if not str(target).startswith(str(self.root.resolve())):
                raise SandboxError(f"拒绝写入工作区之外的路径：{rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
        return self.root


# ---------------------------------------------------------------------------
# 执行核心（转发服务的实现体）
# ---------------------------------------------------------------------------


class Executor:
    """把 :class:`ExecRequest` 落实为一次容器执行。

    这是「唯一」持有 :class:`DockerRuntime` 的类。
    """

    def __init__(self, cfg: SandboxConfig) -> None:
        self.cfg = cfg
        self.runtime = DockerRuntime(cfg)

    def execute(self, req: ExecRequest, *, workspace: str | None = None) -> ExecResult:
        """同步执行（转发服务在线程中调用）。

        工作区来源有三条优先级：
        1. 调用方显式传入 ``workspace``；
        2. 请求里已有挂到 ``workdir`` 的挂载（例如克隆好的仓库）；
        3. 都没有 → 新建一个临时隔离工作区，执行完销毁。
        """
        start = time.monotonic()
        mounts = list(req.mounts)
        bound = next((m for m in mounts if m.target.rstrip("/") == req.workdir.rstrip("/")), None)

        work: Workspace | None = None
        if workspace is not None:
            ws = Path(workspace)
        elif bound is not None:
            ws = Path(bound.source)          # 复用调用方提供的隔离目录（如仓库工作副本）
        else:
            work = Workspace(self.cfg, label=req.label)
            ws = work.create()

        try:
            if not ws.exists():
                return ExecResult(ok=False, error=f"工作区不存在：{ws}")

            if req.files:
                for rel, content in req.files.items():
                    target = (ws / rel).resolve()
                    if not str(target).startswith(str(ws.resolve())):
                        return ExecResult(ok=False, error=f"非法路径：{rel}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # newline="" 保持字节原样，避免 Windows 上把 \n 变成 \r\n
                    target.write_text(content, encoding="utf-8", newline="")

            # 没有现成挂载时才注入临时工作区；绝不挂载宿主普通目录
            if bound is None:
                mounts.insert(0, MountSpec(source=str(ws), target=req.workdir, read_only=False))

            image = req.image or self.cfg.image
            result = self.runtime.run(
                image=image,
                command=req.command,
                workdir=req.workdir,
                mounts=mounts,
                env=req.env,
                network=req.network or self.cfg.network,
                cpus=req.cpus or self.cfg.limits.cpus,
                memory_mb=req.memory_mb or self.cfg.limits.memory_mb,
                pids=req.pids or self.cfg.limits.pids,
                timeout_seconds=req.timeout_seconds or self.cfg.limits.timeout_seconds,
                read_only_root=req.read_only_root,
                user=req.user or self.cfg.user,
                label=req.label,
            )
            result.duration_seconds = round(time.monotonic() - start, 3)
            return result
        except Exception as exc:
            log.exception("沙盒执行异常")
            return ExecResult(
                ok=False,
                error=str(exc),
                duration_seconds=round(time.monotonic() - start, 3),
            )
        finally:
            if work is not None:
                work.destroy()

    async def execute_async(self, req: ExecRequest, *, workspace: str | None = None) -> ExecResult:
        """异步包装（避免阻塞事件循环）。"""
        return await asyncio.to_thread(self.execute, req, workspace=workspace)


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------


class ForwarderServer:
    """监听本地 socket，接收执行请求并回传结果。

    只支持**单客户端串行**（每个连接一次请求-响应），实现简单、边界清晰。
    """

    def __init__(self, cfg: SandboxConfig, socket_path: str | None = None) -> None:
        self.cfg = cfg
        self.socket_path = socket_path or cfg.forwarder.socket
        self.executor = Executor(cfg)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- 生命周期 ---------------------------------------------------------

    def start(self, *, blocking: bool = False) -> None:
        self._sock = self._listen()
        log.info("转发组件已启动，监听 %s", self.socket_path)
        if blocking:
            self._serve_forever()
        else:
            self._thread = threading.Thread(target=self._serve_forever, name="fissue-forwarder", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._cleanup_socket()

    def _listen(self) -> socket.socket:
        path = self.socket_path
        if os.name == "nt" and not path.startswith("\\\\.\\pipe\\"):
            # Windows 命名管道
            return self._listen_windows_pipe(path)
        self._cleanup_socket()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        os.chmod(path, 0o600)          # 仅本用户可连
        sock.listen(4)
        return sock

    def _listen_windows_pipe(self, name: str) -> socket.socket:  # pragma: no cover - 平台相关
        raise SandboxError(
            "Windows 上请使用 inline 模式，或把 sandbox.forwarder.socket 指向 \\\\.\\pipe\\<name>"
        )

    def _cleanup_socket(self) -> None:
        path = self.socket_path
        if os.name != "nt" and path and Path(path).exists():
            try:
                Path(path).unlink()
            except OSError:
                pass

    def _serve_forever(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception:  # pragma: no cover
                log.exception("处理请求失败")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        payload = recv_frame(conn)
        if payload is None:
            return
        try:
            req = request_from_dict(payload)
        except Exception as exc:
            send_frame(conn, result_to_dict(ExecResult(ok=False, error=f"请求解析失败：{exc}")))
            return
        result = self.executor.execute(req)
        send_frame(conn, result_to_dict(result))


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class ForwarderClient:
    """把执行请求发给转发组件。"""

    def __init__(self, socket_path: str, *, timeout: float = 900.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def submit(self, req: ExecRequest) -> ExecResult:
        if os.name == "nt":
            raise SandboxError("Windows 不支持 Unix socket 转发，请使用 inline 模式（sandbox.runtime=local）")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            raise SandboxError(
                f"无法连接转发组件 {self.socket_path}：{exc}（请先运行 `fissue sandbox serve`）"
            ) from exc
        try:
            send_frame(sock, request_to_dict(req))
            payload = recv_frame(sock)
        finally:
            sock.close()
        if payload is None:
            raise SandboxError("转发组件未返回结果（连接被关闭）")
        return result_from_dict(payload)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


class SandboxManager:
    """沙盒统一入口：按配置自动选择 inline 或 forwarder 两种后端。

    调用方只需：``await manager.run(req)``。
    """

    def __init__(self, cfg: SandboxConfig, *, force_inline: bool | None = None) -> None:
        self.cfg = cfg
        self.inline = self._decide_inline(force_inline)
        self._executor: Executor | None = None
        self._client: ForwarderClient | None = None

    def _decide_inline(self, force: bool | None) -> bool:
        if force is not None:
            return force
        # 本地开发模式 / 转发组件关闭 / 非 POSIX 平台 → 直接内联调用
        if not self.cfg.forwarder.enabled:
            return True
        if os.name == "nt":
            return True
        return not Path(self.cfg.forwarder.socket).exists()

    @property
    def available(self) -> tuple[bool, str]:
        """沙盒是否可用（Docker 在不在）。"""
        if self.cfg.runtime == "local":
            return True, "local 模式：无容器隔离，仅限本机开发"
        runtime = DockerRuntime(self.cfg)
        return runtime.available()

    async def run(self, req: ExecRequest) -> ExecResult:
        if not self.cfg.enabled:
            return ExecResult(ok=False, error="沙盒未启用（sandbox.enabled=false）")

        if self.inline:
            if self._executor is None:
                self._executor = Executor(self.cfg)
            return await self._executor.execute_async(req)

        if self._client is None:
            self._client = ForwarderClient(
                self.cfg.forwarder.socket, timeout=req.timeout_seconds + 120
            )
        return await asyncio.to_thread(self._client.submit, req)

    def run_sync(self, req: ExecRequest) -> ExecResult:
        """同步执行（CLI / 线程内使用）。"""
        if not self.cfg.enabled:
            return ExecResult(ok=False, error="沙盒未启用（sandbox.enabled=false）")
        if self.inline:
            if self._executor is None:
                self._executor = Executor(self.cfg)
            return self._executor.execute(req)
        if self._client is None:
            self._client = ForwarderClient(self.cfg.forwarder.socket, timeout=req.timeout_seconds + 120)
        return self._client.submit(req)
