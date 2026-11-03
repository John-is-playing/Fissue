"""部署域模型：计划（plan）、步骤（step）、动作规格与状态枚举。

这一层是**契约**：AI 生成的计划、执行器执行的动作、落库的 JSON、Web 返回的
结构，三处都以此为准。改这里等于改契约，先改 ``docs/DEPLOY.md``。

为什么不用「AI 直接给一串 shell」：
    一串 shell 既没法逐条记录执行结果（就没法回答「第几步挂了」），也没法
    在失败点干净地停下来回滚，更没法在入库前做白名单校验。结构化动作把
    「模型想干什么」和「机器允许干什么」之间隔了一层。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import utcnow

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class StepKind(str, Enum):
    """动作种类。每个 kind 只允许填对应的那个规格字段。"""

    UPLOAD = "upload"          # 把本地产物传到远端 release 目录
    WRITE_FILE = "write_file"  # 写 unit / nginx conf / .env 之类的小文件
    EXEC = "exec"              # 在远端跑命令（过 guard 检查）
    SERVICE = "service"        # systemd 动作：enable / start / restart / stop
    PROBE = "probe"            # 从**控制端**发起的健康探针


class ServiceAction(str, Enum):
    DAEMON_RELOAD = "daemon-reload"
    ENABLE = "enable"
    START = "start"
    RESTART = "restart"
    STOP = "stop"


class DeployStatus(str, Enum):
    """一次部署的状态机（见 docs/DEPLOY.md §4）。

    只有进入终态（success / failed / rolled_back / destroyed）才允许
    ``finished_at`` 有值；执行中的记录被进程杀掉时会停在 running，由
    ``deploy status`` 按「超时未完成」提示可以人工收尾。
    """

    PLANNING = "planning"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    DESTROYED = "destroyed"

    @property
    def terminal(self) -> bool:
        return self in {
            DeployStatus.SUCCESS,
            DeployStatus.FAILED,
            DeployStatus.ROLLED_BACK,
            DeployStatus.DESTROYED,
        }


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class AuthMethod(str, Enum):
    KEY = "key"
    PASSWORD = "password"


# ---------------------------------------------------------------------------
# 动作规格
# ---------------------------------------------------------------------------


class UploadSpec(BaseModel):
    """把本地路径传到远端。

    ``src`` 相对仓库根或绝对路径；``dst`` 一律**相对 release 目录**，
    防止计划把文件写到 ``/etc`` 去——那是 ``write_file`` 的事，而它要求
    显式 ``allow_outside_release``。
    """

    src: str
    dst: str = "."

    @model_validator(mode="after")
    def _no_absolute_dst(self) -> UploadSpec:
        if self.dst.startswith("/") or ".." in self.dst.split("/"):
            raise ValueError(f"upload.dst 必须是 release 内相对路径：{self.dst!r}")
        return self


class WriteFileSpec(BaseModel):
    path: str
    content: str
    mode: str = "0644"
    #: 允许写到 release 目录之外（unit 文件、nginx 站点配置必须写出去）。
    allow_outside_release: bool = False

    @model_validator(mode="after")
    def _mode_ok(self) -> WriteFileSpec:
        if not re.fullmatch(r"0?[0-7]{3,4}", self.mode):
            raise ValueError(f"非法文件权限：{self.mode!r}")
        return self


class ExecSpec(BaseModel):
    command: str
    cwd: str | None = None
    timeout: int | None = Field(default=None, ge=1)


class ServiceSpec(BaseModel):
    action: ServiceAction
    name: str
    #: 是否需要 root。**非 root 账号部署到系统级 unit 时必须置 true**——真机验证
    #: 时踩到的第一个坑就是它：`john` 用户执行 `systemctl restart` 直接被拒，
    #: 而计划里没有任何地方能表达「这一步要提权」。
    #:
    #: 它同时受 ``deploy.allow_dangerous`` 约束：service 动作不走 ``guard`` 的命令
    #: 检查（它不是 exec），所以这项检查放在执行器里，见 ``runner._execute``。
    sudo: bool = False

    @model_validator(mode="after")
    def _name_ok(self) -> ServiceSpec:
        # unit 名会成为 systemctl 的参数；挡住空格与 shell 元字符，
        # 否则 `service.name` 就是一条隐形的命令注入通道。
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", self.name):
            raise ValueError(f"非法 unit 名：{self.name!r}")
        return self


class ProbeSpec(BaseModel):
    """健康探针。**从控制端发起**（见 docs/DEPLOY.md §1）。

    ``url`` 与 ``port`` 二选一：给了 url 就发 HTTP，只给 port 就做 TCP 连通。
    """

    url: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    expect_status: int | None = Field(default=None, ge=100, le=599)
    expect_contains: str | None = None
    timeout: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _one_of(self) -> ProbeSpec:
        if bool(self.url) == (self.port is not None):
            raise ValueError("probe 必须且只能给 url 或 port 之一")
        if self.url and not re.match(r"^https?://", self.url):
            raise ValueError(f"probe.url 只支持 http/https：{self.url!r}")
        return self


class CheckSpec(BaseModel):
    """一步的判定条件。缺省判定见各 kind 的默认语义（退出码 0 等）。"""

    expect_exit_code: int = 0
    output_contains: str | None = None
    output_excludes: str | None = None


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------

#: kind → 规格字段名。用它做「一个 kind 只填一个规格」的校验与分派。
_SPEC_FIELD: dict[StepKind, str] = {
    StepKind.UPLOAD: "upload",
    StepKind.WRITE_FILE: "write",
    StepKind.EXEC: "exec",
    StepKind.SERVICE: "service",
    StepKind.PROBE: "probe",
}


class DeployStep(BaseModel):
    """一个可执行、可判定、可回滚的最小动作。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1, max_length=64)
    kind: StepKind
    title: str = ""

    upload: UploadSpec | None = None
    write: WriteFileSpec | None = None
    exec: ExecSpec | None = None
    service: ServiceSpec | None = None
    probe: ProbeSpec | None = None

    check: CheckSpec | None = None
    #: 这一步失败后要补做的动作（通常是把服务停回去）。
    #: 不再嵌套 rollback——回滚动作套回滚动作没有尽头，也不可验证。
    rollback: list[DeployStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_spec(self) -> DeployStep:
        want = _SPEC_FIELD[self.kind]
        present = [name for name in _SPEC_FIELD.values() if getattr(self, name) is not None]
        if present != [want]:
            raise ValueError(
                f"步骤 {self.id!r}: kind={self.kind.value} 需要且只允许填 {want!r}，实际填了 {present or '（空）'}"
            )
        return self

    @property
    def spec(self) -> BaseModel:
        """按 kind 取出对应的规格对象。"""
        return getattr(self, _SPEC_FIELD[self.kind])

    def summary(self) -> str:
        """一行人类可读描述（CLI 打印 / 审计日志用）。"""
        spec = self.spec
        if isinstance(spec, UploadSpec):
            return f"upload {spec.src} → {spec.dst}"
        if isinstance(spec, WriteFileSpec):
            return f"write_file {spec.path} ({spec.mode})"
        if isinstance(spec, ExecSpec):
            return f"exec {spec.command}"
        if isinstance(spec, ServiceSpec):
            return f"systemctl {spec.action.value} {spec.name}"
        if isinstance(spec, ProbeSpec):
            target = spec.url or f"tcp:{spec.port}"
            return f"probe {target}"
        return self.kind.value


class AppSpec(BaseModel):
    """计划对「这是个什么应用」的结论。"""

    name: str
    runtime: str = "unknown"          # python | node | static | docker | unknown
    service_name: str | None = None
    #: ``service_name`` 对应的 unit 是否为系统级、需要 sudo 才能操作。
    #: 回滚与健康检查也读它（它们不再回头解析计划里的每一步）。
    service_sudo: bool = False
    port: int | None = Field(default=None, ge=1, le=65535)
    entry: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _name_ok(self) -> AppSpec:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.name):
            raise ValueError(f"非法应用名：{self.name!r}")
        return self


