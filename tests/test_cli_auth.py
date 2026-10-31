"""``fissue auth`` CLI —— 本机自救通道的测试。

为什么这组用例值得单独立一个文件：``fissue auth`` 是**界面上进不去时唯一能救自己**
的出口（口令忘了 / 手机丢了 2FA / Key 全吊销了）。改口令的那个接口本身就要求先登录，
所以 WebUI 是进不去的那一侧——这条命令就是「唯一的后门」。

它此前**一条测试都没有**（``cli/auth.py`` 覆盖率 21%）。于是下面这些**安全性质**
全靠文档承诺，而本项目反复吃亏的地方正是「应该是这样」：

* 改口令必须注销该账号的**全部**会话——否则「口令泄露后改口令」这个动作几乎不起作用；
* ``reset`` 必须真的清掉 TOTP 密钥与恢复码（只关开关、留着密钥等于「关了还能用旧码开」）；
* Key 明文只在创建那一刻出现一次，库里绝不留明文；
* 口令太短 / 账号不存在 / 吊销一把不存在的 Key，都要**明确报错并给非零退出码**，
  而不是抛裸异常或静默成功。

所以下面钉的是**性质**，不是「命令能跑」。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from fissue.cli.main import app

runner = CliRunner()

#: Key 明文的形状（`fissue.auth.crypto.generate_api_key`）。
_KEY_RE = re.compile(r"fissue_[0-9a-f]+_[0-9a-f]+")


# ---------------------------------------------------------------------------
# 夹具：一份完全隔离的配置（SQLite + 临时 data_dir）
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """独立 config.yaml / .env / 数据目录，并切到临时 cwd。

    必须显式传 ``--config`` / ``--env``：否则 ``load_settings()`` 会去读仓库根那份
    **开发者真实配置**（含真实 token 与 data_dir），测试就会被环境污染。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FISSUE_DATABASE_URL", raising=False)
    monkeypatch.delenv("FISSUE_LLM_API_KEY", raising=False)

    db_path = (tmp_path / "auth.db").as_posix()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "database": {"url": f"sqlite:///{db_path}"},
                "app": {"data_dir": str(tmp_path / "data"), "log_level": "warning"},
                "export": {"output_dir": str(tmp_path / "exports")},
                "notify": {"enabled": False, "channels": []},
            }
        ),
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text("FISSUE_LLM_API_KEY=test-key\n", encoding="utf-8")

    return {
        "config": str(cfg),
        "env": str(env),
        "db": f"sqlite:///{db_path}",
        "root": str(tmp_path),
    }


def _run(args: list[str], env: dict[str, str]) -> Any:
    """统一带上 --config / --env 执行。"""
    return runner.invoke(app, [*args, "--config", env["config"], "--env", env["env"]])


def _store(env: dict[str, str]):
    """直接开一个 AuthStore（用于准备前置状态、断言落库结果）。"""
    from fissue.auth import AuthStore
    from fissue.store.db import Database

    db = Database(env["db"])
    db.create_all()
    return AuthStore(db)


# ---------------------------------------------------------------------------
# 1. status：看当前鉴权状态
# ---------------------------------------------------------------------------


def test_auth_status_on_fresh_db_reports_no_admin_yet(auth_env: dict[str, str]) -> None:
    """全新库上跑 status 不该崩，并要说清「管理员还没建」。

    这条是**自救通道的入口**：用户拿不到口令时第一个跑的就是它。
    """
    result = _run(["auth", "status"], auth_env)

    assert result.exit_code == 0, result.output
    assert result.exception is None, f"不该有未捕获异常：{result.exception!r}"
    assert "尚未创建" in result.output
    assert "引导口令文件" in result.output


def test_auth_status_lists_admin_keys_and_sessions(auth_env: dict[str, str]) -> None:
    """建过账号后，status 要把管理员 / 2FA / 会话数 / Key 数都报出来。

    这些数字是用户判断「我到底有没有锁在外面」的唯一依据——少报一项，
    用户就只能靠猜。
    """
    store = _store(auth_env)
    from fissue.auth import hash_password

    user = store.create_user("admin", hash_password("correct-horse"))
    store.create_session(
        user_id=user.id, session_id="sess-1", csrf_token="tok", ttl_minutes=60
    )
    store.create_key(name="我的机器", prefix="abcd1234", key_hash="deadbeef")

    result = _run(["auth", "status"], auth_env)

    assert result.exit_code == 0, result.output
    assert "管理员        : admin" in result.output
    assert "两步验证      : 未开启" in result.output
    assert "活跃会话      : 1 个" in result.output
    assert "1 把有效" in result.output
    assert "我的机器" in result.output


