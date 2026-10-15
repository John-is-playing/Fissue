"""Docker 运行时：唯一与 Docker 打交道的地方。

安全基线（Q8）
--------------
* ``network=none``：默认断网，杜绝不可信代码外联/回连。
* ``read_only=True`` + ``tmpfs``：根文件系统只读，只给 /tmp 一个受限可写层。
* 非 root 用户（默认 1000:1000），配合 ``no-new-privileges``。
* ``cap_drop=ALL``：丢弃全部 Linux capabilities。
* 资源限额：CPU、内存、PID 数、执行时长。
* 只挂载调用方传入的**隔离工作区**，绝不挂载宿主普通目录。

当 Docker 不可用时（未安装 / 守护进程未运行），**默认拒绝执行**：降级到本地子进程会绕过
上面列出的全部隔离，而被执行的却是模型生成的验证器与不可信 PR 代码——静默降级等于把隔离
承诺整套架空。确需本机执行请用 ``sandbox.runtime: local``，或显式开启
``sandbox.allow_local_fallback``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..config import DEFAULT_LOCAL_ENV_ALLOWLIST, SandboxConfig
from ..errors import SandboxError
from ..logging_setup import get_logger
from .protocol import ExecResult, MountSpec

log = get_logger(__name__)

#: 单次执行**每个流**保留的输出上限（字符）。超出部分照读不存，见 :func:`_read_capped`。
#: 捕获无上限时，验证器里一个打印循环（或 ``yes``）就能吃光宿主内存与 ``data_dir``；
#: 走转发组件时更糟——超过 64MB 的帧会让它直接抛异常，整条验证 stage 崩掉，
#: 而不是给出一个「输出过大」的结论。
MAX_CAPTURE_BYTES = 2 * 1024 * 1024


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
        server_version = proc.stdout.strip()
        if not server_version:
            # Windows 上 docker 客户端在引擎不可达时会 **exit 0 但无输出**
            # （Linux 下同样情况通常是非零退出码）。只看 returncode 会把它判成
            # 「可用」，于是 run() 走 _run_docker → 镜像不可用，而已经写好、
            # 且文档承诺的本地降级分支永远不触发。
            return False, "Docker 守护进程不可达（未取到 ServerVersion）"
        return True, f"Docker {server_version}"

    def _image_present(self, image: str) -> bool:
        """本地是否已有该镜像（**不触发拉取**，毫秒级）。"""
        if self._docker_bin is None:
            return False
        return (
            subprocess.run(
                [self._docker_bin, "image", "inspect", image],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).returncode
            == 0
        )

    def image_ready(self, image: str) -> tuple[bool, str]:
        """基础镜像是否就绪——**只做快速存在性检查，绝不触发拉取**。

        供 ``fissue verify`` / 常驻服务在跑整批之前做一次前置判定：
        一条条目要先克隆工作区、再花几十秒到数分钟让 LLM 生成验证器，
        最后才在沙盒里发现镜像不在——把这个必然失败的结果提前到几毫秒内、
        且只报一次，而不是每条各报一次。

        为什么不能在这里调 :meth:`ensure_image`：它会 ``docker pull``，
        而本机实测拉取失败前要空等约 20 秒超时。若把它放进逐条的前置检查，
        就退化成比原问题更糟的形态（每条多等 20 秒）。
        """
        if self._docker_bin is None:
            return False, "未找到 docker 命令"
        if self._image_present(image):
            return True, f"镜像就绪：{image}"
        return False, f"基础镜像不存在：{image}"

    def ensure_image(self, image: str) -> bool:
        """确保镜像存在（不存在则拉取）。"""
        if self._docker_bin is None:
            return False
        if self._image_present(image):
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
        """执行一次命令。

        两条路径：``runtime: docker`` 走容器；``runtime: local`` 明确要求本机执行。
        Docker 不可用时**默认拒绝执行**（见 :attr:`SandboxConfig.allow_local_fallback`）：
        降级到本地会绕过全部容器隔离，而被执行的却是模型生成的验证器与不可信
        PR 代码，静默降级等于把 SECURITY.md 的隔离承诺整套架空。
        """
        start = time.monotonic()
        mount_list = list(mounts or [])

        # 未知取值必须 fail-closed。这里原先只有 `if self.cfg.runtime == "docker"` 一条判定，
        # 于是任何别的值都会落到函数末尾的 `_run_local`：拼错一个字母（`Docker`、`podman`）
        # 就等于静默放弃全部容器隔离。配置层已用 `Literal` 拦一道，这一道防的是绕过模型
        # 校验的构造路径（`model_construct`、测试夹具、运行期改字段）。
        if self.cfg.runtime not in {"docker", "local"}:
            return ExecResult(
                ok=False,
                error=(
                    f"未知的 sandbox.runtime={self.cfg.runtime!r}（只支持 docker | local），"
                    "已拒绝执行：非 docker 的取值会落到**无隔离**的本地执行，"
                    "而被执行的是模型生成的验证器与不可信 PR 代码。"
                ),
                duration_seconds=round(time.monotonic() - start, 3),
            )
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
            if not self.cfg.allow_local_fallback:
                return ExecResult(
                    ok=False,
                    error=(
                        f"Docker 不可用（{reason}），已拒绝执行。"
                        "本地降级没有任何容器隔离（不禁网、非只读根、无资源上限），"
                        "而验证器由模型生成、代码来自不可信 PR，故不默认降级。"
                        "确需在本机执行请显式设置 sandbox.allow_local_fallback: true；"
                        "只想用本机做开发可设 sandbox.runtime: local。"
                    ),
                    duration_seconds=round(time.monotonic() - start, 3),
                )
            log.warning(
                "Docker 不可用（%s），按 sandbox.allow_local_fallback 降级为本地执行——**无容器隔离**",
                reason,
            )
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

        # 容器名：超时后要能把它**真正**杀掉（见 :meth:`_reclaim_container`）。uuid 保证
        # 不会与上一轮遗留的容器撞名。
        container = f"fissue-{uuid.uuid4().hex[:12]}"
        args += ["--name", container]

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

        return self._spawn(
            args,
            timeout_seconds=timeout_seconds,
            start=start,
            mode="docker",
            container=container,
        )

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

        注意：**没有隔离**。仅用于 CI 沙盒不可用的降级路径与本地开发
        （降级路径需显式开启 ``sandbox.allow_local_fallback``）。

        命令一律按 POSIX shell 语义执行（与容器内一致），因为在 Windows 上装了
        Git Bash 也很常见，而 cmd.exe 会把引号/重定向处理得面目全非，导致
        「docker 里能跑、本机跑不了」的诡异差异。

        环境变量**只放行白名单**：没有容器挡着，被执行的验证器/PR 代码可以任意
        组包外发，把宿主整份 ``os.environ``（含 LLM key 与各平台 token）交给它
        等于直接交出密钥。
        """
        cwd = mounts[0].source if mounts else tempfile.gettempdir()
        if not Path(cwd).exists():
            return ExecResult(ok=False, error=f"工作目录不存在：{cwd}")

        shell = self._local_shell(command)
        environ = self._local_env(env)
        return self._spawn(
            shell, timeout_seconds=timeout_seconds, start=start, mode="local", cwd=cwd, env=environ
        )

    def _local_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        """本地执行的子进程环境：白名单放行，调用方显式传入的 ``extra`` 优先。"""
        allow = self.cfg.local_env_allowlist or list(DEFAULT_LOCAL_ENV_ALLOWLIST)
        allowed = set(allow)
        environ = {k: v for k, v in os.environ.items() if k in allowed}
        environ.update(extra or {})
        return environ

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
        container: str | None = None,
    ) -> ExecResult:
        """跑子进程：输出**有硬上限**，超时则**真正**回收容器。

        这两件事都必须自己做，不能用 ``subprocess.run`` 一把梭：

        * ``capture_output=True`` 是无上限的——验证器里一个打印循环就能吃光宿主内存与
          ``data_dir``，转发组件那条路上还会因为超过 64MB 的帧直接抛异常；
        * ``subprocess.run`` 超时只 kill **宿主上的 docker CLI**，``--rm`` 不会触发，
          容器仍在后台按限额吃资源（``fissue.managed=true`` 那个标签全仓没人读）。
        """
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                env=env,
            )
        except OSError as exc:
            return ExecResult(
                ok=False,
                error=f"启动失败：{exc}",
                duration_seconds=round(time.monotonic() - start, 3),
            )

        timed_out = False
        with ThreadPoolExecutor(max_workers=2) as pool:
            out_f = pool.submit(_read_capped, proc.stdout, MAX_CAPTURE_BYTES)
            err_f = pool.submit(_read_capped, proc.stderr, MAX_CAPTURE_BYTES)
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()          # 先结束 CLI：管道才会 EOF，两个读线程才会收工
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:      # pragma: no cover - 杀不掉只能放弃
                    log.warning("子进程 kill 之后仍未退出：%s", args[:2])
            stdout, out_cut = out_f.result()
            stderr, err_cut = err_f.result()

        truncated = out_cut or err_cut
        if truncated:
            stderr = f"{stderr}\n[输出已被截断：单流上限 {MAX_CAPTURE_BYTES} 字符]"
            log.warning(
                "沙盒输出超过上限已截断（mode=%s，%s）", mode, container or " ".join(args[:2])
            )
        duration = round(time.monotonic() - start, 3)

        if timed_out:
            self._reclaim_container(container)
            return ExecResult(
                ok=False,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                error=f"执行超时（{timeout_seconds}s）"
                + ("，容器已强制回收" if container else ""),
                duration_seconds=duration,
            )

        return ExecResult(
            ok=True,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            artifacts={"mode": mode, **({"output_truncated": "1"} if truncated else {})},
        )

    def _reclaim_container(self, name: str | None) -> None:
        """超时后把容器**真正**杀掉。

        ``subprocess`` 超时只 kill 宿主上的 ``docker`` CLI；容器自己还在后台跑，``--rm``
        也不会触发——它按 ``--cpus/--memory/--pids-limit`` 一直吃资源，重跑还会累积，最后
        只能人工 ``docker rm -f``。这里显式回收，并把容器名打进日志便于人工核对。
        """
        if not name or self._docker_bin is None:
            return
        try:
            subprocess.run(
                [self._docker_bin, "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=60,
            )
            log.warning("沙盒超时：已强制回收容器 %s", name)
        except Exception as exc:      # pragma: no cover - 回收失败不该盖住原始的超时结论
            log.warning("回收容器 %s 失败（可人工 docker rm -f %s）：%s", name, name, exc)


def _read_capped(pipe: Any, limit: int) -> tuple[str, bool]:
    """读到 EOF，但只**保留**前 ``limit`` 个字符；返回 ``(文本, 是否被截断)``。

    为什么不干脆停止读取（关掉管道）：那会让写端拿到 EPIPE/SIGPIPE 而提前退出，等于悄悄
    改掉被执行命令的语义（本该跑完的套件变成「被杀」）。只丢弃、不停止读，内存有上限而
    进程行为不变。
    """
    kept: list[str] = []
    kept_len = 0
    truncated = False
    while True:
        chunk = pipe.read(65536)
        if not chunk:
            break
        room = limit - kept_len
        if room <= 0:
            truncated = True
            continue
        if len(chunk) > room:
            kept.append(chunk[:room])
            kept_len = limit
            truncated = True
        else:
            kept.append(chunk)
            kept_len += len(chunk)
    return "".join(kept), truncated


def _decode(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)
