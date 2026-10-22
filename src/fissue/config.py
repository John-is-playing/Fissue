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

import difflib
import ipaddress
import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator, model_validator

from . import net
from .errors import ConfigError
from .logging_setup import setup_logging
from .models import Platform

# 环境变量前缀
ENV_PREFIX = "FISSUE_"

DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_ENV_FILE = ".env"
#: 覆盖层：WebUI / `fissue onboard` 改配置时**只写这一份**，不碰人手写的 config.yaml。
#: 理由与写入规则见 ``fissue/configio.py`` 的模块说明。
DEFAULT_OVERLAY_FILE = "config.local.yaml"


def overlay_path_for(config_path: str | Path | None) -> Path:
    """覆盖层文件的位置：默认与 config.yaml 同目录、同名换成 ``config.local.yaml``。

    ``FISSUE_CONFIG_LOCAL`` 可以整体改掉它（容器 / 多环境部署时把覆盖层放到别处）。
    这个读取**刻意留在 config.py**：``tests/test_env.py`` 会用正则扫 config.py 与 net.py 来
    核对「.env.example 里承诺的每个变量都真的被读」，写在别的模块里它就看不见。
    """
    explicit = os.environ.get(ENV_PREFIX + "CONFIG_LOCAL")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(config_path).expanduser() if config_path is not None else Path(DEFAULT_CONFIG_FILE)
    return base.with_name(DEFAULT_OVERLAY_FILE)


# ---------------------------------------------------------------------------
# 子配置模型
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    log_level: str = "info"
    data_dir: str = "./data"


class DatabaseConfig(BaseModel):
    #: 默认走 **SQLite**（零依赖、随包可用，且是开发期与测试真正跑过的那条路）。
    #: PostgreSQL 仍是**生产/多机**的推荐选择，但它是显式 opt-in——见 `_check_url` 与
    #: `docs/ENV.md`：默认值不该是一个「从未真跑过」的后端（`BENCHMARK.md` §5 记录过
    #: PostgreSQL 后端未在真跑里用过）。文件落在 `./data/` 下，目录由 store/db.py 建。
    url: str = "sqlite:///./data/fissue.db"
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        """连接串必须真的能用来建引擎——在**加载期**就挡住，而不是等到写盘之后。

        为什么落在这一层（而不是等 `create_engine`）：设置页 / 初始化向导是「**先写盘、后热重载**」，
        而写盘前那道校验只构造 `Settings`——那时不碰数据库。于是写坏一个连接串会一路过关：
        写进 .env → 重载时 500 → 此后每次保存配置都 500、重启也起不来；而**服务起不来就没有界面**，
        用户只能回到机器上改文件。`web/setup.py::_validate_candidate` 的 docstring 里写的正是
        「别留下一个重启起不来的 .env」。

        放在这里，三条入口（config.yaml / .env / 向导）一律在加载期被同一条规则挡住。
        """
        text = (value or "").strip()
        if not text:
            raise ConfigError("database.url 不能为空")
        if "•" in text:
            # 实测踩到过：用户把页面上显示的**脱敏值**（`sqli••••••e.db`）当成真实连接串填了回去。
            # 单靠「解析失败」提示他，他很可能再看一眼页面、又把脱敏值抄回来一次。
            raise ConfigError(
                "database.url 看起来是**页面上显示的脱敏值**（含 •）——那是用来确认「填的是哪一条」的，"
                "不是真实连接串。请填真实值，例如 "
                "sqlite:////绝对路径/xxx.db 或 postgresql+psycopg://user:pw@host:5432/dbname"
            )
        # 延迟导入：config 是热点路径（每条命令都走），没必要在模块级拉起 sqlalchemy。
        from sqlalchemy.engine import make_url

        try:
            parsed = make_url(text)
        except Exception as exc:
            raise ConfigError(
                f"database.url 不是合法的连接串：{exc}。"
                "常用两种写法：SQLite 用 sqlite:////绝对路径/文件.db（注意**四个斜杠**，"
                "写成 sqlite:///home/... 会把 /home/... 当成相对路径）；"
                "PostgreSQL 用 postgresql+psycopg://user:password@host:5432/dbname"
            ) from exc
        try:
            parsed.get_dialect()
        except Exception as exc:
            # 驱动名写错（`postgresql+psycopgg`）或该驱动没装，同样是「一路写到 .env、重启才炸」。
            raise ConfigError(
                f"database.url 的驱动不可用：{parsed.drivername!r}（{exc}）。"
                "本项目随包提供 PostgreSQL（postgresql+psycopg）与 SQLite（sqlite）两种。"
            ) from exc
        return text


