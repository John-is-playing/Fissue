"""新手引导的生成：固定前两阶段 + AI 生成的第三阶段。

设计见 ``docs/DEPLOY.md`` §15。三条不变量：

1. **前两阶段不依赖模型**。「Fissue 是什么、凭据放哪、主机怎么登记」这些是产品
   事实，模型抽风不该让新手连这些字都看不到。
2. **模型只写叙述与命令**。半自动/自动档执行的是页面写死的五个动作
   （见 :class:`~fissue.deploy.schema.GuideAction`），模型不能指定接口——
   否则一句「顺手 DELETE 一下」就能借引导页之手改服务器。
3. **模型不可用就降级，不是报错**。生成失败返回 ``source="fallback"`` 的固定模板，
   页面照常能展示手动步骤。「自动挂了就看手动」是这套引导存在的理由。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import FissueError
from ..logging_setup import get_logger
from .planner import collect_repo_context
from .schema import (
    GuideAction,
    GuideMode,
    GuidePlan,
    GuideStage,
    GuideStep,
)

log = get_logger(__name__)

#: 引导总步骤上限（schema 也卡 40，这里再紧一点：新手看不完等于没写）。
MAX_GUIDE_STEPS = 24


class GuideError(FissueError):
    """引导生成失败（模型输出不合 schema 且无法降级时）。"""


# ---------------------------------------------------------------------------
# 固定阶段（不依赖模型）
# ---------------------------------------------------------------------------


def intro_stage(host: str = "") -> GuideStage:
    """阶段一：认识 Fissue。"""
    return GuideStage(
        id="intro",
        title="第一步：先搞清楚这套工具会做什么",
        goal="Fissue 能读仓库、登服务器、起服务，并且**只在证据齐全时**才说部署成功。",
        steps=[
            GuideStep(
                id="what",
                title="它会做什么、不会做什么",
                why=(
                    "它会：克隆仓库、生成部署计划、通过 SSH 装依赖并起 systemd 服务，"
                    "然后用「服务状态 + 端口监听 + 从你这台机器发起的探针」三样证据判断是否成功。\n"
                    "它不会：自动合并 PR、替你保管明文密码（凭据加密存在库里，接口只回显认证方式与指纹）、"
                    "在 `deploy.enabled=false` 时做任何远端操作。"
                ),
            ),
            GuideStep(
                id="enable",
                title="确认部署能力已开启",
                why="这是能改远端机器的能力，默认关闭。",
                command="grep -A3 '^deploy:' config.yaml",
                verify="看到 `enabled: true`；若为 false，改成 true 后重启服务。",
            ),
            GuideStep(
                id="safety",
                title="知道危险动作的边界",
                why=(
                    "`sudo`、`rm -rf`、`apt remove` 等默认被拒绝，需要 `deploy.allow_dangerous=true` "
                    "或 `--allow-dangerous`；而 `rm -rf /`、`mkfs`、`dd of=/dev/…` 这类**永远**被拒绝。"
                ),
            ),
        ],
    )


def prepare_stage(host: str = "") -> GuideStage:
    """阶段二：准备服务器（登记主机 + 确认指纹）。"""
    host_arg = host or "<主机别名>"
    return GuideStage(
        id="prepare",
        title="第二步：把目标服务器登记进来",
        goal="登记一台能 SSH 登录的 Linux（有 systemd），并确认它的主机指纹。",
        steps=[
            GuideStep(
                id="ssh-check",
                title="先确认你能手动登上去",
                why="Fissue 用的是同一套密钥。手动都上不去，它更上不去。",
                command=f"ssh -i ~/.ssh/id_ed25519 {host_arg} 'uname -a; systemctl --version | head -1'",
                verify="能看到内核版本与 systemd 版本（没有 systemd 的机器部署不了服务）。",
            ),
            GuideStep(
                id="add-host",
                title="登记主机（凭据会加密入库）",
                why=(
                    "登记时读入私钥或密码，用服务器密钥派生后**可逆加密**存库；"
                    "列表与接口只回显认证方式与指纹。"
                ),
                command=(
                    f"fissue host add {host_arg} --address <IP> --user <用户> --key-file <私钥路径>"
                ),
                verify="`fissue host list` 里能看到这台主机，`fingerprint` 显示「未建立」。",
                action=GuideAction.ADD_HOST,
            ),
            GuideStep(
                id="ping",
                title="连一下并钉住主机指纹（TOFU）",
                why=(
                    "第一次连接会记录主机公钥指纹，之后**不匹配就拒连**——这是中间人防线。"
                    "换机器/重装系统后需要人工更新指纹，不会自动接受。"
                ),
                command=f"fissue host ping {host_arg}",
                verify="打印 uptime / 磁盘 / systemd；再 `host show` 能看到指纹已记录。",
                action=GuideAction.CHECK,
            ),
        ],
    )


def fallback_deploy_stage(repo_slug: str, host: str = "") -> GuideStage:
    """阶段三的兜底模板：模型不可用时也能照着走完。

    刻意写得**保守**：不猜运行时、不猜端口，只给「生成计划 → 看计划 → 执行 →
    看证据 → 必要时回滚」这条与任何技术栈都成立的链路。
    """
    host_arg = host or "<主机别名>"
    return GuideStage(
        id="deploy",
        title="第三步：部署这个仓库（通用步骤）",
        goal="先用计划看清它打算做什么，再执行；失败就回滚。",
        steps=[
            GuideStep(
                id="plan",
                title="先只要计划，不执行",
                why="计划会列出每一步要跑的命令、每条命令怎么判定成功、失败怎么回滚。先看一遍再动手。",
                command=f"fissue deploy plan {repo_slug} --host {host_arg}",
                verify="打印出步骤列表与健康探针；计划同时存库（`deploy status` 可查）。",
                action=GuideAction.GENERATE_PLAN,
            ),
            GuideStep(
                id="review",
                title="检查计划里这三处",
                why=(
                    "① 健康探针不能是 `127.0.0.1`（控制端要能访问到）；"
                    "② 需要 root 的步骤是否标了 sudo；"
                    "③ unit 文件是否由计划自己安装（缺了会报 Unit not found）。"
                ),
                command="fissue deploy status",
                verify="最近一条计划的状态是 planning，步骤数与预期相符。",
                action=GuideAction.CHECK,
            ),
            GuideStep(
                id="apply",
                title="执行部署",
                why="上传 → 装依赖 → 起服务 → 独立证据判定；任一步失败会停下并尝试回滚。",
                command=f"fissue deploy apply {repo_slug} --host {host_arg}",
                verify="末行显示 `success` 且「证据 全部证据通过」。",
                action=GuideAction.APPLY,
            ),
            GuideStep(
                id="check",
                title="看证据，别只看状态",
                why="状态是结论，证据才是判据：systemd 是否 active、端口是否在听、控制端探针是否通。",
                command="fissue deploy status <部署id> && fissue deploy logs <部署id>",
                verify="三样证据都能看到；`url` 用浏览器能打开。",
                action=GuideAction.CHECK,
            ),
            GuideStep(
                id="rollback",
                title="出问题就回滚到上一版",
                why="`current` 是软链，回滚只是把它指回上一个 release 并重启，数据在 `shared/` 不受影响。",
                command="fissue deploy rollback <部署id>",
                verify="状态变成 `rolled_back`，且回滚后会**重新探针**——探针通才算恢复。",
                action=GuideAction.ROLLBACK,
            ),
        ],
    )


def fallback_guide(repo_slug: str, host: str = "", *, notes: str = "") -> GuidePlan:
    """完整兜底引导：固定两阶段 + 通用第三阶段。"""
    return GuidePlan(
        repo_slug=repo_slug,
        host=host,
        mode=GuideMode.HALF,
        stages=[intro_stage(host), prepare_stage(host), fallback_deploy_stage(repo_slug, host)],
        notes=notes,
        source="fallback",
    )


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

SYSTEM_GUIDE_WRITER = """你是给新手写部署指南的工程师。读者**从没部署过服务**，但会照着命令敲。

