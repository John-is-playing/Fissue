"""配置加载与领域模型测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fissue.config import ConfigError, Settings, load_settings
from fissue.errors import ConfigError as ErrConfigError
from fissue.models import (
    Action,
    AuthorKind,
    Category,
    Evaluation,
    ItemStatus,
    Platform,
    Priority,
    QueueName,
    RawItem,
    RepoRef,
    VerifierKind,
    VerifierSpec,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_load_defaults_without_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无配置文件时应可加载（全默认值）。"""
    monkeypatch.chdir(tmp_path)                 # 避免读到仓库根的 config.yaml
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.llm.model
    assert s.database.url
    assert s.config_path is None


def test_missing_explicit_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_yaml_overrides_and_env_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {"model": "from-yaml", "base_url": "https://yaml.invalid/v1"},
                "repos": [{"platform": "gitee", "owner": "a", "name": "b"}],
            }
        ),
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text("FISSUE_LLM_API_KEY=secret-key\nFISSUE_GITHUB_TOKEN=gh-token\n", encoding="utf-8")

    s = load_settings(cfg, env)
    assert s.llm.model == "from-yaml"
    assert s.llm.api_key == "secret-key"          # 密钥只从 env 来
    assert s.tokens.github == "gh-token"
    assert s.repos[0].platform is Platform.GITEE

    # 环境变量覆盖 yaml
    monkeypatch.setenv("FISSUE_LLM_MODEL", "from-env")
    s2 = load_settings(cfg, env)
    assert s2.llm.model == "from-env"


def test_require_llm_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "none.yaml", require_llm_key=True)


def test_repo_name_with_slash_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        yaml.safe_dump({"repos": [{"platform": "github", "owner": "a", "name": "b/c"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_exposed_api_without_auth_is_refused(tmp_path: Path) -> None:
    """对外可达 + 启用 API + 无鉴权 → **拒绝启动**（fail-safe）。

    回归：这三者原先能同时成立，而 `/api/*` 里带着**会写外部仓库**的路由（`/api/flush`
    会真的去平台上打标签、把条目推进修复队列）。`api.auth_required` 默认 false，
    唯一的保险是「设了 `FISSUE_API_TOKEN` 才自动翻成 true」——于是把 `WEB_HOST` 指向
    外部（或放到反向代理后面）就等于**把写操作入口对所有人开放**。
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"web": {"host": "0.0.0.0"},
                        "api": {"enabled": True, "auth_required": False}}),
        encoding="utf-8",
    )
    # **必须传一个显式的空 env 文件**：`load_settings(cfg)` 默认会去读仓库的 `.env`，
    # 而那里有一个空的 `FISSUE_WEB_HOST=`（键在值空），它会把 host 打回默认的
    # `127.0.0.1`——用例就永远触发不了这条规则（我第一次就是这么写错的）。
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="FISSUE_API_TOKEN"):
        load_settings(cfg, env)


def test_loopback_api_without_auth_is_still_allowed(tmp_path: Path) -> None:
    """只绑本机时不拦——本地开发不该被迫配令牌（别把安全规则做成拦路虎）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"web": {"host": "127.0.0.1"},
                        "api": {"enabled": True, "auth_required": False}}),
        encoding="utf-8",
    )
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")

    s = load_settings(cfg, env)

    assert s.api.enabled and not s.api.auth_required


def test_repo_lookup_helpers(settings: Settings) -> None:
    r = settings.repo("psf/requests")
    assert r.platform is Platform.GITHUB
    assert settings.enabled_repos()
    with pytest.raises(ErrConfigError):
        settings.repo("nobody/nothing")


def test_chat_completions_url_builder(settings: Settings) -> None:
    assert settings.llm.chat_completions_url.endswith("/chat/completions")
    settings.llm.base_url = "https://x/v1/chat/completions"
    assert settings.llm.chat_completions_url == "https://x/v1/chat/completions"


def test_pricing_cost(settings: Settings) -> None:
    p = settings.llm.pricing
    assert p.cost(1000, 0) == pytest.approx(p.input_per_1k)
    assert p.cost(0, 1000) == pytest.approx(p.output_per_1k)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


def test_item_key_and_hash_stability(sample_issue: RawItem) -> None:
    assert sample_issue.key == "github:psf/requests#42"
    h1 = sample_issue.content_hash()
    assert h1 == sample_issue.content_hash()
    same = sample_issue.model_copy(deep=True)
    assert same.content_hash() == h1

    same.body = "改了正文"
    assert same.content_hash() != h1


def test_content_hash_ignores_comment_body_changes(sample_issue: RawItem) -> None:
    """指纹只看评论数量（避免评论编辑触发重复评测）。"""
    before = sample_issue.content_hash()
    sample_issue.comments[0].body = "改了评论内容"
    assert sample_issue.content_hash() == before


def test_evaluation_score_accessors(good_evaluation: Evaluation) -> None:
    assert good_evaluation.importance == 88
    assert good_evaluation.difficulty == 25
    assert good_evaluation.authenticity == 92
    assert good_evaluation.scores.as_dict() == {
        "authenticity": 92,
        "importance": 88,
        "feasibility": 80,
        "difficulty": 25,
    }


def test_evaluation_missing_dimensions_default(good_evaluation: Evaluation) -> None:
    empty = Evaluation(category=Category.BUG)
    assert empty.importance == 0
    assert empty.difficulty == 100          # 缺难度 → 视为很难，禁止自动修
    assert empty.authenticity == 0


def test_verifier_kind_is_executable() -> None:
    assert VerifierKind.EXECUTABLE.is_executable
    assert VerifierKind.SHELL.is_executable
    assert not VerifierKind.CHECKLIST.is_executable


def test_verifier_spec_defaults() -> None:
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, command="pytest")
    assert spec.timeout_seconds == 600
    assert spec.expect_fail_on_base is True
    assert spec.is_executable