class BudgetConfig(BaseModel):
    """按**单仓库**每日累计的预算闸门，全部可在 WebUI 改。

    ``0`` = **关掉那一维度的限制**（代码里是 `budget.daily_usd_per_repo and …` 的写法）——
    这是有意的出口，所以约束是「非负」而不是「正」：负预算只会让判定永远成立、把每一轮
    都掐断，属于纯粹的误填。
    """

    daily_tokens_per_repo: int = Field(default=2_000_000, ge=0)
    daily_usd_per_repo: float = Field(default=5.0, ge=0)
    hard_stop: bool = True


class PricingConfig(BaseModel):
    """单位：美元 / 1K token。

    这两个数**直接决定** ``cost_usd`` 与预算判定：填错（或与实际单价不符）会让 ``hard_stop``
    要么提前掐断、要么形同虚设。所以它必须能在 WebUI 里改——原先只能改 config.yaml。
    """

    input_per_1k: float = Field(default=0.00014, ge=0)
    output_per_1k: float = Field(default=0.00028, ge=0)

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
    temperature: float = Field(default=0.2, ge=0, le=2)
    #: 单次补全的 token 上限。这只是**上限**，不是预留——实际计费按真实用量，
    #: 调高不会产生闲置成本。推理模型的 reasoning_content 会吃掉大量预算
    #: （实测同一提示的推理长度在 14207~17264 字符间波动），8192 余量偏紧。
    max_tokens: int = Field(default=16384, ge=1)
    #: 被长度截断后自动加码的上限（见 LLMClient.chat_json）。
    #: 截断时翻倍重试，但不超过此上限，避免单次调用失控。
    max_tokens_ceiling: int = Field(default=32768, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)
    max_retries: int = Field(default=4, ge=0)
    concurrency: int = Field(default=4, ge=1)
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
    """评测行为。

    这里有两处**刻意不可配置**的口径（原先有对应的配置键，但代码从未读取，
    已于 P2 清理时删除，免得给人「可以调」的错觉）：

    * 四个维度（真实性 / 重要性 / 可行性 / PR 质量）与 0~100 的评分区间硬编码在
      ``ai/evaluator.py``；
    * 反刷子（重复 / AI 生成 / 刷量）恒开——它是本项目「别信模型自述」的核心主张，
      不是一个可以关掉的选项。
    """

    thresholds: Thresholds = Field(default_factory=Thresholds)


class VerifierConfig(BaseModel):
    mode: str = "hybrid"                   # executable | checklist | hybrid
    require_f2p: bool = True
    max_rounds: int = 3
    allow_checklist_fallback: bool = True
    #: 强制修复（force_fix）条目的清单判定是否改用「强制性提示词」：
    #: 明说人已拍板要修，禁止用「无法验证 / 证据不足」回避，证据确实不足时
    #: 必须点名缺哪个文件、哪段代码——否则判定会退化成「拒绝执行」。
    checklist_force_prompt: bool = True
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
    #: 只能用这两个值。原先这里是裸 `str`，而 `DockerRuntime.run` 的判定是
    #: `if runtime == "docker"`、否则落到本地执行——于是 `Docker`（大小写）、
    #: `podman`、带尾空格之类**任何非精确值都会被静默当成「本机执行」**，
    #: 绕开 `allow_local_fallback` 这道显式开关，把 SECURITY.md 的隔离承诺
    #: 整套架空（模型生成的验证器 + 不可信 PR 代码，无容器、不禁网）。
    #: 现在拼错在加载配置时就被拒绝；`sandbox.runtime` 侧另有一道 fail-closed。
    runtime: Literal["docker", "local"] = "docker"
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
    #: 容器里跑命令用的 ``uid:gid``。**留空 = 跟随宿主**（见
    #: ``sandbox.runtime.default_container_user``）。
    #:
    #: 不能写死 ``1000:1000``：工作区是宿主 uid 的 ``mkdtemp``（0700），bind mount 会把属主
    #: 与权限一起带进容器；宿主 uid 不是 1000 时容器写不进 ``/workspace``，整条验证链路会以
    #: ``Permission denied`` 收场，而日志看上去像代码或验证器的问题。
    user: str = ""
    #: 把工作区里的 `.git` 以**只读**挂进容器（`index` 除外，见下）。默认开启。
    #:
    #: 容器对工作区可写，而 `.git` 是**宿主的执行入口**：hooks、`config` 里的
    #: `filter.*.clean`（`git add` 时执行）、`diff.*.textconv`（`git diff` 时执行）、
    #: `credential.helper`，以及 `objects`/`refs`（能伪造提交与引用，把内容混进面向上游的
    #: 补丁里）。宿主侧另有几道软闸门（`RepoWorkspace.harden_git`），这一条是从**挂载层**
    #: 直接掐掉——写不进去，就没有东西可执行、也没有东西可伪造。
    #:
    #: `.git/index` 单独留成可写：不少仓库的测试套件会顺手跑 `git status`/`git diff`，
    #: 它们要刷新索引；整块只读会让这类套件直接报 `Read-only file system`。索引被改也无害：
    #: 宿主提交前会 `reset_index()` 拉回 HEAD，再按白名单重新 stage。
    #:
    #: 确需容器写 git（例如测试套件要 `git commit`）时关掉它，退回「只有宿主侧软闸门」。
    git_readonly: bool = True
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
    # 这里曾有 `max_attempts`，但代码从未读取它（重试次数是 `Repository.claim_next`
    # 自己的方法参数，且该方法在生产代码里没有调用者），已删除。