def test_auth_status_never_leaks_hashes(auth_env: dict[str, str]) -> None:
    """status 里不能出现口令哈希或 Key 哈希。

    这条命令的输出会被用户直接粘进 issue / 聊天窗口去求助——把哈希一并带出去，
    等于把「离线爆破」的素材递到别人手上。
    """
    store = _store(auth_env)
    from fissue.auth import hash_password

    store.create_user("admin", hash_password("correct-horse"))
    store.create_key(name="k", prefix="abcd1234", key_hash="deadbeefhash")

    result = _run(["auth", "status"], auth_env)

    assert result.exit_code == 0, result.output
    assert "pbkdf2_sha256$" not in result.output
    assert "deadbeefhash" not in result.output


# ---------------------------------------------------------------------------
# 2. set-password：建 / 改口令
# ---------------------------------------------------------------------------


def test_set_password_creates_the_account_and_stores_only_a_hash(
    auth_env: dict[str, str],
) -> None:
    """建账号：口令要能验回来，但库里存的必须是哈希而不是明文。"""
    result = _run(["auth", "set-password", "--password", "correct-horse"], auth_env)

    assert result.exit_code == 0, result.output
    assert "已创建账号" in result.output

    store = _store(auth_env)
    user = store.user_by_name("admin")
    assert user is not None
    assert user.password_hash != "correct-horse"
    assert user.password_hash.startswith("pbkdf2_sha256$")

    from fissue.auth import verify_password

    assert verify_password("correct-horse", user.password_hash) is True


def test_set_password_rejects_a_short_password(auth_env: dict[str, str]) -> None:
    """短口令必须被拒，且**不建账号**。

    失败后留一个 3 位口令的账号，比直接报错糟糕得多——那等于留了个后门。
    """
    result = _run(["auth", "set-password", "--password", "short"], auth_env)

    assert result.exit_code == 1
    assert "至少 8 位" in result.output

    store = _store(auth_env)
    assert store.user_by_name("admin") is None, "被拒的口令不该留下账号"


def test_set_password_kicks_every_session_of_that_account(auth_env: dict[str, str]) -> None:
    """改口令必须注销该账号的**全部**会话。

    这是这条命令存在的**全部意义**：口令泄露之后，「改口令」若不踢掉旧会话，
    拿着旧 cookie 的人照样能进——这个动作等于没做。
    """
    store = _store(auth_env)
    from fissue.auth import hash_password

    user = store.create_user("admin", hash_password("old-password"))
    store.create_session(user_id=user.id, session_id="s1", csrf_token="t", ttl_minutes=60)
    store.create_session(user_id=user.id, session_id="s2", csrf_token="t", ttl_minutes=60)

    result = _run(["auth", "set-password", "--password", "brand-new-one"], auth_env)

    assert result.exit_code == 0, result.output
    assert "注销了 2 个会话" in result.output
    assert store.list_sessions() == [], "旧会话必须全部失效"
    assert verify_password_new(store, "brand-new-one") is True


def verify_password_new(store: Any, password: str) -> bool:
    """小工具：拿库里最新的口令哈希验一次口令。"""
    from fissue.auth import verify_password

    user = store.user_by_name("admin")
    assert user is not None
    return bool(verify_password(password, user.password_hash))


def test_set_password_on_existing_account_updates_it(auth_env: dict[str, str]) -> None:
    """已存在的账号走「更新」分支，口令要真的换掉。"""
    store = _store(auth_env)
    from fissue.auth import hash_password

    store.create_user("admin", hash_password("old-password"))

    result = _run(["auth", "set-password", "--password", "brand-new-one"], auth_env)

    assert result.exit_code == 0, result.output
    assert "已更新口令" in result.output
    assert verify_password_new(store, "brand-new-one") is True
    assert verify_password_new(store, "old-password") is False


