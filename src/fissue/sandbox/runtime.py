"""Docker 运行时：唯一与 Docker 打交道的地方。

安全基线（Q8）
--------------
* ``network=none``：默认断网，杜绝不可信代码外联/回连。
* ``read_only=True`` + ``tmpfs``：根文件系统只读，只给 /tmp 一个受限可写层。
* 非 root 用户（默认 1000:1000），配合 ``no-new-privileges``。
* ``cap_drop=ALL``：丢弃全部 Linux capabilities。
* 资源限额：CPU、内存、PID 数、执行时长。
* 只挂载调用方传入的**隔离工作区**，绝不挂载宿主普通目录。

当 Docker 不可用时（未安装 / 守护进程未运行），自动降级为**本地子进程**执行，
并给出显式警告——本地模式没有容器隔离，只应在可信的本地开发环境使用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import SandboxConfig
from ..errors import SandboxError
from ..logging_setup import get_logger
from .protocol import ExecResult, MountSpec

log = get_logger(__name__)


class DockerRuntime:
    """容器执行器（含本地降级）。"""

    def __init__(self, cfg: SandboxConfig) -> None:
        self.cfg = cfg
        self._docker_bin = shutil.which("docker")

    # -- 可用性 -----------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        """Docker 是否可用。"""
        if self._docker_bin is None:
            return False, "未找到 docker 命令"
        try:
            proc = subprocess.run(
                [self._docker_bin, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"docker 不可用：{exc}"
        if proc.returncode != 0:
            return False, f"Docker 守护进程未运行：{proc.stderr.strip()[:200]}"
        return True, f"Docker {proc.stdout.strip()}"

    def ensure_image(self, image: str) -> bool:
        """确保镜像存在（不存在则拉取）。"""
        if self._docker_bin is None:
            return False
        inspect = subprocess.run(
            [self._docker_bin, "image", "inspect", image],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if inspect.returncode == 0:
            return True
        log.info("镜像 %s 不存在，尝试拉取…", image)
        pull = subprocess.run(
            [self._docker_bin, "pull", image],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if pull.returncode != 0:
            log.error("拉取镜像失败：%s", pull.stderr.strip()[:300])
            return False
        return True

    # -- 执行 -------------------------------------------------------------

    def run(
        self,
        *,
        image: str,
        command: str,
        workdir: str = "/workspace",
        mounts: list[MountSpec] | None = None,
        env: dict[str, str] | None = None,
        network: str = "none",
        cpus: float = 2.0,
        memory_mb: int = 2048,
        pids: int = 256,
        timeout_seconds: int = 600,
        read_only_root: bool = True,
        user: str = "1000:1000",
        label: str = "",
    ) -> ExecResult:
        """执行一次命令。Docker 不可用则降级本地执行。"""
        start = time.monotonic()
        mount_list = list(mounts or [])

        if self.cfg.runtime == "docker":
            ok, reason = self.available()
            if ok:
                return self._run_docker(
                    image=image,
                    command=command,
                    workdir=workdir,
                    mounts=mount_list,
                    env=env or {},
                    network=network,
                    cpus=cpus,
                    memory_mb=memory_mb,
                    pids=pids,
                    timeout_seconds=timeout_seconds,
                    read_only_root=read_only_root,
                    user=user,
                    label=label,
                    start=start,
                )
            log.warning("Docker 不可用（%s），降级为本地执行——**无容器隔离**", reason)
        return self._run_local(
            command=command,
            workdir=workdir,
            mounts=mount_list,
            env=env or {},
            timeout_seconds=timeout_seconds,
            start=start,
        )

    # -- Docker 路径 ------------------------------------------------------

    def _run_docker(
        self,
        *,
        image: str,
        command: str,
        workdir: str,
        mounts: list[MountSpec],
        env: dict[str, str],
        network: str,
        cpus: float,
        memory_mb: int,
        pids: int,
        timeout_seconds: int,
        read_only_root: bool,
        user: str,
        label: str,
        start: float,
    ) -> ExecResult:
        assert self._docker_bin is not None
        if not self.ensure_image(image):
            return ExecResult(ok=False, error=f"镜像不可用：{image}")

        args: list[str] = [self._docker_bin, "run", "--rm"]

        # 标识
        if label:
            args += ["--label", f"fissue.label={label}"]
        args += ["--label", "fissue.managed=true"]

        # 隔离
        args += ["--network", network or "none"]
        if read_only_root:
            args += ["--read-only"]
        # 只读根下必须给几个可写点，否则多数运行时无法启动
        args += [
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs", "/run:rw,noexec,nosuid,size=16m",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--user", user,
        ]

        # 资源限额
        args += [
            "--cpus", str(cpus),
            "--memory", f"{memory_mb}m",
            "--memory-swap", f"{memory_mb}m",
            "--pids-limit", str(pids),
        ]

        # 挂载（只挂调用方给的隔离目录）
        for m in mounts:
            mode = "ro" if m.read_only else "rw"
            args += ["-v", f"{m.source}:{m.target}:{mode}"]

        # 环境变量
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]

        args += ["--workdir", workdir]
        args += [image, "sh", "-lc", command]

        return self._spawn(args, timeout_seconds=timeout_seconds, start=start, mode="docker")

    # -- 本地降级 ---------------------------------------------------------

    def _run_local(
        self,
        *,
        command: str,
        workdir: str,
        mounts: list[MountSpec],
        env: dict[str, str],
        timeout_seconds: int,
        start: float,
    ) -> ExecResult:
        """无容器执行：把第一个挂载当工作目录。

        注意：**没有隔离**。仅用于 CI 沙盒不可用的降级路径与本地开发。

        命令一律按 POSIX shell 语义执行（与容器内一致），因为在 Windows 上装了
        Git Bash 也很常见，而 cmd.exe 会把引号/重定向处理得面目全非，导致
        「docker 里能跑、本机跑不了」的诡异差异。
        """
        cwd = mounts[0].source if mounts else tempfile.gettempdir()
        if not Path(cwd).exists():
            return ExecResult(ok=False, error=f"工作目录不存在：{cwd}")

        shell = self._local_shell(command)
        environ = {**os.environ, **(env or {})}
        return self._spawn(
            shell, timeout_seconds=timeout_seconds, start=start, mode="local", cwd=cwd, env=environ
        )

    @staticmethod
    def _local_shell(command: str) -> list[str]:
        """选取本机可用的 shell。"""
        if os.name == "nt":
            sh = DockerRuntime._find_posix_shell()
            if sh:
                return [sh, "-lc", command]
            return ["cmd", "/c", command]
        return ["sh", "-lc", command]

    @staticmethod
    def _find_posix_shell() -> str | None:
        """在 Windows 上找一个**真正的 POSIX shell**（Git Bash 的 sh/bash）。

        不能直接信 ``which("bash")``：从 PowerShell / 资源管理器启动时，用户 PATH
        里可能只有 ``%ProgramFiles%\\Git\\cmd``（没有 ``usr\\bin``），于是
        ``sh`` 取不到、``bash`` 落到 ``C:\\Windows\\System32\\bash.exe`` —— 那是
        **WSL 的垫片**，不是 Git Bash。WSL 垫片不认 ``-lc``，会报
        ``execvpe(...): No such file or directory``，导致本地沙盒里的验证器、
        回归门、e2e 全部失败（「在 Git Bash 里能跑、在 PowerShell 里全红」）。
        """
        for name in ("sh", "bash"):
            found = shutil.which(name)
            if found and DockerRuntime._is_posix_shell(found):
                return found
        # PATH 里没有 Git 的 shell：git.exe 通常仍可达，用它的安装根推导 usr\bin。
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "sh.exe"
            if candidate.exists():
                return str(candidate)
        return None

    @staticmethod
    def _is_posix_shell(path: str) -> bool:
        """排除 WSL 的 bash/wsl 垫片，只认 Git Bash 这类真 POSIX shell。"""
        p = Path(path)
        lowered = str(p).lower()
        if "windowsapps" in lowered:                       # 应用执行别名垫片
            return False
        if p.parent.name.lower() == "system32" and p.name.lower() in ("bash.exe", "wsl.exe"):
            return False
        return True

    # -- 进程封装 ---------------------------------------------------------

    def _spawn(
        self,
        args: list[str],
        *,
        timeout_seconds: int,
        start: float,
        mode: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                cwd=cwd,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                ok=False,
                exit_code=None,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                timed_out=True,
                error=f"执行超时（{timeout_seconds}s）",
                duration_seconds=round(time.monotonic() - start, 3),
            )
        except OSError as exc:
            return ExecResult(
                ok=False,
                error=f"启动失败：{exc}",
                duration_seconds=round(time.monotonic() - start, 3),
            )

        return ExecResult(
            ok=True,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_seconds=round(time.monotonic() - start, 3),
            artifacts={"mode": mode},
        )


def _decode(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)
