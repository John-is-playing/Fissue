"""沙盒执行的线协议。

宿主与沙盒之间通过**转发组件**通信：宿主机上的 ``ForwarderServer`` 监听一个
本地 socket（Unix domain socket / Windows 命名管道），``ForwarderClient`` 把
「执行请求」发过去，由转发组件在容器里执行，再把结果回传。

这样做的目的（Q8）：
* 沙盒代码永远拿不到宿主的文件系统 / Docker socket，只拿到一条受控的请求通道；
* 所有资源限制、挂载策略、清理动作都集中在一处（转发组件），不会散落各模块；
* 将来要把沙盒挪到别的机器上，只需把这条通道换成 TCP，调用方零改动。

编码：``4 字节大端长度 + UTF-8 JSON``。逐条请求-响应，不做多路复用（简单可靠）。
"""

from __future__ import annotations

import json
import struct
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_FRAME_BYTES = 64 * 1024 * 1024      # 单帧上限 64MB（diff/日志可能很大）
_HEADER = struct.Struct(">I")

PROTOCOL_VERSION = 1


@dataclass
class MountSpec:
    """挂载到容器的卷。

    ``source`` 为宿主上一个**临时目录**（由转发组件创建并负责销毁），
    ``target`` 为容器内绝对路径。绝不挂载宿主的普通目录（Q8：只挂载隔离虚拟盘）。
    """

    source: str
    target: str
    read_only: bool = False


@dataclass
class ExecRequest:
    """一次沙盒执行请求。"""

    # 工作负载
    image: str = ""
    command: str = ""                            # 以 shell 形式执行
    workdir: str = "/workspace"
    # 写入工作区的初始文件：容器内相对路径 -> 内容
    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    # 限制
    cpus: float = 2.0
    memory_mb: int = 2048
    pids: int = 256
    timeout_seconds: int = 600
    # 隔离
    network: str = "none"                        # none | bridge
    read_only_root: bool = True
    user: str = "1000:1000"
    # 额外挂载（如克隆好的仓库）
    mounts: list[MountSpec] = field(default_factory=list)
    # 元信息（透传，便于日志与产物归档）
    label: str = ""
    keep_container: bool = False
    version: int = PROTOCOL_VERSION


@dataclass
class ExecResult:
    """一次沙盒执行结果。"""

    ok: bool = False                             # 转发链路是否成功（≠ 命令成功）
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    error: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)   # 容器内路径 -> 内容快照
    version: int = PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# 编解码
# ---------------------------------------------------------------------------


def encode(obj: dict[str, Any]) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError(f"帧过大：{len(payload)} 字节（上限 {MAX_FRAME_BYTES}）")
    return _HEADER.pack(len(payload)) + payload


def decode_from(buffer: bytearray) -> dict[str, Any] | None:
    """从缓冲区里尝试解出一帧；不足一帧返回 ``None``（并从缓冲区移除已消费字节）。"""
    if len(buffer) < _HEADER.size:
        return None
    (length,) = _HEADER.unpack_from(buffer, 0)
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"帧长度非法：{length}")
    if len(buffer) < _HEADER.size + length:
        return None
    start = _HEADER.size
    payload = bytes(buffer[start : start + length])
    del buffer[: start + length]
    return json.loads(payload.decode("utf-8"))


def request_to_dict(req: ExecRequest) -> dict[str, Any]:
    data = asdict(req)
    data["mounts"] = [asdict(m) for m in req.mounts]
    return data


def request_from_dict(data: dict[str, Any]) -> ExecRequest:
    mounts = [MountSpec(**m) for m in data.get("mounts") or []]
    known = {f for f in ExecRequest.__dataclass_fields__}
    kwargs = {k: v for k, v in data.items() if k in known and k != "mounts"}
    return ExecRequest(mounts=mounts, **kwargs)


def result_to_dict(res: ExecResult) -> dict[str, Any]:
    return asdict(res)


def result_from_dict(data: dict[str, Any]) -> ExecResult:
    known = {f for f in ExecResult.__dataclass_fields__}
    return ExecResult(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# 同步 socket 收发（转发组件与客户端共用）
# ---------------------------------------------------------------------------


def send_frame(sock: Any, obj: dict[str, Any]) -> None:
    sock.sendall(encode(obj))


def recv_frame(sock: Any) -> dict[str, Any] | None:
    """阻塞读取一帧；对端关闭返回 ``None``。"""
    buffer = bytearray()
    header: bytes | None = None
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buffer.extend(chunk)
        if header is None:
            if len(buffer) < _HEADER.size:
                continue
            (length,) = _HEADER.unpack_from(buffer, 0)
            if length > MAX_FRAME_BYTES:
                raise ValueError(f"帧长度非法：{length}")
            header = bytes(buffer[: _HEADER.size + length])
            del buffer[: _HEADER.size + length]
        # 继续补齐
        if len(header) < _HEADER.size + _HEADER.unpack_from(header, 0)[0]:
            need = _HEADER.size + _HEADER.unpack_from(header, 0)[0]
            while len(header) < need:
                more = sock.recv(65536)
                if not more:
                    return None
                header += more
        return json.loads(header[_HEADER.size :].decode("utf-8"))
