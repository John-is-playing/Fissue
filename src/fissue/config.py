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
    #: 单次补全的 token 上限。这只是**上限**，不是预留——实际计费按真实用量，
    #: 调高不会产生闲置成本。推理模型的 reasoning_content 会吃掉大量预算
    #: （实测同一提示的推理长度在 14207~17264 字符间波动），8192 余量偏紧。
    max_tokens: int = 16384
    #: 被长度截断后自动加码的上限（见 LLMClient.chat_json）。
    #: 截断时翻倍重试，但不超过此上限，避免单次调用失控。
    max_tokens_ceiling: int = 32768
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
    # -- verify 命令的执行调度（Q7）----------------------------------------
    # 单条验证的耗时几乎全在「LLM 生成验证器」上（实测数万字符推理，
    # 单次数十秒到数分钟），沙盒执行只占 1~2 秒。条目之间彼此独立
    # （各自克隆工作区、各自生成验证器），所以并发能近似线性压缩墙钟时间。
    # 上限受沙盒并发与 LLM 限流约束，默认与 llm.concurrency 对齐。
    concurrency: int = 4
    #: 单条因**环境错误**（克隆失败 / 网络抖动 / LLM 服务错误）失败后的重试次数。
    #: 重跑一条要从头走「克隆 → 生成验证器 → 沙盒」，代价很高；
    #: 验证器自身写错由 runner 内部的修正回路处理，不走这里。
    item_retries: int = 2
    # -- 既有测试回归门（见 docs/REGRESSION-GATE.md）------------------------
    # off | warn | strict。默认 strict：这是本项目对外的招牌主张——「修好目标
    # 却弄坏别处」的补丁不该被自动提 PR，默认只提醒等于招牌不生效。
    #
    # 原先默认 warn 是担心「本就失败/跳过的老仓库被大面积误杀」，但那个顾虑
    # 已经由 judge_regression 的安全底线挡住了：base 阶段测试不绿、跑不通或
    # 超时，一律**放行**（本门不可信，不据此拒绝）。所以 strict 只在
    # 「改之前是绿的、改完之后红了」这一种情形下拦人，那正是它该拦的。
    regression_gate: str = "strict"
    regression_command: str | None = None  # 留空则取仓库级 test_hint，再探测
    regression_timeout_seconds: int = 900  # 独立预算，通常远大于单测验证器

    @field_validator("regression_gate")
    @classmethod
    def _check_regression_gate(cls, v: str) -> str:
        allowed = {"off", "warn", "strict"}
        if v not in allowed:
            raise ConfigError(
                f"verifier.regression_gate 只接受 off|warn|strict，当前为 {v!r}"
            )
        return v


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


#: 本地执行时默认放行的环境变量。宿主环境里带着 LLM key 与各平台 token，
#: 若把整份 ``os.environ`` 透传给模型生成的验证器与不可信 PR 代码，等于直接
#: 交出这些密钥（本地执行没有容器隔离，被执行的代码能任意组包外发）。
#: 这里只留「把命令跑起来」所必需的几项。
DEFAULT_LOCAL_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",                    # 找到 python / git 等可执行文件
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",              # Windows 必需
    "PATHEXT",
    "COMSPEC",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
)


class SandboxConfig(BaseModel):
    enabled: bool = True
    image: str = "fissue/sandbox-base:latest"
    runtime: str = "docker"                # docker | local
    #: Docker 不可用时是否允许降级为**无隔离**的本地执行。默认关闭。
    #: 降级会绕过全部容器隔离（禁网、只读根、非 root、CPU/内存/PID 限额），
    #: 被执行的却是模型生成的验证器与不可信 PR 代码——静默降级等于把
    #: SECURITY.md 的隔离承诺整套架空，因此必须由用户显式开启。
    allow_local_fallback: bool = False
    #: 本地执行（含显式 ``runtime: local``）放行给子进程的环境变量白名单。
    #: 宿主环境里有 LLM key 与各平台 token，整份 ``os.environ`` 透传等于把
    #: 密钥交给上面那段代码；默认只留跑测试所必需的几项。
    local_env_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_LOCAL_ENV_ALLOWLIST)
    )
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
    # 这里曾有一个 `auto_submit` 开关：文档建议「首次使用设成 false」，但代码
    # 从来没读过它——设了照样自动提 PR，是个会给人虚假安全感的空开关，已删除。
    # 要「只出补丁、不提 PR」请用 `mode: patch_only` 或 `fissue fix --dry-run`。