class PRStrategy(BaseModel):
    mode: str = "fork"                     # fork | direct | patch_only
    branch_prefix: str = "fissue/fix-"
    label: str = "ai-generated"
    extra_labels: list[str] = Field(default_factory=lambda: ["fissue"])
    draft: bool = False
    # 这里曾有一个 `auto_submit` 开关：文档建议「首次使用设成 false」，但代码
    # 从来没读过它——设了照样自动提 PR，是个会给人虚假安全感的空开关，已删除。
    # 要「只出补丁、不提 PR」请用 `mode: patch_only` 或 `fissue fix --dry-run`。

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        """只接受**代码真的实现了**的三种取值。

        这条校验是安全相关的：`fixer.autofix` 里只有**精确等于** `fork` 才走 fork、
        `patch_only` 走只落补丁，其余一律落到 `head_repo = 上游仓库`——也就是「把分支直推到
        别人的仓库」。于是 `Fork` / `frok` / `direct ` 这类拼写错误会**静默**关掉 fork 这道
        安全阀（对比：fork 真失败时代码是明确停手、不降级的）。
        """
        allowed = {"fork", "direct", "patch_only"}
        if v not in allowed:
            raise ConfigError(
                f"auto_fix.pr_strategy.mode 只接受 fork|direct|patch_only，当前为 {v!r}"
            )
        return v


