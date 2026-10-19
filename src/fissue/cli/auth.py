"""``fissue auth`` —— 账号与凭据的**自救通道**。

为什么必须有 CLI 而不只是 WebUI：`docs/AUTH.md` §8 把「锁定自救」列成一条安全要点。
WebUI 是进不去的那一侧——口令忘了、2FA 手机丢了、Key 全吊销了，界面上没有任何入口能
把自己救回来（能改口令的接口本身就要求先登录）。所以这几条命令必须在**本机终端**里可用，
它们也是「唯一的后门」：只认磁盘上的配置文件与数据库，不接受任何网络输入。
"""

from __future__ import annotations

import typer

from ..config import load_settings
from ..logging_setup import setup_logging

auth_app = typer.Typer(help="账号与凭据：改口令 / 清 2FA / 管理 API Key / 看状态")

CONFIG_OPT = typer.Option(None, "--config", "-c", help="config.yaml 路径")
ENV_OPT = typer.Option(None, "--env", "-e", help=".env 路径")
VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="输出调试日志")


def _store(config: str | None, env: str | None, *, verbose: bool = False):
    """打开数据库并确保表存在，返回 ``(settings, database, AuthStore)``。

    ``create_all`` 不能省：这几条命令常在**新库**上跑（`auth set-password` 是初始化之后
    的第一条命令），表还不存在时直接查会 ``no such table``。
    """
    from ..auth import AuthStore
    from ..store.db import build_database

    settings = load_settings(config, env)
    setup_logging("debug" if verbose else settings.app.log_level)
    database = build_database(settings)
    database.create_all()
    return settings, database, AuthStore(database)


