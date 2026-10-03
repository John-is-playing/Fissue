"""配置加载：``.env``（密钥）+ ``config.yaml``（业务配置）。

设计要点
--------
* 环境变量优先级高于 config.yaml（便于 CI / 容器覆盖）。
* 敏感项（LLM key、平台 token、数据库密码）只从环境变量读。
* 所有下游模块统一接收 :class:`Settings`，不各自读文件。

用法::

    from fissue.config import load_settings
    settings = load_settings()              # 默认读 ./config.yaml + ./.env
    settings = load_settings("other.yaml")
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator, model_validator

from .errors import ConfigError
from .logging_setup import setup_logging
from .models import Platform

# 环境变量前缀
ENV_PREFIX = "FISSUE_"

DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_ENV_FILE = ".env"


# ---------------------------------------------------------------------------
# 子配置模型
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    name: str = "fissue"
    log_level: str = "info"
    data_dir: str = "./data"
    timezone: str = "Asia/Shanghai"


class DatabaseConfig(BaseModel):
    url: str = "postgresql+psycopg://fissue:fissue@localhost:5432/fissue"
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10


class BudgetConfig(BaseModel):
    daily_tokens_per_repo: int = 2_000_000
    daily_usd_per_repo: float = 5.0
    hard_stop: bool = True


class PricingConfig(BaseModel):
    """单位：美元 / 1K token。"""

    input_per_1k: float = 0.00014
    output_per_1k: float = 0.00028

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1000.0 * self.input_per_1k
            + completion_tokens / 1000.0 * self.output_per_1k
        )


class LLMConfig(BaseModel):
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    fallback_model: str | None = None
    api_key: str = Field(default="", repr=False)
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout_seconds: int = 120
    max_retries: int = 4
    concurrency: int = 4
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


class RepoConfig(BaseModel):
    platform: Platform
    owner: str
    name: str
    base_branch: str | None = None
    api_base: str | None = None            # GitLab 自建实例等
    enabled: bool = True
    collect: list[str] = Field(default_factory=lambda: ["issue", "pr"])
    labels_include: list[str] = Field(default_factory=list)
    labels_exclude: list[str] = Field(default_factory=list)
    assignee: str | None = None
    since_days: int = 30
    test_hint: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        return f"{self.platform.value}:{self.slug}"


class ClassificationConfig(BaseModel):
    use_keywords_first: bool = True
    bug_keywords: list[str] = Field(
        default_factory=lambda: [
            "bug", "error", "crash", "fail", "exception", "regression",
            "报错", "崩溃", "异常", "失败", "修复", "无法",
        ]
    )
    feature_keywords: list[str] = Field(
        default_factory=lambda: [
            "feature", "enhancement", "proposal", "support", "request",
            "建议", "新增", "支持", "需求", "优化",
        ]
    )
    fallback_to_llm: bool = True


class Thresholds(BaseModel):
    importance_high: int = 70
    difficulty_low: int = 40
    authenticity_min: int = 40
    alert_importance: int = 80


class EvaluationConfig(BaseModel):
    dimensions: list[str] = Field(
        default_factory=lambda: ["authenticity", "importance", "feasibility", "pr_quality"]
    )
    score_range: tuple[int, int] = (0, 100)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    anti_spam: bool = True

    @field_validator("score_range", mode="before")
    @classmethod
    def _as_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v


class VerifierConfig(BaseModel):
    mode: str = "hybrid"                   # executable | checklist | hybrid
    require_f2p: bool = True
    max_rounds: int = 3
    allow_checklist_fallback: bool = True


class SandboxForwarderConfig(BaseModel):
    enabled: bool = True
    socket: str = "/var/run/fissue-runner.sock"
    protocol: str = "unix"


class SandboxMountConfig(BaseModel):
    type: str = "volume"                   # volume | loopback
    size_mb: int = 2048


class SandboxLimits(BaseModel):
    cpus: float = 2.0
    memory_mb: int = 2048
    pids: int = 256
    timeout_seconds: int = 600


class SandboxConfig(BaseModel):
    enabled: bool = True
    image: str = "fissue/sandbox-base:latest"
    runtime: str = "docker"                # docker | local
    network: str = "none"
    read_only_root: bool = True
    user: str = "1000:1000"
    mounts: SandboxMountConfig = Field(default_factory=SandboxMountConfig)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    forwarder: SandboxForwarderConfig = Field(default_factory=SandboxForwarderConfig)
    keep_artifacts: bool = True


class QueueSpec(BaseModel):
    flush_size: int = 10
    idle_flush_seconds: int = 300


class QueuesConfig(BaseModel):
    verify_queue: QueueSpec = Field(default_factory=lambda: QueueSpec(flush_size=10, idle_flush_seconds=300))
    fix_queue: QueueSpec = Field(default_factory=lambda: QueueSpec(flush_size=10, idle_flush_seconds=300))
    manual_flush_enabled: bool = True
    max_attempts: int = 3


class PRStrategy(BaseModel):
    mode: str = "fork"                     # fork | direct | patch_only
    branch_prefix: str = "fissue/fix-"
    label: str = "ai-generated"
    extra_labels: list[str] = Field(default_factory=lambda: ["fissue"])
    draft: bool = False
    auto_submit: bool = True


class AutoFixConfig(BaseModel):
    enabled: bool = True
    agent_max_rounds: int = 12
    allow_file_write: bool = True
    run_tests_in_loop: bool = True
    protected_paths: list[str] = Field(
        default_factory=lambda: [".github/**", ".gitlab/**", "LICENSE", "**/*.lock"]
    )
    max_changed_files: int = 20
    max_diff_lines: int = 800
    on_failure: str = "report_manual"      # report_manual | discard | retry_then_report
    pr_strategy: PRStrategy = Field(default_factory=PRStrategy)


class FixTier(BaseModel):
    max_difficulty: int = 40
    min_importance: int = 0


class FixPolicyConfig(BaseModel):
    tier1: FixTier = Field(default_factory=lambda: FixTier(max_difficulty=40, min_importance=70))
    tier2: FixTier = Field(default_factory=lambda: FixTier(max_difficulty=40, min_importance=0))
    only_issues: bool = True


class ScheduleConfig(BaseModel):
    enabled: bool = True
    incremental: bool = True
    scan_interval_seconds: int = 3600
    queue_flush_interval_seconds: int = 60
    full_rescan_days: int = 30


class NotifyChannel(BaseModel):
    type: str                              # webhook | email | wecom | dingtalk | feishu | console
    enabled: bool = True
    url: str | None = None
    secret: str | None = None
    to: list[str] = Field(default_factory=list)


class NotifyConfig(BaseModel):
    enabled: bool = True
    channels: list[NotifyChannel] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    read_only: bool = False
    allow_repo_management: bool = False


class APIConfig(BaseModel):
    enabled: bool = True
    prefix: str = "/api/v1"
    auth_required: bool = False
    token: str = Field(default="", repr=False)


class ExportConfig(BaseModel):
    default_format: str = "json"
    output_dir: str = "./data/exports"


class PlatformTokens(BaseModel):
    """各平台访问令牌（来自环境变量）。"""

    github: str = Field(default="", repr=False)
    gitee: str = Field(default="", repr=False)
    atomgit: str = Field(default="", repr=False)
    gitlab: str = Field(default="", repr=False)

    def get(self, platform: Platform) -> str:
        return getattr(self, platform.value, "") or ""


# ---------------------------------------------------------------------------
# 总配置
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    """Fissue 全局配置。"""

    app: AppConfig = Field(default_factory=AppConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    repos: list[RepoConfig] = Field(default_factory=list)
    classification: ClassificationConfig = Field(default_factory=ClassificationConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    queues: QueuesConfig = Field(default_factory=QueuesConfig)
    auto_fix: AutoFixConfig = Field(default_factory=AutoFixConfig)
    fix_policy: FixPolicyConfig = Field(default_factory=FixPolicyConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    tokens: PlatformTokens = Field(default_factory=PlatformTokens)

    # 运行期元信息（不写入文件）
    config_path: str | None = None
    env_path: str | None = None

    # -- 便捷方法 ---------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        p = Path(self.app.data_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def repo(self, slug: str, platform: Platform | None = None) -> RepoConfig:
        """按 ``owner/name``（可选平台）取出仓库配置。"""
        matches = [r for r in self.repos if r.slug == slug and (platform is None or r.platform == platform)]
        if not matches:
            raise ConfigError(f"仓库未在配置中登记：{slug}" + (f" (platform={platform.value})" if platform else ""))
        return matches[0]

    def enabled_repos(self) -> list[RepoConfig]:
        return [r for r in self.repos if r.enabled]

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not self.repos:
            # 空仓库列表是合法的（允许只用 CLI 临时指定仓库），不报错。
            pass
        for r in self.repos:
            if "/" in r.name:
                raise ConfigError(f"repos[].name 不应包含 '/'：{r.name}（owner 与 name 请分开写）")
        if self.sandbox.runtime == "local" and self.sandbox.enabled:
            # 允许但提醒：本地模式隔离弱
            import logging

            logging.getLogger(__name__).warning(
                "sandbox.runtime=local：隔离性弱，仅建议本机开发使用"
            )
        return self


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = dotenv_values(path)
    return {k: (v or "") for k, v in values.items() if k}


def _apply_env_overrides(data: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """把 FISSUE_* 环境变量映射回配置字段。

    优先级：**进程环境变量 > .env 文件 > config.yaml > 内置默认值**。

    为什么进程环境优先？容器 / CI / 临时调试时用
    ``FISSUE_LLM_MODEL=xxx fissue eval`` 覆盖一次最符合直觉；
    而 .env 是「长期默认值」，不该压住当场显式指定的值。
    """
    merged = dict(data)

    def env_of(name: str) -> str | None:
        key = ENV_PREFIX + name
        value = os.environ.get(key) or env.get(key)
        return value if value else None

    def port_of(name: str) -> int | None:
        """解析端口等整数项；非法值给出可操作的报错而不是裸 ValueError。"""
        raw = env_of(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(
                f"环境变量 {ENV_PREFIX}{name} 必须是整数，当前为 {raw!r}"
            ) from exc

    # 数据库
    if url := env_of("DATABASE_URL"):
        merged.setdefault("database", {})["url"] = url

    # LLM
    llm = merged.setdefault("llm", {})
    if v := env_of("LLM_API_KEY"):
        llm["api_key"] = v
    if v := env_of("LLM_BASE_URL"):
        llm["base_url"] = v
    if v := env_of("LLM_MODEL"):
        llm["model"] = v
    if v := env_of("LLM_FALLBACK_MODEL"):
        llm["fallback_model"] = v

    # 平台 token
    tokens = merged.setdefault("tokens", {})
    for plat in ("GITHUB", "GITEE", "ATOMGIT", "GITLAB"):
        if v := env_of(plat + "_TOKEN"):
            tokens[plat.lower()] = v

    # 日志级别
    if v := env_of("LOG_LEVEL"):
        merged.setdefault("app", {})["log_level"] = v

    # 沙盒转发组件 socket
    if v := env_of("SANDBOX_RUNNER_SOCKET"):
        merged.setdefault("sandbox", {}).setdefault("forwarder", {})["socket"] = v

    # Web / API
    web = merged.setdefault("web", {})
    if v := env_of("WEB_HOST"):
        web["host"] = v
    if (port := port_of("WEB_PORT")) is not None:
        web["port"] = port
    api = merged.setdefault("api", {})
    if v := env_of("API_TOKEN"):
        api["token"] = v
        api["auth_required"] = True

    return merged


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    require_llm_key: bool = False,
) -> Settings:
    """加载配置。

    :param config_path: config.yaml 路径；默认取 ``FISSUE_CONFIG`` 或 ``./config.yaml``，
        文件不存在时使用内置默认值（方便首次运行不报错）。
    :param env_path: .env 路径；默认 ``FISSUE_ENV`` 或 ``./.env``。
    :param overrides: 最高优先级的覆盖（测试用）。
    :param require_llm_key: 为 True 时缺少 LLM key 直接报错。
    """
    cfg_path = Path(
        config_path or os.environ.get(ENV_PREFIX + "CONFIG") or DEFAULT_CONFIG_FILE
    ).expanduser()
    env_file = Path(
        env_path or os.environ.get(ENV_PREFIX + "ENV") or DEFAULT_ENV_FILE
    ).expanduser()

    raw: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:  # pragma: no cover
            raise ConfigError(f"config.yaml 解析失败：{exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("config.yaml 顶层必须是映射（key: value）")
        raw = loaded
    elif config_path is not None:
        # 显式指定却不存在 → 报错，避免静默用错配置
        raise ConfigError(f"配置文件不存在：{cfg_path}")

    env_values = _read_env_file(env_file)
    raw = _apply_env_overrides(raw, env_values)
    if overrides:
        raw = _deep_merge(raw, overrides)

    try:
        settings = Settings(**raw)
    except Exception as exc:
        raise ConfigError(f"配置校验失败：{exc}") from exc

    settings.config_path = str(cfg_path) if cfg_path.exists() else None
    settings.env_path = str(env_file) if env_file.exists() else None

    setup_logging(settings.app.log_level)

    if require_llm_key and not settings.llm.api_key:
        raise ConfigError(
            "缺少 LLM API Key：请在 .env 中设置 FISSUE_LLM_API_KEY（或 config.yaml 的 llm.api_key）"
        )

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级缓存的配置（CLI / Web 复用）。"""
    return load_settings()


def reset_cache() -> None:
    """清缓存（测试用）。"""
    get_settings.cache_clear()
