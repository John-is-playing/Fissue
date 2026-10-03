"""环境变量 / .env 加载测试。

覆盖三类容易出错的地方：
1. **优先级**：进程环境变量 > .env > config.yaml > 默认值。
2. **类型与报错**：整数项非法值要给可操作的 ConfigError，而不是裸 ValueError。
3. **接线完整性**：示例文件里承诺的每个变量都必须真的被读取（防「写了没用」）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from fissue.config import ENV_PREFIX, ConfigError, load_settings
from fissue.models import Platform

REPO_ROOT = Path(__file__).resolve().parents[1]

BASE_OVERRIDES = {"repos": []}


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离外部干扰：清掉 FISSUE_* 变量，并把 cwd 切到临时目录。

    切目录是必要的——``load_settings()`` 不带参数时会读 ``./config.yaml``，
    若在仓库根跑测试就会读到真实配置，断言全乱。
    注意：显式传入一个**不存在**的 config 路径会按设计报错，
    所以这里把 cwd 挪开、参数传 ``None``。
    """
    for key in list(__import__("os").environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# 1. 优先级
# ---------------------------------------------------------------------------


def test_process_env_beats_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程环境变量优先级最高（容器/CI 临时覆盖靠它）。"""
    env = _write(tmp_path / ".env", "FISSUE_LLM_MODEL=from-dotenv\nFISSUE_LLM_API_KEY=from-dotenv-key\n")
    monkeypatch.setenv("FISSUE_LLM_MODEL", "from-shell")

    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.llm.model == "from-shell"          # shell 赢
    assert s.llm.api_key == "from-dotenv-key"   # shell 没设 → 用 .env


def test_dotenv_beats_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """.env 覆盖 config.yaml（.env 是长期默认值，yaml 是可提交的业务配置）。"""
    cfg = _write(tmp_path / "config.yaml", yaml.safe_dump({"llm": {"model": "from-yaml"}}))
    env = _write(tmp_path / ".env", "FISSUE_LLM_MODEL=from-dotenv\n")

    s = load_settings(cfg, env, overrides=BASE_OVERRIDES)
    assert s.llm.model == "from-dotenv"


def test_yaml_beats_defaults(tmp_path: Path) -> None:
    """config.yaml 覆盖内置默认值。"""
    cfg = _write(tmp_path / "config.yaml", yaml.safe_dump({"llm": {"model": "from-yaml"}}))
    s = load_settings(cfg, tmp_path / "missing.env", overrides=BASE_OVERRIDES)
    assert s.llm.model == "from-yaml"


def test_defaults_when_nothing_set(tmp_path: Path) -> None:
    """全都缺省时用内置默认值。"""
    s = load_settings(None, tmp_path / "missing.env", overrides=BASE_OVERRIDES)
    assert s.llm.model == "deepseek-chat"
    assert s.web.port == 8000


def test_missing_dotenv_is_not_an_error(tmp_path: Path) -> None:
    """没有 .env 文件不该报错（首次运行友好）。"""
    s = load_settings(None, tmp_path / "missing.env", overrides=BASE_OVERRIDES)
    assert s.env_path is None


def test_env_path_is_recorded(tmp_path: Path) -> None:
    env = _write(tmp_path / ".env", "FISSUE_LLM_API_KEY=k\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.env_path == str(env)


def test_empty_value_does_not_override(tmp_path: Path) -> None:
    """`.env` 里的空值视为「未设置」，不该把 yaml 的值清成空。"""
    cfg = _write(tmp_path / "config.yaml", yaml.safe_dump({"llm": {"model": "from-yaml"}}))
    env = _write(tmp_path / ".env", "FISSUE_LLM_MODEL=\n")
    s = load_settings(cfg, env, overrides=BASE_OVERRIDES)
    assert s.llm.model == "from-yaml"


# ---------------------------------------------------------------------------
# 2. 各项变量的实际接线
# ---------------------------------------------------------------------------


def test_database_url(tmp_path: Path) -> None:
    env = _write(tmp_path / ".env", "FISSUE_DATABASE_URL=sqlite:///tmp/x.db\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.database.url == "sqlite:///tmp/x.db"


def test_llm_block(tmp_path: Path) -> None:
    env = _write(
        tmp_path / ".env",
        "\n".join([
            "FISSUE_LLM_API_KEY=key-1",
            "FISSUE_LLM_BASE_URL=https://llm.example/v1",
            "FISSUE_LLM_MODEL=model-1",
            "FISSUE_LLM_FALLBACK_MODEL=model-2",
        ]),
    )
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.llm.api_key == "key-1"
    assert s.llm.base_url == "https://llm.example/v1"
    assert s.llm.model == "model-1"
    assert s.llm.fallback_model == "model-2"
    assert s.llm.chat_completions_url == "https://llm.example/v1/chat/completions"


@pytest.mark.parametrize(
    "var,platform",
    [
        ("FISSUE_GITHUB_TOKEN", Platform.GITHUB),
        ("FISSUE_GITEE_TOKEN", Platform.GITEE),
        ("FISSUE_ATOMGIT_TOKEN", Platform.ATOMGIT),
        ("FISSUE_GITLAB_TOKEN", Platform.GITLAB),
    ],
)
def test_platform_tokens(tmp_path: Path, var: str, platform: Platform) -> None:
    env = _write(tmp_path / ".env", f"{var}=tok-{platform.value}\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.tokens.get(platform) == f"tok-{platform.value}"


def test_app_log_level(tmp_path: Path) -> None:
    env = _write(tmp_path / ".env", "FISSUE_LOG_LEVEL=debug\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.app.log_level == "debug"


def test_sandbox_forwarder_socket(tmp_path: Path) -> None:
    """socket 变量要真的接到 sandbox.forwarder.socket 上。"""
    env = _write(tmp_path / ".env", "FISSUE_SANDBOX_RUNNER_SOCKET=/run/custom.sock\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.sandbox.forwarder.socket == "/run/custom.sock"


def test_sandbox_socket_does_not_clobber_other_forwarder_fields(tmp_path: Path) -> None:
    """只改 socket，不该把 forwarder 的其它字段（enabled/protocol）弄丢。"""
    cfg = _write(
        tmp_path / "config.yaml",
        yaml.safe_dump({"sandbox": {"forwarder": {"enabled": False, "protocol": "tcp"}}}),
    )
    env = _write(tmp_path / ".env", "FISSUE_SANDBOX_RUNNER_SOCKET=/run/x.sock\n")
    s = load_settings(cfg, env, overrides=BASE_OVERRIDES)
    assert s.sandbox.forwarder.socket == "/run/x.sock"
    assert s.sandbox.forwarder.enabled is False
    assert s.sandbox.forwarder.protocol == "tcp"


def test_web_host_and_port(tmp_path: Path) -> None:
    env = _write(tmp_path / ".env", "FISSUE_WEB_HOST=0.0.0.0\nFISSUE_WEB_PORT=9001\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.web.host == "0.0.0.0"
    assert s.web.port == 9001


def test_api_token_enables_auth_automatically(tmp_path: Path) -> None:
    """填了 API_TOKEN 就自动开启鉴权（防呆：不用再记住改 auth_required）。"""
    env = _write(tmp_path / ".env", "FISSUE_API_TOKEN=s3cret\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.api.token == "s3cret"
    assert s.api.auth_required is True


def test_api_token_absent_keeps_auth_off(tmp_path: Path) -> None:
    env = _write(tmp_path / ".env", "FISSUE_API_TOKEN=\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.api.token == ""
    assert s.api.auth_required is False


# ---------------------------------------------------------------------------
# 3. 非法输入要给出可操作的报错
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc", "80.5", "八千"])
def test_invalid_port_raises_config_error(tmp_path: Path, bad: str) -> None:
    """端口非法 → ConfigError（而不是裸 ValueError 让用户看 traceback）。"""
    env = _write(tmp_path / ".env", f"FISSUE_WEB_PORT={bad}\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(None, env, overrides=BASE_OVERRIDES)
    assert "FISSUE_WEB_PORT" in str(exc.value)
    assert "整数" in str(exc.value)


def test_unknown_env_var_is_ignored(tmp_path: Path) -> None:
    """未知的 FISSUE_* 变量应被忽略（前向兼容，不炸）。"""
    env = _write(tmp_path / ".env", "FISSUE_NOT_A_REAL_OPTION=1\nFISSUE_LLM_MODEL=m\n")
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.llm.model == "m"


def test_quoted_values_are_parsed(tmp_path: Path) -> None:
    """带引号/含空格的值应能正确解析。"""
    env = _write(tmp_path / ".env", 'FISSUE_LLM_API_KEY="key with spaces"\n')
    s = load_settings(None, env, overrides=BASE_OVERRIDES)
    assert s.llm.api_key == "key with spaces"


def test_require_llm_key_flag(tmp_path: Path) -> None:
    env = _write(tmp_path / ".env", "FISSUE_LLM_MODEL=m\n")     # 故意不给 key
    with pytest.raises(ConfigError) as exc:
        load_settings(None, env, overrides=BASE_OVERRIDES, require_llm_key=True)
    assert "LLM API Key" in str(exc.value)

    env2 = _write(tmp_path / "ok.env", "FISSUE_LLM_API_KEY=k\n")
    s = load_settings(None, env2, overrides=BASE_OVERRIDES, require_llm_key=True)
    assert s.llm.api_key == "k"


# ---------------------------------------------------------------------------
# 4. 防漂移：示例文件 vs 代码实际读取
# ---------------------------------------------------------------------------


def _vars_read_by_code() -> set[str]:
    """从源码里提取全部会被读取的 FISSUE_* 变量名。

    覆盖 config.py（主配置）与 net.py（TLS 相关）两处：
    ``env_of("X")`` / ``port_of("X")`` / ``for plat in (...)`` / ``ENV_X = "FISSUE_X"``。
    """
    src = (REPO_ROOT / "src" / "fissue" / "config.py").read_text(encoding="utf-8")
    # 直接读取：env_of("X")
    names = set(re.findall(r'env_of\("([A-Z_]+)"\)', src))
    # 经类型转换读取：port_of("X")（WEB_PORT 走的就是这条）
    names |= set(re.findall(r'port_of\("([A-Z_]+)"\)', src))
    # 平台 token 是循环拼接的：for plat in ("GITHUB", ...) → GITHUB_TOKEN ...
    for m in re.finditer(r'for plat in \(([^)]+)\)', src):
        for tok in re.findall(r'"([A-Z_]+)"', m.group(1)):
            names.add(f"{tok}_TOKEN")
    # 元配置走 os.environ.get：ENV_PREFIX + "CONFIG" / "ENV"
    names |= set(re.findall(r'ENV_PREFIX \+ "([A-Z_]+)"', src))

    # net.py 的 TLS 变量以常量形式声明：ENV_CA_BUNDLE = "FISSUE_CA_BUNDLE"
    net_src = (REPO_ROOT / "src" / "fissue" / "net.py").read_text(encoding="utf-8")
    names |= set(re.findall(r'ENV_[A-Z_]+ = "FISSUE_([A-Z_]+)"', net_src))
    return names


def _vars_shown_in_example() -> set[str]:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    # 匹配「生效的」或「被注释掉的」两种赋值行
    return set(re.findall(r"^#?\s*FISSUE_([A-Z_]+)=", text, re.M))


def test_env_example_covers_every_variable_code_reads() -> None:
    """示例文件必须列出代码会读的每个变量——防止「文档里没有、代码偷偷读」。"""
    missing = _vars_read_by_code() - _vars_shown_in_example()
    assert not missing, f".env.example 缺少这些变量的说明：{sorted(missing)}"


def test_env_example_lists_no_phantom_variables() -> None:
    """示例文件不该宣传代码根本不读的变量——防止「写了不生效」。"""
    phantom = _vars_shown_in_example() - _vars_read_by_code()
    assert not phantom, f".env.example 列了代码不读的变量：{sorted(phantom)}"


def test_env_example_is_parseable_and_has_required_keys() -> None:
    """示例文件本身要能被 dotenv 解析，且必须包含最小必需项。"""
    from dotenv import dotenv_values

    values = dotenv_values(REPO_ROOT / ".env.example")
    assert "FISSUE_LLM_API_KEY" in values
    assert "FISSUE_DATABASE_URL" in values
    # 不能有生效的真实密钥被误提交
    assert values["FISSUE_LLM_API_KEY"].startswith("sk-your-key")
    for key in ("FISSUE_GITHUB_TOKEN", "FISSUE_GITEE_TOKEN", "FISSUE_ATOMGIT_TOKEN", "FISSUE_GITLAB_TOKEN"):
        assert values.get(key) in (None, ""), f"{key} 在示例里应为空"


def test_env_example_can_load_as_valid_settings(tmp_path: Path) -> None:
    """示例文件原样加载必须是通过校验的合法配置（用户 copy 后就能跑）。"""
    s = load_settings(
        REPO_ROOT / "config.example.yaml",
        REPO_ROOT / ".env.example",
        overrides={"repos": [{"platform": "github", "owner": "a", "name": "b"}]},
    )
    assert s.llm.api_key.startswith("sk-your-key")
    assert s.database.url.startswith("postgresql")
    assert s.web.port == 8000
