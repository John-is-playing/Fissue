#!/usr/bin/env python3
"""容器入口：修目录属主 → 降到普通用户 → 选启动模式 → exec 服务本体。

为什么不直接用 `CMD ["fissue", "web"]`（这几件事都不是多余的包装）：

1. **降权**：镜像以 root 启动，只为把 /data 与沙盒工作区这两个目录的属主修成运行用户。
   宿主的 bind 目录若不存在，Docker 会替你创建并归 **root** 所有；而容器以 uid 1000 运行，
   于是「第一次启动就写不进去、报 Permission denied」，用户却只会看到一堆无关的报错。
   修完立刻 `setuid(1000)`，服务本体**不以 root 运行**（也不是 privileged）。

2. **模式选择**：「LLM key 留空也能起」和「配好 key 就自动跑流水线」是两条不同的路：
   * `fissue web`              只提供 Web 工作台 + REST API，加载配置时**不要求** key；
   * `fissue serve --with-web` 是常驻流水线（扫描/评测/验证/通知），加载配置时**硬性要求**
     key，缺了直接退出（cli/ops.py 里 `require_llm=True` 的效果，不该在这里绕开）。
   默认 `FISSUE_MODE=auto`：有 key 起流水线，没 key 起界面——两条路都不用用户改命令。

3. **TMPDIR**：沙盒要求「宿主路径 = 容器内路径」（DooD 只认宿主路径，而工作副本由本容器
   创建），所以临时目录要指到那个共享目录上。但它**可写才指过去**：指到一个不可写的
   目录（Docker Desktop 的共享目录常有这种限制）会让 tempfile 全线报错，比沙盒不可用
   严重得多。

覆盖方式：
  * `FISSUE_MODE=web` / `serve` / `sandbox-runner` 强制某种模式；
  * 给容器传命令（如 `docker compose run --rm fissue fissue status`）则原样执行，不再挑选。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 镜像里创建的普通用户（uid/gid 与 docker/sandbox 的 sandbox 用户一致：不少镜像与夹具
#: 都假定 1000:1000，且沙盒容器默认跟随转发组件的 uid）。
APP_UID = int(os.environ.get("FISSUE_APP_UID", "1000"))
APP_GID = int(os.environ.get("FISSUE_APP_GID", "1000"))

#: 固定要准备属主的目录：数据卷、转发组件 socket 目录。
FIXED_DIRS = ("/data", "/run/fissue-sandbox")


def log(msg: str) -> None:
    print(f"[entrypoint] {msg}", flush=True)


def workspace_dir() -> str | None:
    """沙盒工作区（compose 里由 FISSUE_SANDBOX_WORKSPACE 给出，宿主路径与容器路径相同）。"""
    return os.environ.get("FISSUE_SANDBOX_WORKSPACE") or None


def prepare_dirs() -> None:
    """以 root 身份把需要的目录建好、属主改成运行用户（改不动就告警，不拦启动）。"""
    if os.geteuid() != 0:
        return
    targets = [*FIXED_DIRS]
    ws = workspace_dir()
    if ws:
        targets.append(ws)
    for raw in sorted(set(targets)):
        path = Path(raw)
        try:
            path.mkdir(parents=True, exist_ok=True)
            # 已经是运行用户的就不动：转发组件把 /data 挂成只读，对一个只读挂载点
            # 发 chown 只会换回一句 EROFS 噪音（属主本来就对）。
            st = path.stat()
            if (st.st_uid, st.st_gid) != (APP_UID, APP_GID):
                os.chown(path, APP_UID, APP_GID)
        except OSError as exc:
            # 典型场景：宿主 bind 目录在只读文件系统上，或 Docker Desktop 的共享目录
            # 不让改属主。不拦启动（界面照常可用），但把「之后会写不进去」说清楚。
            log(f"⚠️  无法准备目录 {path}：{exc}（该目录若需要写入，服务会报 Permission denied）")


def drop_privileges() -> None:
    """从 root 降到运行用户。**不**调用 setgroups：`group_add` 给的附加组（例如宿主 docker
    组的 gid）要留着，而 setuid 本身不会清掉它们。"""
    if os.geteuid() != 0 or APP_UID == 0:
        return
    try:
        os.setgid(APP_GID)
        os.setuid(APP_UID)
    except OSError as exc:  # pragma: no cover - 镜像异常
        log(f"⚠️  降权到 {APP_UID}:{APP_GID} 失败：{exc}（继续以 root 运行）")
        return
    os.environ.setdefault("HOME", "/home/fissue")


def writable(path: Path) -> bool:
    """以**当前（已降权的）用户**试写一次：以 root 探等于没探，root 哪都写得进。"""
    probe = path / f".fissue-probe-{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def configure_tmpdir() -> None:
    """把临时目录指到共享工作区（沙盒需要），不可写就退回容器自己的 /tmp。"""
    ws = workspace_dir()
    if not ws:
        return
    path = Path(ws)
    if writable(path):
        os.environ["TMPDIR"] = str(path)
        return
    os.environ.pop("TMPDIR", None)
    log(
        f"⚠️  沙盒工作区 {path} 不可写：临时目录退回 /tmp；"
        "沙盒（sandbox profile）会因此失败，界面与 API 不受影响。"
        "修法见 docs/COMPOSE.md「沙盒 profile」一节。"
    )


def llm_key_present() -> bool:
    """有没有可用的 LLM key（.env 与进程环境都算）。读不出来就当作没有，退回 web 模式。"""
    try:
        from fissue.config import load_settings

        return bool(load_settings(require_llm_key=False).llm.api_key)
    except Exception as exc:  # noqa: BLE001 - 配置坏了不该在这里崩，交给服务本体报错
        log(f"读取配置失败（{type(exc).__name__}: {exc}），退回 web 模式")
        return False


def build_command(mode: str) -> list[str]:
    port = os.environ.get("FISSUE_WEB_PORT", "8000")
    if mode == "sandbox-runner":
        # 转发组件：沙盒执行的唯一通道（只给 sandbox profile 里的那个容器用）。
        return ["fissue", "sandbox", "serve"]
    if mode == "serve":
        return ["fissue", "serve", "--with-web"]
    return ["fissue", "web", "--host", "0.0.0.0", "--port", str(port)]


def main() -> None:
    # 1) 传了命令就原样执行（`docker compose run --rm fissue fissue status` 这类用法）。
    forwarded = sys.argv[1:]
    if forwarded:
        os.execvp(forwarded[0], forwarded)

    # 2) 目录属主 → 降权 →（以普通用户身份）定下临时目录。
    prepare_dirs()
    drop_privileges()
    configure_tmpdir()

    # 3) 选模式。
    mode = (os.environ.get("FISSUE_MODE") or "auto").strip().lower()
    auto = mode == "auto"
    if auto:
        mode = "serve" if llm_key_present() else "web"
    if mode not in {"web", "serve", "sandbox-runner"}:
        log(f"未知 FISSUE_MODE={mode!r}，按 web 处理（可选：auto / web / serve / sandbox-runner）")
        mode = "web"

    cmd = build_command(mode)
    log(f"模式 {mode}：{' '.join(cmd)}")
    if mode != "sandbox-runner":
        log(f"Web/API 监听 0.0.0.0:{os.environ.get('FISSUE_WEB_PORT', '8000')}；数据目录 /data")
        if auto and mode == "web":
            log("未配置 LLM key：只起界面与 API，不跑轮次。在界面「设置」页填 key 后重启即可自动跑流水线。")
        log("首次启动若尚无管理员：日志会打印一次性引导口令（也在 /data/auth-bootstrap-token），")
        log("用它打开 /login 创建管理员账号。")

    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:  # pragma: no cover - 镜像损坏
        log(f"❌ 找不到命令 {cmd[0]}：镜像里没有装好 fissue")
        sys.exit(127)


if __name__ == "__main__":
    main()