class DeployPlan(BaseModel):
    """AI 生成的部署计划。

    ``version`` 留作未来不兼容变更的出口；执行器遇到不认识的大版本直接拒跑，
    而不是猜着执行——猜错是在别人服务器上猜。
    """

    #: 赋值也走校验。没有它时「给字段赋一个方法对象」会静默成功，直到落库
    #: （``model_dump``）才以 ``Unable to serialize unknown type: <class 'method'>``
    #: 爆出来——真机验证时正是踩在 ``commit_sha`` 上：``getattr(ws, "commit")`` 拿到的是
    #: ``RepoWorkspace.commit()`` 这个方法本身。
    model_config = ConfigDict(validate_assignment=True)

    version: int = 1
    repo_slug: str
    ref: str = "main"
    commit_sha: str | None = None
    app: AppSpec
    steps: list[DeployStep] = Field(default_factory=list)
    #: 部署收尾必须全绿的探针（独立证据，见 §1）。
    health: list[ProbeSpec] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    notes: str = ""
    generated_at: Any = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _ids_unique(self) -> DeployPlan:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"步骤 id 重复：{step.id!r}")
            seen.add(step.id)
        if not self.health:
            # 没有健康检查的「成功」等于没判据，直接判计划非法。
            raise ValueError("计划必须至少包含一条 health 探针")
        return self

    def get_step(self, step_id: str) -> DeployStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def dump(self) -> dict[str, Any]:
        """存库用的 JSON（枚举转字符串，datetime 转 ISO）。"""
        return self.model_dump(mode="json")

    @classmethod
    def load(cls, raw: dict[str, Any]) -> DeployPlan:
        return cls.model_validate(raw)


class HostSpec(BaseModel):
    """连接目标的只读视图（凭据不在其中）。"""

    name: str
    address: str
    port: int = Field(default=22, ge=1, le=65535)
    user: str = "root"
    auth_method: AuthMethod = AuthMethod.KEY
    host_fingerprint: str | None = None
    enabled: bool = True


class StepResult(BaseModel):
    """一步的实际结果（执行器产出，落库）。"""

    step_id: str
    kind: StepKind
    status: StepStatus
    exit_code: int | None = None
    output_tail: str = ""
    error: str | None = None