def _fail(message: str) -> None:
    typer.secho(f"❌ {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _ok(message: str) -> None:
    typer.secho(f"✅ {message}", fg=typer.colors.GREEN)


def _prompt_password(*, confirm: bool = True) -> str:
    """交互式输入口令（不回显）。

    ``hide_input=True`` 是重点：口令明文出现在终端里会被 scrollback 与截图带走。
    """
    password = typer.prompt("新口令（至少 8 位）", hide_input=True)
    if len(password) < 8:
        _fail(f"口令至少 8 位（当前 {len(password)} 位）。")
    if confirm:
        again = typer.prompt("再输一次", hide_input=True)
        if again != password:
            _fail("两次输入不一致。")
    return password


@auth_app.command("status")
def auth_status(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """看当前鉴权状态：有没有管理员、有没有开 2FA、有多少会话与 Key。"""
    from ..auth import bootstrap_token_path, read_bootstrap_token

    settings, _, store = _store(config, env, verbose=verbose)
    admin = store.admin()
    keys = store.list_keys()
    active = [k for k in keys if k.revoked_at is None]

    typer.echo(f"配置文件      : {settings.config_path or '(内置默认值)'}")
    typer.echo(f"数据库        : {settings.database.url.split('@')[-1]}")
    typer.echo(f"会话鉴权      : {'开启' if settings.web.auth.enabled else '关闭'}")
    typer.echo(f"会话时长      : {settings.web.auth.session_ttl_minutes} 分钟")
    typer.echo(f"可信网段      : {', '.join(settings.web.trusted_networks) or '(空：只有回环免登录)'}")
    typer.echo(f"监听          : {settings.web.host}:{settings.web.port}")
    if admin is None:
        token = read_bootstrap_token(settings)
        typer.echo("管理员账号    : **尚未创建**")
        if token:
            typer.echo(f"引导口令文件  : {bootstrap_token_path(settings)}（存在）")
            typer.echo(f"引导口令      : {token}")
        else:
            typer.echo("引导口令文件  : （不存在；下次启动 `fissue web`/`serve` 会生成）")
        raise typer.Exit(0)
    typer.echo(f"管理员        : {admin.username}")
    typer.echo(f"两步验证      : {'已开启' if admin.totp_enabled else '未开启'}")
    typer.echo(f"恢复码剩余    : {len(admin.recovery_code_hashes or [])} 个")
    typer.echo(f"活跃会话      : {len(store.list_sessions(limit=200))} 个")
    typer.echo(f"API Key       : {len(active)} 把有效 / 共 {len(keys)} 把")
    for row in keys:
        state = "已吊销" if row.revoked_at else "有效"
        typer.echo(f"  - {row.prefix:<12} {row.name}（{state}）")


@auth_app.command("set-password")
def auth_set_password(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    username: str = typer.Option("admin", "--username", help="用户名（不存在则创建）"),
    password: str | None = typer.Option(
        None, "--password", help="直接给口令（非交互；不给则交互式输入）"
    ),
) -> None:
    """设置某个账号的口令（账号不存在就创建）。

    这是「口令忘了」的唯一出口。改完会**注销该账号的全部会话**——用旧口令登进去的
    那些浏览器必须失效，否则「改口令」这个动作等于没做。
    """
    from ..auth import hash_password

    settings, _, store = _store(config, env, verbose=verbose)
    if password is None:
        password = _prompt_password()
    if len(password) < 8:
        _fail(f"口令至少 8 位（当前 {len(password)} 位）。")

    target = store.user_by_name(username)
    created = target is None
    if target is None:
        target = store.create_user(username, hash_password(password))
    else:
        store.set_password(target.id, hash_password(password))
    kicked = store.delete_sessions_for_user(target.id)
    _ok(
        f"{'已创建账号' if created else '已更新口令'}：{username}"
        + (f"（注销了 {kicked} 个会话）" if kicked else "")
    )
    if created and settings.web.auth.enabled:
        typer.echo("现在可以在浏览器打开 /login 用它登录了。")


@auth_app.command("reset")
def auth_reset(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    password: str | None = typer.Option(
        None, "--password", help="同时把口令也重设为它（不给则只清 2FA 与会话）"
    ),
    username: str = typer.Option("admin", "--username", help="用户名"),
) -> None:
    """解锁：清掉 2FA 与恢复码、注销全部会话（可选同时重设口令）。

    典型场景：手机丢了进不去 2FA，而恢复码也找不到了。此时界面上**没有任何入口**
    （能关 2FA 的接口要求先登录），只有本机终端这条通道。
    """
    from ..auth import hash_password

    _, _, store = _store(config, env, verbose=verbose)
    user = store.user_by_name(username)
    if user is None:
        _fail(f"账号不存在：{username}（可先用 fissue auth set-password 创建）")

    store.set_totp(user.id, secret=None, enabled=False)
    store.set_recovery_codes(user.id, [])
    kicked = store.delete_sessions_for_user(user.id)
    _ok(f"已清除 {username} 的两步验证与恢复码，注销 {kicked} 个会话")
    if password:
        if len(password) < 8:
            _fail(f"口令至少 8 位（当前 {len(password)} 位）。")
        store.set_password(user.id, hash_password(password))
        _ok("口令已一并重设")


key_app = typer.Typer(help="API Key 的创建 / 吊销（CLI，用于 WebUI 进不去的场合）")
auth_app.add_typer(key_app, name="key")


@key_app.command("list")
def key_list(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """列出全部 Key（只有 prefix 与名称，**没有明文**——库里也没有）。"""
    _, _, store = _store(config, env, verbose=verbose)
    rows = store.list_keys()
    if not rows:
        typer.echo("（还没有任何 Key）")
        raise typer.Exit(0)
    typer.echo(f"{'id':<5}{'prefix':<14}{'名称':<20}状态")
    for row in rows:
        typer.echo(
            f"{row.id:<5}{row.prefix:<14}{(row.name or ''):<20}"
            f"{'已吊销' if row.revoked_at else '有效'}"
        )


@key_app.command("create")
def key_create(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    name: str = typer.Option("CLI 创建", "--name", help="给 Key 起个名字"),
) -> None:
    """创建一把 Key。**明文只在这里打印这一次**。"""
    from ..auth import generate_api_key, hash_token

    _, _, store = _store(config, env, verbose=verbose)
    prefix, secret, plaintext = generate_api_key()
    row = store.create_key(name=name, prefix=prefix, key_hash=hash_token(secret))
    _ok(f"已创建：{name}（id={row.id}, prefix={prefix}）")
    typer.echo("")
    typer.secho("  这是唯一一次显示明文，请现在保存：", fg=typer.colors.YELLOW)
    typer.secho(f"  {plaintext}", fg=typer.colors.YELLOW, bold=True)
    typer.echo("")
    typer.echo("用法：Authorization: Bearer <上面这串>")


@key_app.command("revoke")
def key_revoke(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    key_id: int = typer.Option(..., "--id", help="要吊销的 Key id（见 fissue auth key list）"),
) -> None:
    """吊销一把 Key（软删：行保留下来用于审计）。"""
    _, _, store = _store(config, env, verbose=verbose)
    if not store.revoke_key(key_id):
        _fail(f"没有这把 Key，或它已经被吊销：id={key_id}")
    _ok(f"已吊销：id={key_id}")


@key_app.command("import-legacy")
def key_import_legacy(
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    token: str = typer.Option(
        ..., "--token", help="旧口令明文（FISSUE_API_TOKEN 那个值）", hide_input=True
    ),
) -> None:
    """把一把旧风格口令导入成 API Key（兼容垫片，见 docs/AUTH.md §7）。"""
    from ..auth import LEGACY_KEY_PREFIX, hash_token

    _, _, store = _store(config, env, verbose=verbose)
    if any(row.prefix == LEGACY_KEY_PREFIX for row in store.list_keys()):
        _fail("已经导入过一条旧口令了；请先吊销它（auth key list / auth key revoke）再导入。")
    row = store.create_key(
        name="旧口令（CLI 导入）", prefix=LEGACY_KEY_PREFIX, key_hash=hash_token(token.strip())
    )
    _ok(f"已导入：id={row.id}，现在它可以作为 Bearer 使用")
