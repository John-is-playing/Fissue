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
    DimensionScore,
    Evaluation,
    ItemStatus,
    ItemType,
    Platform,
    Priority,
    QueueName,
    RawItem,
    RepoRef,
    Scores,
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