def test_repo_ref_str_and_slug() -> None:
    ref = RepoRef(platform=Platform.ATOMGIT, owner="o", name="n")
    assert str(ref) == "atomgit:o/n"
    assert ref.slug == "o/n"


def test_enums_have_expected_members() -> None:
    assert {p.value for p in Platform} == {"github", "gitee", "atomgit", "gitlab"}
    assert {q.value for q in QueueName} == {"verify", "fix_bug", "fix_feature"}
    assert Category.BUG.value == "bug"
    assert ItemStatus.PR_CREATED.value == "pr_created"
    assert Action.FIX_NOW.value == "fix_now"
    assert Priority.TIER1.value == "tier1"
    assert AuthorKind.AI_AGENT.value == "ai_agent"


# ---------------------------------------------------------------------------
# P1：回归门默认档、已删除的空开关
# ---------------------------------------------------------------------------


def test_regression_gate_defaults_to_strict() -> None:
    """回归门默认必须**真拦**。

    「修好目标却弄坏别处」的补丁不该被自动提 PR；默认只提醒等于本门对外宣称的
    作用根本不生效。误杀顾虑由「base 不绿一律放行」的安全底线兜住。
    """
    from fissue.config import VerifierConfig

    assert VerifierConfig().regression_gate == "strict"


def test_pr_strategy_no_longer_has_auto_submit() -> None:
    """那个空开关不能再回来。

    `auto_submit` 曾被文档当成安全阀（「首次使用建议设成 false」），但代码从未
    读取它——设了照样自动提 PR。要只出补丁请用 `mode: patch_only` 或 `--dry-run`。
    """
    from fissue.config import PRStrategy

    assert not hasattr(PRStrategy(), "auto_submit")
    # 老配置文件里残留的那一行不该让加载直接报错（多余键被忽略）——故意传这个键
    assert PRStrategy(auto_submit=False).mode == "fork"  # type: ignore[call-arg]


def test_unknown_config_keys_are_reported_with_a_hint(tmp_path: Path) -> None:
    """拼错的配置键必须能被**指出来**：老键仍不报错，但不能一声不响地忽略。

    回归：`load_settings` 把 YAML 直接交给 pydantic，而 pydantic 默认忽略多余键——把
    `api.auth_required` 写成 `api.auth_requird`、`web.read_only` 写成 `web.readonly`，既不报错
    也不提示，而这两个都是**安全开关**：用户以为开了，实际按默认跑。
    """
    from fissue.config import _unknown_config_keys, load_settings

    found = dict(
        _unknown_config_keys({"api": {"auth_requird": True}, "web": {"readonly": True}, "nope": 1})
    )
    assert set(found) == {"api.auth_requird", "web.readonly", "nope"}
    assert "auth_required" in found["api.auth_requird"], "要给「你是不是想写…」的候选"
    assert "read_only" in found["web.readonly"]

    # 既有契约：老配置里的残留键（已删除的字段）仍**不报错**，只是会被忽略
    assert _unknown_config_keys({"auto_fix": {"pr_strategy": {"mode": "fork"}}, "repos": []}) == []

    # 端到端：带未知键的配置照旧能加载（不报错），未知键按默认值走
    cfg = tmp_path / "c.yaml"
    cfg.write_text("web:\n  readonly: true\nrepos: []\n", encoding="utf-8")
    settings = load_settings(cfg, tmp_path / "nope.env")
    assert settings.web.read_only is False, "未知键被忽略（不会变成 read_only）"