class AutoFixConfig(BaseModel):
    enabled: bool = True
    agent_max_rounds: int = 12
    #: 连续多少轮「验证器输出完全一致」即判定停滞、提前收工。
    #: 没有这个闸门时，模型会把同一次失败原样重放直到 ``agent_max_rounds``：
    #: demo 实测 pipekit #8 连续 10 轮输出**逐字节相同**、耗时同为 1.4s，
    #: 白烧 10 轮 LLM 调用与沙盒执行，最后还报出误导性的「仍未通过验证器」
    #: （真相是「卡住了」，不是「修了 12 次没修好」）。设 0 关闭该闸门。
    stall_rounds: int = 4
    #: 回归门（`regression:fix`）失败后，最多**带着失败反馈重跑几次修复**（0 = 关闭，即旧行为）。
    #:
    #: 为什么需要：验证器只覆盖这条 issue 自己的行为，模型完全可能「修好了它、却改坏了别处」。
    #: 实测（Fissue #1）：一次修复让自写验证器通过，却破坏了 `parse_event` 的既有契约（既有
    #: 用例当场失败），回归门拦下后直接收工——而**失败信息本身就是最好的下一步提示**（哪条
    #: 既有用例被改坏了），喂回去通常一两轮就能改对。
    regression_retries: int = 1
    #: 连续多少轮**一个文件都没改**就收工（0/1 = 关闭）。
    #:
    #: 为什么要与 `stall_rounds` 分开：那条判据把「读到**新**文件」也算进展，而出于不误杀
    #: 「跨模块定位根因」的正当探查，这个宽松是**故意**的。可真跑（Fissue #1）暴露了它的漏洞：
    #: 9 轮只读不写里有 5 次「读到新文件」，每次都把空转计数**整体清零**，于是它拖到第 9 轮才
    #: 累计到阈值。编辑者一直不动笔，这件事本身就已经说明问题了——所以另立一个**只被 write
    #: 清零**的计数器。
    no_write_rounds: int = 6
    #: 写入时对 Python 文件做语法自检（默认开）。写出来的内容解析不了就**拒绝该文件**并把理由
    #: 回给模型。
    #:
    #: 为什么需要：实测（Fissue #1）模型把 `src/fissue/web/hooks.py` 整个换成了 `/*placeholder*/`，
    #: 于是 `app.py` 导入即 SyntaxError、验证器 5 次全败、12 轮耗尽——而那次 diff 只有 300 行删除，
    #: 现有的锁定/保护路径/大小/文件数/diff 行数全都没拦住。与其枚举「占位符长什么样」，不如守住
    #: 底线：**Python 文件必须先是 Python**。真有仓库需要故意写非法 Python（比如夹具）时可关掉。
    syntax_check: bool = True
    #: 「评审 → 返修」的轮数（默认 1；0 = 关闭评审步骤）。
    #:
    #: 为什么要有评审：F2P 与回归门挡得住「弄坏别处」，**挡不住「写得脏」**——实测（Fissue #1）
    #: 那份补丁三关全过，却把投递去重写成了**模块级全局** + 靠调用顺序隐式传值，只有人眼能看出来。
    #: 评审因此不看测试结果、只看**改动本身**（diff + 改动后的文件），并把意见交回给修复 Agent。
    review_rounds: int = 1
    #: 评审用哪个模型；留空 = 用全局 `llm.model`。
    #:
    #: 建议配一个**不同**的模型：写和审不是同一个模型时，不容易犯同一个盲点（第二意见）。
    review_model: str = ""
    #: 克隆工作区的重试次数（0 = 不重试）。
    #:
    #: 克隆是整条修复链路上唯一「纯网络」的一步，也最容易因网关抖动 / 中间人抽风
    #: 而失败：实测 envkit #1 一次 `GnuTLS recv error (-110)` 抖动就把条目打成
    #: ``failed``，而 ``fix_candidates`` 只取 ``fix_queued``——它从此再也不会被修。
    #: verify 侧早有 ``verifier.item_retries`` 应对「克隆/网络抖动」，这里补齐同一
    #: 处理；但**只重试克隆这一步**：整条 Agent 循环重跑一次要烧十几轮 LLM，
    #: 那个代价不该由一次网络抖动来决定。
    clone_retries: int = 2
    #: 是否允许 Agent 改动仓库文件。设 false 时只出补丁不改工作区（见
    #: ``fixer.agent._apply_writes``）。注意这不是安全边界——真正的边界是
    #: 下面的 ``protected_paths`` 与沙盒隔离。
    allow_file_write: bool = True
    protected_paths: list[str] = Field(
        default_factory=lambda: [".github/**", ".gitlab/**", "LICENSE", "**/*.lock"]
    )
    max_changed_files: int = 20
    max_diff_lines: int = 800
    on_failure: str = "report_manual"      # report_manual | discard
    pr_strategy: PRStrategy = Field(default_factory=PRStrategy)

    @field_validator("on_failure")
    @classmethod
    def _check_on_failure(cls, v: str) -> str:
        """只接受代码**真的实现了**的取值。

        这里曾有第三个取值 ``retry_then_report``，但它与 ``report_manual`` 行为
        完全一致（都是只写人工报告、**不重试**）——名字承诺了重试而代码没做，属于
        「配置说了没做到」。与其留一个说谎的选项，不如删掉它。

        顺带把另一个坑也堵上：原先没有任何校验，写错的值会被**静默**当成
        ``report_manual``（照 ``verifier.regression_gate`` 的做法加校验）。
        """
        allowed = {"report_manual", "discard"}
        if v not in allowed:
            raise ConfigError(
                f"auto_fix.on_failure 只接受 report_manual|discard，当前为 {v!r}"
                "（retry_then_report 已移除：它与 report_manual 行为一致，并不会重试）"
            )
        return v


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
    def _check_custom_tier(self) -> FixPolicyConfig:
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

    #: 常驻服务每一轮是否做「验证」（生成验证器 + 跑 F2P）这一步。
    #:
    #: 关掉它 = 常驻服务只做 抓取 → 评测 → flush → 修复，验证留给人工 `fissue verify`
    #: （或调 API）。适合「验证要花很多钱/很慢，我想手动挑着跑」的用法。
    verify_enabled: bool = True

    #: 常驻服务**每轮**最多验证多少条。
    #:
    #: 验证是一轮里最慢的一步（每条要 LLM 生成验证器 + 数次沙盒执行），不设上限会把单轮
    #: 拉得极长、日志也刷得看不清。积压不会因此丢：后面的轮次会继续消化。
    #: 想「关掉这一步」请用上面的 `verify_enabled`，而不是把这里设成 0。
    verify_batch_limit: int = Field(default=30, ge=1)


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