铁律：
1. 只输出一个**合法 JSON 对象**，不要解释、不要 Markdown 代码块。
2. 只写 `deploy` 这一个阶段（stages 数组里**恰好一个**元素，id 用 "deploy"）；
   前面的「认识工具」「准备服务器」由系统固定提供，不要重复。
3. 步骤要具体到「敲什么、看到什么算成功」：
   - `command` 必须是可以直接复制执行的完整命令，最好用 `fissue deploy ...`；
   - `verify` 写「怎么确认这一步成了」，不要写「应该没问题」。
4. 你**不能**指定接口或 URL。若某步对应系统内置动作，只能从这些里选：
   add-host | generate-plan | apply | check | rollback；对不上就留空（纯手动）。
5. 必须覆盖：需要什么运行时/系统包、依赖怎么装、用什么命令起服务、怎么验证它在听、
   日志在哪看、失败怎么回滚。仓库里没写的（端口、启动方式）**不要编**，
   在 `notes` 里说明「未找到，需人工确认」。
6. 总步骤不超过 12 步；宁可分成「先能起」「再能稳」两组，也不要一条命令干三件事。
7. **不要教人手搓部署**。禁止出现 `git clone` 到工作目录、`nohup ... &`、`pkill -f`、
   `cd ~/xxx && python run.py` 这类手工管理方式：真正的部署由 `fissue deploy apply`
   完成（release 目录 + `current` 软链 + systemd 托管 + `shared/` 存数据）。
   手动步骤只用来**准备环境与排查**（查 Python 版本、装系统包、看端口、看日志）。