def test_pr_strategy_mode_rejects_typos() -> None:
    """拼错 `mode` 必须报错，不能静默落到「直推上游仓库」。

    `fixer.autofix` 只有**精确等于** `fork` 才走 fork（`patch_only` 只落补丁），其余取值一律
    `head_repo = 上游仓库` → 把分支直推到别人的仓库。于是 `Fork` / `frok` / 带尾空格这类拼写
    错误会静默关掉 fork 安全阀。
    """
    from fissue.config import PRStrategy
    from fissue.errors import ConfigError

    for good in ("fork", "direct", "patch_only"):
        assert PRStrategy(mode=good).mode == good

    for bad in ("Fork", "frok", "direct ", "dirct", ""):
        with pytest.raises(ConfigError):
            PRStrategy(mode=bad)


def test_platform_max_retry_after_default() -> None:
    """限流单次等待上限有默认值，且能按平台覆盖。"""
    from fissue.config import PlatformConfig

    assert PlatformConfig().max_retry_after_seconds == 300.0
    assert PlatformConfig(max_retry_after_seconds=30.0).max_retry_after_seconds == 30.0


@pytest.mark.parametrize(
    "cls_name,field",
    [
        ("AppConfig", "name"),
        ("AppConfig", "timezone"),
        ("EvaluationConfig", "dimensions"),
        ("EvaluationConfig", "score_range"),
        ("EvaluationConfig", "anti_spam"),
        ("SandboxForwarderConfig", "protocol"),
        ("QueuesConfig", "max_attempts"),
        ("AutoFixConfig", "run_tests_in_loop"),
        ("WebConfig", "allow_repo_management"),
        ("ExportConfig", "default_format"),
    ],
)
def test_dead_config_keys_are_gone(cls_name: str, field: str) -> None:
    """这些键曾写在示例配置里却**从未被代码读取**，已删除。

    留着比没有更危险：用户以为配上就生效。跟之前那个 `auto_submit` 是同一类问题。
    """
    import fissue.config as cfg

    instance = getattr(cfg, cls_name)()
    assert not hasattr(instance, field), f"{cls_name}.{field} 又回来了，但它没人读"


def test_deleted_keys_in_old_config_files_do_not_break_loading() -> None:
    """老配置文件里残留这些键不该让加载报错（多余键被忽略）。"""
    from fissue.config import SandboxForwarderConfig, WebConfig

    # 故意传已删除的键：考的就是 pydantic 会忽略它们
    assert SandboxForwarderConfig(protocol="unix").enabled is True  # type: ignore[call-arg]
    assert WebConfig(allow_repo_management=True).read_only is False  # type: ignore[call-arg]


def test_switches_that_are_read_have_their_defaults() -> None:
    """这几个开关是真生效的（各自有行为测试守着）。"""
    from fissue.config import APIConfig, AutoFixConfig, PlatformConfig, ScheduleConfig

    assert AutoFixConfig().allow_file_write is True
    assert ScheduleConfig().enabled is True
    assert APIConfig().enabled is True
    assert PlatformConfig().max_retries == 4


@pytest.mark.parametrize("value", ["report_manual", "discard"])
def test_on_failure_accepts_documented_values(tmp_path: Path, value: str) -> None:
    """两个「真的实现了」的取值都要能配上。"""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"auto_fix": {"on_failure": value}}), encoding="utf-8")

    assert load_settings(cfg).auto_fix.on_failure == value


@pytest.mark.parametrize("value", ["retry_then_report", "retry", "report-manual", ""])
def test_on_failure_rejects_undocumented_values(tmp_path: Path, value: str) -> None:
    """写错的值必须报错，而不是被**静默**当成 report_manual。

    `retry_then_report` 这个取值曾经存在，但它与 `report_manual` 行为完全一致
    （只写人工报告、**不重试**）——名字承诺了重试而代码没做，属于「配置说了没
    做到」，已移除。原先没有任何校验，任何拼错的值都会被静默吞掉。
    """
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"auto_fix": {"on_failure": value}}), encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_settings(cfg)
    assert "on_failure" in str(exc.value), str(exc.value)