def test_set_password_prompts_interactively_when_no_password_given(
    auth_env: dict[str, str],
) -> None:
    """不给 ``--password`` 时走交互式输入（不回显）——这是最常见的用法路径。"""
    result = runner.invoke(
        app,
        ["auth", "set-password", "--config", auth_env["config"], "--env", auth_env["env"]],
        input="interactive-pass\ninteractive-pass\n",
    )

    assert result.exit_code == 0, result.output
    assert verify_password_new(_store(auth_env), "interactive-pass") is True


def test_set_password_rejects_mismatched_confirmation(auth_env: dict[str, str]) -> None:
    """两次输入不一致必须报错——否则「以为改了、其实没改」比不改更危险。"""
    result = runner.invoke(
        app,
        ["auth", "set-password", "--config", auth_env["config"], "--env", auth_env["env"]],
        input="one-password\nother-password\n",
    )

    assert result.exit_code == 1
    assert "两次输入不一致" in result.output
    assert _store(auth_env).user_by_name("admin") is None


# ---------------------------------------------------------------------------
# 3. reset：清 2FA / 恢复码 / 会话
# ---------------------------------------------------------------------------


def test_reset_clears_totp_recovery_codes_and_sessions(auth_env: dict[str, str]) -> None:
    """reset 要把「进不去」的三样东西一次清干净。

    关键是 **TOTP 密钥也要忘掉**：只把 ``totp_enabled`` 置 false 而留着密钥，
    等于「关了还能用旧码开」——那这个 reset 就没修好问题。
    """
    store = _store(auth_env)
    from fissue.auth import hash_password

    user = store.create_user("admin", hash_password("correct-horse"))
    store.set_totp(user.id, secret="JBSWY3DPEHPK3PXP", enabled=True)
    store.set_recovery_codes(user.id, ["hash1", "hash2"])
    store.create_session(user_id=user.id, session_id="s1", csrf_token="t", ttl_minutes=60)

    result = _run(["auth", "reset"], auth_env)

    assert result.exit_code == 0, result.output
    assert "已清除" in result.output

    after = store.user_by_name("admin")
    assert after is not None
    assert after.totp_enabled is False
    assert after.totp_secret is None, "密钥必须忘掉：留着等于关了还能用旧码开"
    assert after.recovery_code_hashes == []
    assert store.list_sessions() == []


def test_reset_can_also_reset_the_password(auth_env: dict[str, str]) -> None:
    """``reset --password`` 要连口令一起换掉。"""
    store = _store(auth_env)
    from fissue.auth import hash_password

    store.create_user("admin", hash_password("correct-horse"))

    result = _run(["auth", "reset", "--password", "brand-new-one"], auth_env)

    assert result.exit_code == 0, result.output
    assert "口令已一并重设" in result.output
    assert verify_password_new(store, "brand-new-one") is True


def test_reset_rejects_a_short_password_without_touching_the_account(
    auth_env: dict[str, str],
) -> None:
    """``reset --password`` 给短口令要被拒，且**不能**先把 2FA 清了。

    失败路径的顺序很重要：先清 2FA 再校验口令，等于「口令没改成、但 2FA 已经被清了」，
    用户反而更难进去。
    """
    store = _store(auth_env)
    from fissue.auth import hash_password

    user = store.create_user("admin", hash_password("correct-horse"))
    store.set_totp(user.id, secret="JBSWY3DPEHPK3PXP", enabled=True)

    result = _run(["auth", "reset", "--password", "short"], auth_env)

    assert result.exit_code == 1
    assert "至少 8 位" in result.output

    after = store.user_by_name("admin")
    assert after is not None
    assert after.totp_enabled is True, "口令校验不过就不该动 2FA 状态"
    assert verify_password_new(store, "correct-horse") is True


def test_reset_on_unknown_account_fails_with_a_clear_message(auth_env: dict[str, str]) -> None:
    """账号不存在要明确报错，而不是抛 `AttributeError: 'NoneType'`。"""
    result = _run(["auth", "reset", "--username", "nobody"], auth_env)

    assert result.exit_code == 1
    assert "账号不存在" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# 4. key：API Key 的创建 / 列出 / 吊销
# ---------------------------------------------------------------------------