class WebAuthConfig(BaseModel):
    """会话鉴权开关（docs/AUTH.md §6）。

    这是**唯一**的鉴权开关。此前有一个 `api.auth_required` 布尔量，它与
    `FISSUE_API_TOKEN` 是否设置隐式联动，结果同一件事有两个真相来源
    （设了口令却把 auth_required 写成 false、或反过来）。现在只有一个：
    要么开着会话鉴权，要么声明可信网段，要么绑回环。
    """

    #: 是否启用会话鉴权。关掉 = 只靠「回环 / 可信网段」放行。
    #:
    #: 默认 **true**：升级后你原来那条「对外可达却没鉴权」的启动报错不会再出现，
    #: 因为默认就已经声明了机制。
    enabled: bool = True
    #: 会话有效期（分钟）。
    session_ttl_minutes: int = Field(default=480, ge=1)
    #: 反向代理已提供 TLS 时置 true —— 给会话 cookie 加 `Secure`。
    secure_cookie: bool = False
    #: 触发退避锁定前允许的密码失败次数。
    login_max_failures: int = Field(default=5, ge=1)


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    read_only: bool = False
    #: 会话鉴权（登录 / 2FA / API Key）。
    auth: WebAuthConfig = Field(default_factory=WebAuthConfig)
    #: **可信网段**：落在这些 CIDR 里的来源视为等同回环——免登录、可写配置。
    #:
    #: 满足「内网直接用浏览器访问」这个诉求，且不用把口令写进浏览器。
    #: 判定只看**真实对端地址**（`request.client.host`），**不信任** `X-Forwarded-For`：
    #: 转发头本身可以被伪造，而「相信转发头」等于把判定权交给任何能构造请求的人。
    trusted_networks: list[str] = Field(default_factory=list)
    #: 「配置写入」类操作（初始化向导 / 仓库管理 / 设置页）的额外边界。
    #:
    #: 正常路径是：来源在回环/可信网内，或请求已通过会话/API Key 认证。
    #: 打开它 = 「我明白风险，强制允许**未经认证**的非本机来源改写配置」——给
    #: 「内网无口令自用」这种部署留的出口，页面上以显式开关呈现并带警告，而不是悄悄放行。
    #:
    #: 与 `read_only` 的分工：`read_only` 管的是「要不要去平台上打标签/提 PR」，
    #: 这个开关管的是「能不能改本机的配置文件」。两件不同的事，别互相借用语义。
    allow_remote_config_write: bool = False
    # 这里曾有 `allow_repo_management`（文档说它控制「仓库与密钥管理界面」），
    # 但那个界面从未实现、背后没有任何路由，属于「配置写了不生效」，已删除。
    # 真正的写操作闸门是上面的 `read_only`（见 web/app.py 的 _require_writable）。

    @field_validator("trusted_networks")
    @classmethod
    def _check_trusted_networks(cls, value: list[str]) -> list[str]:
        """网段必须是**带前缀长度**的合法 CIDR，并在加载期就报错。

        这条校验放在这里而不是请求期：写错一个网段如果被静默忽略，用户会在
        「我以为配了可信网」的错觉里把控制面暴露出去。宁可启动就失败，并说清哪一条不对。

        为什么裸地址（``192.168.1.0``）也要拒：``ip_network`` 会把它当成 ``/32``——
        用户想圈一个网段，实际只放行了一台机器，而**任何地方都不会提示他**。
        要单机就写 ``192.168.1.5/32``，要把意图写出来。
        """
        cleaned: list[str] = []
        for raw in value:
            text = str(raw).strip()
            if not text:
                continue
            if "/" not in text:
                raise ConfigError(
                    f"web.trusted_networks 里的条目缺少前缀长度：{text!r}。"
                    "请写 CIDR 形式——网段用 192.168.1.0/24，单机用 192.168.1.5/32。"
                )
            try:
                ipaddress.ip_network(text, strict=False)
            except ValueError as exc:
                raise ConfigError(
                    f"web.trusted_networks 里的网段不合法：{text!r}（{exc}）。"
                    "请写 CIDR 形式，例如 192.168.1.0/24 或 10.0.0.0/8。"
                ) from exc
            cleaned.append(text)
        return cleaned

    def network_of(self, host: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
        """``host`` 落在哪个可信网段里（不在任何网段则 None）。"""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return None
        for item in self.trusted_networks:
            try:
                net = ipaddress.ip_network(item, strict=False)
            except ValueError:  # pragma: no cover - 构造期已校验过
                continue
            if addr.version == net.version and addr in net:
                return net
        return None


class APIConfig(BaseModel):
    """REST 前缀与开关。

    这里**曾有** ``auth_required`` 与 ``token``（单一全局 Bearer 口令）。它们已被
    `web.auth` + 会话/API Key 整体替换（docs/AUTH.md §2 决策 3）——不再是两套机制并存。
    旧口令可被一次性导入为一条 API Key，见 `fissue.web.auth.migrate_legacy_token`。
    """

    enabled: bool = True
    prefix: str = "/api/v1"


class ExportConfig(BaseModel):
    # 这里曾有 `default_format`，但代码从未读取它（`fissue report/export` 的格式
    # 由命令行 `--format` 决定），已删除。
    output_dir: str = "./data/exports"


class PlatformTokens(BaseModel):
    """各平台访问令牌（来自环境变量）。"""

    github: str = Field(default="", repr=False)
    gitee: str = Field(default="", repr=False)
    atomgit: str = Field(default="", repr=False)
    gitlab: str = Field(default="", repr=False)

    def get(self, platform: Platform) -> str:
        return getattr(self, platform.value, "") or ""


class WebhookSecrets(BaseModel):
    """各平台 webhook 的**签名密钥**（与 API token 是两回事，别混用）。"""

    github: str = Field(default="", repr=False)
    gitee: str = Field(default="", repr=False)
    atomgit: str = Field(default="", repr=False)
    gitlab: str = Field(default="", repr=False)

    def get(self, platform: Platform) -> str:
        return getattr(self, platform.value, "") or ""


class WebhookConfig(BaseModel):
    """入站 webhook：让平台把「新 Issue / 新 PR」推过来，省掉一个轮询间隔。

    默认**关闭**。开启后某个平台**没配密钥就一律 403**（fail-closed）——这个端点天然带不了
    bearer 令牌（平台不会带），所以**签名/密钥就是它的身份证明**；而且只有 `repos` 里登记且
    启用的仓库才会被接受，其余事件一律忽略（返回 202 并写明原因）。

    各平台的校验方式：GitHub 用 HMAC-SHA256（``X-Hub-Signature-256``）；GitLab / Gitee /
    AtomGit 用共享密钥头（``X-Gitlab-Token`` / ``X-Gitee-Token``），一律**常数时间**比对。
    细节见 ``web/hooks.py``。
    """

    enabled: bool = False
    secrets: WebhookSecrets = Field(default_factory=WebhookSecrets)


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
    #: 平台 HTTP 的重试次数。此前是直接借用 ``llm.max_retries``——于是「把 LLM 的
    #: 重试调成 0」会顺手关掉平台重试，「调大 LLM 重试」又会放大写请求的风险，
    #: 两个不相干的旋钮被绑在了一个数上。
    max_retries: int = 4


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
    webhooks: WebhookConfig = Field(default_factory=WebhookConfig)

    # 运行期元信息（不写入文件）
    config_path: str | None = None
    env_path: str | None = None
    #: 覆盖层（WebUI / onboard 的写回目标）。不存在时为 None——「设置」页据此决定
    #: 是提示「还没有覆盖层」还是「改过这些键」。
    overlay_path: str | None = None
    #: **解析后的输入路径**，文件不存在时同样记录。
    #:
    #: 为什么不能只用上面那两个：`config_path` / `env_path` 在文件不存在时是 None，而热重载
    #: （`web/setup.py::reload_state`）拿 None 再走一遍 `load_settings` 会**回落到 cwd 下的
    #: 默认文件**。具体后果实测过：用 `--config /tmp/x.yaml` 起的服务，保存一次仓库配置后
    #: 数据库就从 `/tmp/t.db` 悄悄换成了 `./fissue.db`——用户不会想到「改个仓库列表会换库」。
    config_source: str | None = None
    env_source: str | None = None

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
    def _validate(self) -> Settings:
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

        # 安全默认：把「远程可达」与「无身份」这两件事**互斥**掉。
        #
        # （这一段替换了旧版的 `api.auth_required` 判据。旧版唯一的保险是「设了
        # `FISSUE_API_TOKEN` 就自动把它翻成 true」，于是把 `WEB_HOST` 指向 0.0.0.0
        # 就等于把会写外部仓库的路由暴露给所有人——fail-open。）
        #
        # 现在的判据只看**配置**（此刻数据库还没连上，拿不到「有没有管理员」）：
        #
        #     条件：web.host 非回环 且 api.enabled
        #     放行当且仅当：web.auth.enabled 或 web.trusted_networks 非空
        #
        # 因为 `web.auth.enabled` 默认 true，唯一还会触发这条报错的情况是：用户**显式**
        # 关掉了会话鉴权、又没声明可信网、却把页面绑到非回环地址——也就是「既不要身份、
        # 又不要可信网、又要对外」，这正是应当拦下的组合。
        #
        # 「尚无管理员账号」这件事启动期看不见，改由**请求期**兜（web/auth.py 的
        # bootstrap 流程）。启动期放行但会打一条警告。
        host = str(self.web.host or "").strip().lower()
        loopback = host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")
        has_mechanism = self.web.auth.enabled or bool(self.web.trusted_networks)
        if not loopback and self.api.enabled and not has_mechanism:
            raise ConfigError(
                f"api.enabled=true 且 web.host={self.web.host} 对外可达，但没有身份机制："
                "请在 config.yaml 打开 web.auth.enabled（默认就是 true，你应当是被显式关掉过），"
                "或声明可信内网网段 web.trusted_networks（例如 [\"192.168.1.0/24\"]）。"
            )
        if not loopback and not has_mechanism:
            # 页面本身只有 GET（只读），所以这里只提醒不拦——但提前告知，
            # 免得之后一开 api.enabled 就被上面那条拒绝启动。
            import logging

            logging.getLogger(__name__).warning(
                "web.host=%s 对外可达且未声明任何身份机制：页面只读无碍，但一旦启用 api.enabled "
                "会被拒绝启动；建议现在就打开 web.auth.enabled 或声明 trusted_networks",
                self.web.host,
            )
        # 入站 webhook：开了却没有密钥 = 那个端点对外裸奔，**直接拒绝启动**（fail-closed）。
        # 这里是构造期路径，正是把配置错误拦在最早的时刻；请求期还会再拦一道（见 web/hooks.py）。
        if self.webhooks.enabled:
            if not self.api.enabled:
                raise ConfigError(
                    "webhooks.enabled=true 需要 api.enabled=true：端点挂在 API 前缀下，"
                    "api 关掉等于它从未存在。请开 api.enabled，或关掉 webhooks.enabled。"
                )
            need = sorted({r.platform.value for r in self.enabled_repos()})
            missing = [p for p in need if not self.webhooks.secrets.get(Platform(p))]
            if missing:
                raise ConfigError(
                    "webhooks.enabled=true，但以下已启用平台的 webhook 密钥是空的："
                    + "、".join(missing)
                    + "。请在 .env 里设置 "
                    + "、".join(f"{ENV_PREFIX}WEBHOOK_{m.upper()}_SECRET" for m in missing)
                    + "（或写进 config.yaml 的 webhooks.secrets）——"
                    "否则那些平台的推送一律会被拒绝处理。"
                )

        # 注意：「绑本机但 api.enabled 且无鉴权」的告警**不在这里发**：这里是每条命令都会
        # 走的构造期路径，而那条告警讲的是「即将对外提供 API」，发在 `web.app.create_app`
        # 更贴切。（它历史上还额外打坏过 `fissue status --json`——那是日志写到了 stdout
        # 导致的，根因已在 `logging_setup` 修正：现在日志一律走 stderr。）
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

    # 入站 webhook：开关与各平台密钥都支持环境变量（容器部署时不方便改 config.yaml）
    wh = merged.setdefault("webhooks", {})
    if v := env_of("WEBHOOK_ENABLED"):
        wh["enabled"] = v.strip().lower() in ("1", "true", "yes", "on")
    wh_secrets = wh.setdefault("secrets", {})
    # 密钥名**写成字面量、不循环拼**：`tests/test_env.py` 会逐字核对「.env.example 里写的 =
    # 代码真的读的」，而拼出来的名字它认不出——那条守卫正是用来防「文档写了、代码不读」的。
    if v := env_of("WEBHOOK_GITHUB_SECRET"):
        wh_secrets["github"] = v
    if v := env_of("WEBHOOK_GITEE_SECRET"):
        wh_secrets["gitee"] = v
    if v := env_of("WEBHOOK_ATOMGIT_SECRET"):
        wh_secrets["atomgit"] = v
    if v := env_of("WEBHOOK_GITLAB_SECRET"):
        wh_secrets["gitlab"] = v

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
    # 这里曾有 `FISSUE_API_TOKEN` → `api.token` + 自动翻 `auth_required`。
    # 单一口令的机制已被会话 + API Key 整体替换（docs/AUTH.md §2 决策 3、§6）。
    # 旧口令**仍可**被一次性导入为一条 API Key，但那条路径在 `fissue.web.auth` 里
    # 显式读环境变量，不再把它接进通用配置模型——口令不该是一个「配置项」。

    return merged


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _sub_model(annotation: Any) -> type[BaseModel] | None:
    """字段注解里的子模型（含 ``X | None`` 这类联合）。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in getattr(annotation, "__args__", ()):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _legacy_auth_keys(raw: dict[str, Any]) -> list[str]:
    """配置里残留的**旧版单一口令**键（`api.token` / `api.auth_required`）。

    它们已被会话 + API Key 整体替换（docs/AUTH.md §2 决策 3），但老部署的 config.yaml 里
    多半还留着。识别出来是为了给一条**说人话**的迁移提示，而不是拿通用那句
    「未知键 `api.auth_required`，你是不是想写 enabled？」去打发刚升级上来的用户。
    """
    api = raw.get("api")
    if not isinstance(api, dict):
        return []
    return [f"api.{key}" for key in ("token", "auth_required") if key in api]


def _unknown_config_keys(
    raw: dict[str, Any], model: Any = None, prefix: str = ""
) -> list[tuple[str, list[str]]]:
    """找出 YAML 里**模型不认识的键路径**，返回 ``[(路径, 该层已知键), …]``。

    为什么不直接报错：项目明确允许「老配置文件里的残留键」（见
    ``tests/test_config_models.py::test_deleted_keys_in_old_config_files_do_not_break_loading``），
    报错会把这类用户的加载直接打死。

    但**静默忽略**更糟：把 ``api.auth_required`` 写成 ``api.auth_requird``、``web.read_only``
    写成 ``web.readonly``，pydantic 默认一声不响地丢掉——而这两个都是**安全开关**：用户以为
    开了，实际按默认值跑。所以策略是：不报错，但一条条**响亮地喊出来**，并给出「你是不是想写 X」。
    """
    model = model or Settings
    fields = dict(getattr(model, "model_fields", {}))
    out: list[tuple[str, list[str]]] = []
    for key, value in (raw or {}).items():
        if key not in fields:
            out.append((f"{prefix}{key}", sorted(fields)))
            continue
        sub = _sub_model(fields[key].annotation)
        if isinstance(value, dict) and sub is not None:
            out.extend(_unknown_config_keys(value, sub, prefix=f"{prefix}{key}."))
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

    # 覆盖层（WebUI / `fissue onboard` 的写回目标）夹在 config.yaml 与 .env 之间：
    #
    #     进程环境 > .env > config.local.yaml > config.yaml > 内置默认值
    #
    # 为什么在 .env **之下**：.env 装的是密钥与部署项（更贴近这次具体部署），两者冲突时它该赢；
    # 为什么在 config.yaml **之上**：它是「人通过 WebUI 明确改过的值」，语义上就是补丁。
    # 本函数只**读**覆盖层；写入规则与「不许丢数据」的约束见 `fissue/configio.py`。
    #
    # 为什么在这里才 import：configio 需要 config 里的常量与 _deep_merge，模块级互相 import
    # 会成环。
    from .configio import read_yaml_mapping  # noqa: PLC0415 —— 规避模块级循环导入

    ol_path = overlay_path_for(cfg_path)
    overlay = read_yaml_mapping(ol_path)
    if overlay:
        raw = _deep_merge(raw, overlay)

    env_values = _read_env_file(env_file)
    # .env 的值必须让 TLS 判定也能看到：``net`` 读的是 ``os.getenv``，而 .env 是
    # ``dotenv_values`` 读成的字典（不导出到进程环境），不登记的话「在 .env 里设
    # FISSUE_CA_BUNDLE 解决 TLS」会**静默无效**——只有 export 到 shell 才管用。
    net.register_env_file_values(env_values)
    raw = _apply_env_overrides(raw, env_values)
    if overrides:
        raw = _deep_merge(raw, overrides)

    # 未知键**不报错**（项目允许老配置里的残留键），但必须**响亮**：安全开关被拼错时，
    # 静默忽略等于让用户以为它生效了。见 _unknown_config_keys 的说明。
    #
    # 例外：旧版单一口令机制那两个键（`api.token` / `api.auth_required`）走下面那条**专门**的
    # 提示。它们不是拼错，是被整体替换掉了（docs/AUTH.md §2 决策 3）——拿「你是不是想写
    # enabled？」去提示一个刚升级上来的用户，只会把人带偏。
    legacy_keys = set(_legacy_auth_keys(raw))
    for path, candidates in _unknown_config_keys(raw):
        if path in legacy_keys:
            continue
        leaf = path.rsplit(".", 1)[-1]
        near = difflib.get_close_matches(leaf, candidates, n=2, cutoff=0.6)
        logging.getLogger(__name__).warning(
            "配置里有未知键 `%s`，会被忽略%s",
            path,
            f"；你是不是想写 {'、'.join(near)}？" if near else "",
        )
    if legacy_keys:
        logging.getLogger(__name__).warning(
            "配置里的 %s 已不再是配置项：单一访问口令机制已被**会话 + API Key** 整体替换"
            "（见 docs/AUTH.md），它们会被忽略。若 .env 里还留着 FISSUE_API_TOKEN，"
            "启动时会自动把它导入成一条 API Key（prefix=legacy）——到「API Key」页新建一把"
            "正式 Key 并吊销它即可。",
            "、".join(sorted(legacy_keys)),
        )

    try:
        settings = Settings(**raw)
    except Exception as exc:
        raise ConfigError(f"配置校验失败：{exc}") from exc

    settings.config_path = str(cfg_path) if cfg_path.exists() else None
    settings.env_path = str(env_file) if env_file.exists() else None
    # 覆盖层同理：**存在才记**（页面据此区分「还没生成」与「已经改过这些键」）。
    # 别把它跟 config_source / env_source 混了——那两个是「解析出的输入路径」，不存在也记。
    settings.overlay_path = str(ol_path) if ol_path.exists() else None
    # 无论存在与否都记下**解析结果**：热重载靠它复现同一次加载，不会回落到 cwd 的默认文件。
    settings.config_source = str(cfg_path)
    settings.env_source = str(env_file)

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