class AutoFixConfig(BaseModel):
    enabled: bool = True
    agent_max_rounds: int = 12
    #: 连续多少轮「验证器输出完全一致」即判定停滞、提前收工。
    #: 没有这个闸门时，模型会把同一次失败原样重放直到 ``agent_max_rounds``：
    #: demo 实测 pipekit #8 连续 10 轮输出**逐字节相同**、耗时同为 1.4s，
    #: 白烧 10 轮 LLM 调用与沙盒执行，最后还报出误导性的「仍未通过验证器」
    #: （真相是「卡住了」，不是「修了 12 次没修好」）。设 0 关闭该闸门。
    stall_rounds: int = 4
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

    # 优先级判定策略（tier1/tier2/none 怎么算）：
    #   importance —— 默认。以``min_importance``门槛为准；调高 tier1.min_importance
    #                 可让「重要性刚过线」的条目落到 tier2。
    #   dual       —— 难度与重要性各自判档，取**更严**的一档（双阈值）。
    #   custom     —— 由 ``custom_tier`` 指定的用户函数判定；此时各 tier 的
    #                 ``min_importance`` 不再参与计算。
    tier_strategy: str = "importance"
    # 用户判定函数路径，支持 ``pkg.mod:func`` 与 ``pkg.mod.func`` 两种写法。
    # 签名 ``func(difficulty, importance, policy) -> str``，返回值接受
    # ``tier1`` / ``tier2`` / ``none``（大小写不敏感）。
    custom_tier: str | None = None

    @field_validator("tier_strategy")
    @classmethod
    def _check_tier_strategy(cls, v: str) -> str:
        allowed = {"importance", "dual", "custom"}
        if v not in allowed:
            raise ConfigError(
                f"fix_policy.tier_strategy 只接受 importance|dual|custom，当前为 {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _check_custom_tier(self) -> "FixPolicyConfig":
        # custom 模式没有判定函数等于没有策略，配置期直接报错，别等到运行时才发现。
        if self.tier_strategy == "custom" and not (self.custom_tier or "").strip():
            raise ConfigError(
                "fix_policy.tier_strategy=custom 时必须提供 custom_tier"
                "（如 'myproj.policy:tier_of'）"
            )
        return self


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


class PlatformConfig(BaseModel):
    """平台适配器的通用行为（四平台共用）。"""

    #: 仓库标签列表的进程内缓存时长（秒）。打标签是批量行为，缓存能把
    #: 「每条 × 每标签各拉一次标签列表」压成一次；但常驻服务（``fissue serve``）
    #: 长跑时缓存若永不过期，平台侧新建的标签会被一直判为「不存在」——
    #: 表现为持续的 422 创建失败并在 :meth:`ensure_labels` 里直接抛出。
    #: 默认 600s：单次 flush（秒级~分钟级）内仍是 0 次重复拉取，跨轮次又能自愈。
    #: 设为 0 表示不缓存（每次都拉，最保守）。
    label_cache_ttl_seconds: float = 600.0
    #: 打标签的并发上限；留空（None）跟随 ``llm.concurrency``——
    #: 打标签是纯平台 HTTP、不消耗 token，语义上不该被 LLM 并发数绑死。
    label_concurrency: int | None = None
    #: 被限流时**单次最多等多久**（秒）。`Retry-After` 与 `X-RateLimit-Reset`
    #: 是平台给的「建议等多久」，直接照等没有上限——最坏能把整条流水线按住一小时，
    #: 而且日志上只表现为「卡住」。超出上限一律按此值封顶；封顶后仍被限流就报错。
    max_retry_after_seconds: float = 300.0


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
    platforms: PlatformConfig = Field(default_factory=PlatformConfig)
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

    # 标签名缓存时长（秒；0 = 不缓存）
    if v := env_of("LABEL_CACHE_TTL"):
        try:
            merged.setdefault("platforms", {})["label_cache_ttl_seconds"] = float(v)
        except ValueError as exc:
            raise ConfigError(
                f"环境变量 {ENV_PREFIX}LABEL_CACHE_TTL 必须是数字（秒），当前为 {v!r}"
            ) from exc

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