def test_key_create_shows_the_plaintext_once_and_stores_only_a_hash(
    auth_env: dict[str, str],
) -> None:
    """明文只在创建那一刻打印一次；库里存的是哈希，绝不是明文。"""
    result = _run(["auth", "key", "create", "--name", "我的机器"], auth_env)

    assert result.exit_code == 0, result.output
    assert "唯一一次显示明文" in result.output

    match = _KEY_RE.search(result.output)
    assert match is not None, f"输出里应有 Key 明文：{result.output}"
    plaintext = match.group(0)

    store = _store(auth_env)
    rows = store.list_keys()
    assert len(rows) == 1
    assert rows[0].key_hash != plaintext
    assert plaintext not in rows[0].key_hash
    assert rows[0].name == "我的机器"


def test_key_list_shows_the_prefix_but_never_the_plaintext(auth_env: dict[str, str]) -> None:
    """list 只给 prefix 与名称——库里本来就没有明文，输出也不该有。"""
    store = _store(auth_env)
    store.create_key(name="我的机器", prefix="abcd1234", key_hash="deadbeef")

    result = _run(["auth", "key", "list"], auth_env)

    assert result.exit_code == 0, result.output
    assert "abcd1234" in result.output
    assert "我的机器" in result.output
    assert "deadbeef" not in result.output


def test_key_list_on_empty_store_says_so(auth_env: dict[str, str]) -> None:
    """一把 Key 都没有时要说清，而不是打一张空表让人以为出错了。"""
    result = _run(["auth", "key", "list"], auth_env)

    assert result.exit_code == 0, result.output
    assert "还没有任何 Key" in result.output


def test_key_revoke_marks_the_key_revoked(auth_env: dict[str, str]) -> None:
    """吊销是**软删**：行留着供审计，但 ``revoked_at`` 要落上。"""
    store = _store(auth_env)
    row = store.create_key(name="k", prefix="abcd1234", key_hash="deadbeef")

    result = _run(["auth", "key", "revoke", "--id", str(row.id)], auth_env)

    assert result.exit_code == 0, result.output
    assert "已吊销" in result.output

    after = store.key_by_prefix("abcd1234")
    assert after is not None, "行必须留着（审计要看得见它曾经存在）"
    assert after.revoked_at is not None


def test_key_revoke_unknown_id_fails_with_a_clear_message(auth_env: dict[str, str]) -> None:
    """吊销一把不存在的 Key 要明确报错并给非零退出码（可放在脚本里用）。"""
    result = _run(["auth", "key", "revoke", "--id", "9999"], auth_env)

    assert result.exit_code == 1
    assert "9999" in result.output
    assert "Traceback" not in result.output


def test_key_revoke_twice_fails_the_second_time(auth_env: dict[str, str]) -> None:
    """重复吊销要报错——它同时说明「这把已经废了」，而不是假装成功。"""
    store = _store(auth_env)
    row = store.create_key(name="k", prefix="abcd1234", key_hash="deadbeef")

    first = _run(["auth", "key", "revoke", "--id", str(row.id)], auth_env)
    second = _run(["auth", "key", "revoke", "--id", str(row.id)], auth_env)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 1
    assert "已经被吊销" in second.output


# ---------------------------------------------------------------------------
# 5. 注册结构：命令确实挂在 app 上
# ---------------------------------------------------------------------------


def test_auth_cli_is_registered_with_all_its_subcommands() -> None:
    """``fissue auth`` 与 ``fissue auth key`` 两级子命令都要真的挂上。

    挂不上不会报错——只会在用户敲进去时才发现「No such command」，
    而那正是他最需要帮助的时刻。
    """
    groups = {g.name: g for g in app.registered_groups}
    assert "auth" in groups

    auth_typer = groups["auth"].typer_instance
    assert auth_typer is not None
    names = {c.name for c in auth_typer.registered_commands}
    assert {"status", "set-password", "reset"} <= names

    key_subgroups = {g.name: g for g in auth_typer.registered_groups}
    assert "key" in key_subgroups
    key_typer = key_subgroups["key"].typer_instance
    assert key_typer is not None
    key_names = {c.name for c in key_typer.registered_commands}
    assert {"list", "create", "revoke"} <= key_names
