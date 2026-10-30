"""真容器用例：把 ``sandbox.runtime=docker`` 那条路径真正跑一遍。

为什么单独开一个文件、还默认跳过
--------------------------------
默认测试套件承诺**完全离线**（`conftest.py` 有 autouse 网络守卫，CI 不需要 Docker）。
于是 ``runtime.py`` 里的容器分支在 CI 与日常跑测中**一次都不会被执行**——
测试对 Docker 全是打桩（``monkeypatch.setattr(DockerRuntime, "available", ...)``）。

而本项目反复吃过「只有真机才照得出来」的亏：PostgreSQL、MySQL、Gitee 写操作、
私有仓库克隆，每一次都是真机才暴露。**最该真机验证的沙盒隔离，反而恰恰没真机跑过**：
挂载构造、``--user`` 跟随宿主、``--read-only``、``--network none`` 这些参数写错了，
单测照样全绿。

所以这里用环境变量开门：

* ``FISSUE_TEST_DOCKER=1`` 才跑（否则模块整体 skip，日常 ``pytest -q`` 不受影响）；
* ``FISSUE_TEST_DOCKER_IMAGE`` 指定镜像（默认 ``alpine:3.20``，CI 里预拉）。

CI 的 ``sandbox (docker)`` 任务就是跑这个文件。
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from fissue.config import SandboxConfig
from fissue.sandbox.protocol import MountSpec
from fissue.sandbox.runtime import DockerRuntime, default_container_user

IMAGE = os.environ.get("FISSUE_TEST_DOCKER_IMAGE", "alpine:3.20")

pytestmark = pytest.mark.skipif(
    os.environ.get("FISSUE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="真容器用例：需要 FISSUE_TEST_DOCKER=1 且本机有 docker",
)


def _runtime(**overrides: object) -> DockerRuntime:
    cfg = SandboxConfig(
        enabled=True,
        runtime="docker",
        image=IMAGE,
        network="none",
        read_only_root=True,
        **overrides,  # type: ignore[arg-type]
    )
    return DockerRuntime(cfg)


@pytest.fixture(scope="module")
def runtime() -> DockerRuntime:
    rt = _runtime()
    ok, why = rt.available()
    if not ok:
        pytest.skip(f"docker 不可用：{why}")
    if not rt.ensure_image(IMAGE):
        pytest.skip(f"镜像拉不下来：{IMAGE}")
    return rt


def test_runs_a_command_in_a_container(runtime: DockerRuntime) -> None:
    """最基本的通路：命令真的进了容器、输出真的回得来。"""
    result = runtime.run(image=IMAGE, command="echo fissue-probe-ok")
    assert result.ok is True, result.error
    assert result.exit_code == 0
    assert "fissue-probe-ok" in result.stdout


def test_network_is_off_by_default(runtime: DockerRuntime) -> None:
    """默认 ``network=none``：容器里的外发必须失败。

    这条是 SECURITY.md 的落点。参数写错（比如漏了 ``--network none``）时容器照样能跑，
    单测看不出任何异常——只有真机能照出来。
    """
    result = runtime.run(
        image=IMAGE,
        command="wget -q -T 3 -O - http://example.com || exit 9",
        timeout_seconds=60,
    )
    assert result.ok is True, result.error
    assert result.exit_code != 0, f"容器竟然能联网：stdout={result.stdout[:200]!r}"


def test_read_only_root_blocks_writes(runtime: DockerRuntime) -> None:
    """``read_only_root=True``：容器根文件系统不可写，防止污染镜像层。"""
    result = runtime.run(image=IMAGE, command="sh -c 'echo x > /etc/fissue-probe'")
    assert result.ok is True, result.error
    assert result.exit_code != 0, "根文件系统竟然可写"


def test_workspace_mount_is_writable(runtime: DockerRuntime) -> None:
    """挂进来的工作区**必须可写**，且写进去的内容宿主能读到。

    这条最容易被 ``--user`` 写死坑到：容器 uid 与宿主 uid 不一致时，工作区（宿主
    ``mkdtemp``，0700）既遍历不了也写不进，整条验证链路会以 Permission denied 收场——
    而日志看上去像代码或验证器的问题。
    """
    root = Path(tempfile.mkdtemp(prefix="fissue-docker-test-"))
    # 让容器里的 uid 有机会写：宿主上放开权限，避免把「本机 umask」误读成「参数写错」
    root.chmod(stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
    try:
        result = runtime.run(
            image=IMAGE,
            command="sh -c 'echo hello > /workspace/probe.txt && cat /workspace/probe.txt'",
            mounts=[MountSpec(source=str(root), target="/workspace")],
        )
        assert result.ok is True, result.error
        assert result.exit_code == 0, f"工作区写不进：{result.stderr[:200]}"
        assert "hello" in result.stdout
        assert (root / "probe.txt").read_text(encoding="utf-8").strip() == "hello"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_timeout_kills_the_container(runtime: DockerRuntime) -> None:
    """超时必须真的掐掉容器，否则一条挂死的验证器会拖住整个队列。"""
    result = runtime.run(image=IMAGE, command="sleep 30", timeout_seconds=2)
    assert result.timed_out is True, f"没有超时：ok={result.ok} err={result.error}"


def test_container_user_follows_host(runtime: DockerRuntime) -> None:
    """``--user`` 跟随宿主（不是写死 1000:1000），容器里看到的 uid 应当就是它。"""
    expected = default_container_user().split(":")[0]
    result = runtime.run(image=IMAGE, command="id -u")
    assert result.ok is True, result.error
    assert result.stdout.strip() == expected, (
        f"容器内 uid={result.stdout.strip()!r}，期望 {expected!r}"
    )