8. 需要人确认服务是否可用时，别用 `curl http://127.0.0.1:...`：那只能证明远端自己看得到自己。
   用 `fissue deploy status <id>` 看证据，或写 `curl http://<控制端能访问的地址>:<端口>/`。
"""

SCHEMA_GUIDE = """\
GuidePlan（只填这些字段）:
  version: int = 1
  repo_slug: str
  host: str | null
  mode: "readonly" | "half" | "auto"   # 默认 half
  stages: [GuideStage]                 # 恰好一个，id="deploy"
  notes: str
  source: "ai"

GuideStage:
  id: str        # 必须是 "deploy"
  title: str
  goal: str
  steps: [GuideStep]

GuideStep:
  id: str         # <=64，全阶段唯一
  title: str      # <=120，动词开头，如「安装系统依赖」
  why: str        # 为什么需要这一步（新手最缺的就是这个）
  command: str|null
  verify: str|null
  action: "add-host"|"generate-plan"|"apply"|"check"|"rollback"|null
  cautions: [str]
"""


def guide_prompt(
    *,
    repo_slug: str,
    host: str,
    host_address: str,
    context: str,
    deploy_enabled: bool,
) -> list[dict[str, str]]:
    """组提示词（纯函数，便于测试断言读了哪些内容）。"""
    user = (
        f"仓库：{repo_slug}\n"
        f"目标主机：{host}（{host_address}）\n"
        f"部署能力已开启：{deploy_enabled}\n\n"
        "下面是仓库的 README、依赖清单与启动线索。请据此写第三阶段「部署这个仓库」：\n\n"
        f"{context}\n\n"
        "严格按下面的 schema 输出 JSON（只给 JSON）：\n\n" + SCHEMA_GUIDE
    )
    return [
        {"role": "system", "content": SYSTEM_GUIDE_WRITER},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 生成器
# ---------------------------------------------------------------------------


#: 每个固定动作对应的**规范命令**。
#:
#: 真机验证时模型两次编错 CLI 语法（`fissue deploy add-host`、`fissue deploy rollback <slug>`），
#: 而这条命令是要新手直接复制执行的——错的比没有更糟。所以有 ``action`` 的步骤，
#: 命令一律由这里覆盖：动作是语义锚点，命令是它的派生物，不该由模型自由发挥。
CANONICAL_COMMANDS: dict[GuideAction, str] = {
    GuideAction.ADD_HOST: "fissue host add {host} --address <IP> --user <用户> --key-file <私钥路径>",
    GuideAction.GENERATE_PLAN: "fissue deploy plan {repo_slug} --host {host}",
    GuideAction.APPLY: "fissue deploy apply {repo_slug} --host {host}",
    GuideAction.CHECK: "fissue deploy status <部署id>",
    GuideAction.ROLLBACK: "fissue deploy rollback <部署id>",
}

#: 已存在的 Fissue 命令。手动步骤里出现 `fissue ` 但不匹配它，就加一条核对提示。
#: 必须**精确到子命令**：`fissue deploy-status` 以 `fissue deploy` 开头，
#: 用前缀匹配会把它误判成合法命令（模型真编过这种）。
_KNOWN_FISSUE_COMMAND = re.compile(
    r"^fissue\s+("
    r"host\s+(add|list|show|remove|ping|enable)"
    r"|deploy\s+(plan|apply|status|logs|rollback|destroy)"
    r"|status"
    r"|onboard"
    r"|sandbox\s+(serve|check)"
    r")\b"
)


def _normalize_commands(plan: GuidePlan, *, repo_slug: str, host: str) -> None:
    """把 ``action`` 步骤的命令换成规范写法，并给可疑的手动命令加提示。

    只动 ``command`` 文本，不改标题/解释/动作——引导的叙述仍是模型写的，
    会被执行的命令则来自代码。
    """
    host_arg = host or "<主机别名>"
    for stage in plan.stages:
        for step in stage.steps:
            if step.action is not None:
                canonical = CANONICAL_COMMANDS.get(step.action, "").format(
                    host=host_arg, repo_slug=repo_slug or "<owner/name>"
                )
                if canonical and step.command != canonical:
                    if step.command:
                        step.cautions = [
                            *step.cautions,
                            "命令已按 Fissue 实际语法校正（模型原写的不一定存在）",
                        ]
                    step.command = canonical
                continue
            # 手动步骤：模型可能编出不存在的子命令，提示人去核对而不是直接删掉
            # （删掉会让新手失去上下文）。
            text = (step.command or "").lstrip()
            if text.startswith("fissue ") and not _KNOWN_FISSUE_COMMAND.match(text):
                step.cautions = [
                    *step.cautions,
                    "这条命令里的子命令未经校验，执行前请用 `fissue --help` 核对",
                ]


class GuideBuilder:
    """把「仓库内容 + 一次 LLM 调用」变成一份新手引导；失败则降级。"""

    def __init__(self, client: Any, *, max_repairs: int = 1) -> None:
        self.client = client
        self.max_repairs = max(0, max_repairs)
        #: 最近一次调用的累计用量（供上层记账）。
        self.last_usage: Any = None

    async def build(
        self,
        root: str | Path,
        *,
        repo_slug: str,
        host: str = "",
        host_address: str = "",
        deploy_enabled: bool = True,
        notes: str = "",
    ) -> GuidePlan:
        """生成引导。**任何失败都降级为 fallback**，只记警告不抛异常。

        为什么降级而不是抛：这是新手页面，报错等于「你还是自己看文档吧」；
        而固定模板本身就能把人带完，只是少了针对这个仓库的细节。
        """
        try:
            context = collect_repo_context(root)
        except Exception as exc:  # noqa: BLE001 - 读取仓库失败也要能给出兜底
            log.warning("收集仓库上下文失败，改用兜底引导：%s", exc)
            return fallback_guide(repo_slug, host, notes=f"{notes}\n读取仓库失败：{exc}".strip())

        try:
            plan = await self._ask(
                repo_slug=repo_slug,
                host=host,
                host_address=host_address,
                context=context,
                deploy_enabled=deploy_enabled,
            )
        except Exception as exc:  # noqa: BLE001 - 模型/网络/解析失败统一降级
            log.warning("生成引导失败，改用兜底模板：%s", exc)
            return fallback_guide(
                repo_slug, host, notes=f"{notes}\n模型不可用：{type(exc).__name__}".strip()
            )

        # 固定阶段 + AI 阶段拼装：前两阶段永远来自代码，避免模型"忘了写"。
        plan.stages = [intro_stage(host), prepare_stage(host), *plan.stages]
        plan.repo_slug = repo_slug
        plan.host = host
        plan.source = "ai"
        _normalize_commands(plan, repo_slug=repo_slug, host=host)
        if notes:
            plan.notes = (plan.notes + "\n" + notes).strip()
        return plan

    async def _ask(
        self,
        *,
        repo_slug: str,
        host: str,
        host_address: str,
        context: str,
        deploy_enabled: bool,
    ) -> GuidePlan:
        messages = guide_prompt(
            repo_slug=repo_slug,
            host=host,
            host_address=host_address,
            context=context,
            deploy_enabled=deploy_enabled,
        )
        last_error = ""
        for attempt in range(self.max_repairs + 1):
            raw, usage = await self.client.chat_json(
                messages, purpose="deploy.guide", default={}
            )
            self.last_usage = usage if self.last_usage is None else self.last_usage
            if not isinstance(raw, dict) or not raw:
                last_error = "模型没有返回可解析的 JSON"
            else:
                try:
                    plan = GuidePlan.load(raw)
                    self._check_actions(plan)
                    if plan.step_count() > MAX_GUIDE_STEPS:
                        raise GuideError(f"步骤过多（{plan.step_count()} > {MAX_GUIDE_STEPS}）")
                    return plan
                except Exception as exc:  # noqa: BLE001 - 校验失败带错误重试一轮
                    last_error = str(exc)
            if attempt < self.max_repairs:
                messages = messages + [
                    {"role": "assistant", "content": "（上一轮输出不合法）"},
                    {
                        "role": "user",
                        "content": (
                            f"上面的输出没通过校验：{last_error}\n"
                            "请只输出一个合法 JSON 对象，字段严格照 schema，"
                            'stages 恰好一个且 id 必须是 "deploy"。'
                        ),
                    },
                ]
        raise GuideError(f"引导生成失败：{last_error}")

    def _check_actions(self, plan: GuidePlan) -> None:
        """动作只允许固定枚举（schema 已保证），这里再挡「一步塞多个动作」的典型错误。"""
        allowed = {action.value for action in GuideAction}
        for stage in plan.stages:
            if stage.id != "deploy":
                raise GuideError(f"AI 只应写 id=deploy 的阶段，收到 {stage.id!r}")
            for step in stage.steps:
                if step.action is not None and step.action.value not in allowed:
                    raise GuideError(f"步骤 {step.id!r} 的动作不在白名单：{step.action}")