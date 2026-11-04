"""AI 读仓库 → 部署计划。

分工（见 ``docs/DEPLOY.md`` §1 §2 §5）
------------------------------------
* 模型只产出**结构化计划**（``DeployPlan``），不产出任意 shell：它想做的事
  必须落成白名单动作（upload / write_file / exec / service / probe）之一。
* 本模块只负责「收集上下文 → 组提示词 → 解析 → 校验」。执行与危险命令拦截
  在 ``runner`` / ``guard``，这里**不**替模型改写命令（例如把 ``sudo`` 去掉）：
  偷偷改写会让审计失真，用户会以为 AI 没要求 sudo。
* 校验失败时把**校验错误原文**喂回模型修正，最多 ``max_repairs`` 轮；
  仍不合法就抛 :class:`PlannerError`——宁可不出计划，也不执行半份计划。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..ai.client import Usage
from ..errors import FissueError
from ..logging_setup import get_logger
from .schema import DeployPlan, DeployStep, ExecSpec, UploadSpec

log = get_logger(__name__)


class PlannerError(FissueError):
    """无法产出合法计划（模型输出不合 schema、或仓库里没有可部署的东西）。"""


#: 收集仓库上下文时的硬上限，防止把整个仓库塞进提示词把预算烧光
MAX_CONTEXT_BYTES: int = 120_000
MAX_FILE_BYTES: int = 40_000

#: 目录树深度与条数上限（深度 0 = 仓库根）
MAX_TREE_DEPTH = 3
MAX_TREE_ENTRIES = 200
#: 启动线索（服务/入口）只取前若干行——这些文件的正文通常很长，结论只在前几十行
MAX_STARTUP_LINES = 200

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
    ".next",
    "target",
    "site-packages",
}

#: 依赖/构建清单（按「信息量」从高到低排）
_MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Makefile",
    "Procfile",
    ".env.example",
)

#: 锁文件体积大、信息密度低，放在最后（预算不够时先牺牲它们）
_LOCKFILES = ("pnpm-lock.yaml", "yarn.lock", "package-lock.json")

#: 项目根/``src`` 下常见的启动入口
_ENTRY_NAMES = ("manage.py", "main.py", "app.py", "asgi.py", "wsgi.py")
_ENTRY_GLOBS = ("src/main.*", "src/app.*", "src/asgi.py", "src/wsgi.py")


# ---------------------------------------------------------------------------
# 仓库上下文收集
# ---------------------------------------------------------------------------


def _read_text(
    path: Path,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    max_lines: int | None = None,
) -> str | None:
    """读取文本文件；二进制（前 4KB 有 ``\\x00``）返回 ``None``。

    不依赖外部库，也不整份读进内存：先看前 4KB 判二进制，再最多读到
    ``max_bytes``，超出部分标一句截断说明。
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(4096)
            if b"\x00" in head:
                return None
            rest = fh.read(max_bytes)
    except OSError:
        return None

    truncated = len(head) + len(rest) >= max_bytes
    raw = head + rest
    if truncated and len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True
    text = raw.decode("utf-8", errors="replace")

    if max_lines is not None:
        lines = text.splitlines()
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines])
            truncated = True

    if truncated:
        text += f"\n…（已截断：只保留前 {max_bytes} 字节" + (
            f"/ 前 {max_lines} 行" if max_lines else ""
        ) + "）"
    return text


def _render_tree(base: Path) -> str:
    """深度 <= MAX_TREE_DEPTH、最多 MAX_TREE_ENTRIES 条的目录树。"""
    lines: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        try:
            rel = current.relative_to(base)
        except ValueError:  # pragma: no cover - os.walk 不会给 base 之外的路径
            continue
        depth = 0 if str(rel) == "." else len(rel.parts)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        if depth >= MAX_TREE_DEPTH:
            dirnames[:] = []
        for name in sorted(dirnames):
            lines.append("/".join((*rel.parts, name)) + "/" if rel.parts else name + "/")
        for name in sorted(filenames):
            lines.append("/".join((*rel.parts, name)) if rel.parts else name)
        if len(lines) >= MAX_TREE_ENTRIES:
            lines = lines[:MAX_TREE_ENTRIES]
            lines.append(f"…（目录树已截断：最多 {MAX_TREE_ENTRIES} 条）")
            break
    return "\n".join(lines) if lines else "（空）"


def _glob_files(base: Path, patterns: Sequence[str]) -> list[Path]:
    """按顺序 glob，去重，只保留文件。"""
    seen: list[Path] = []
    for pattern in patterns:
        for path in sorted(base.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            rel_parts = path.relative_to(base).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            seen.append(path)
    return seen


def _file_block(label: str, path: Path, text: str, *, rel: str) -> str:
    return f"## {label}：{rel}\n```\n{text}\n```"


def collect_repo_context(root: str | Path, *, ref: str | None = None) -> str:
    """把「判断这是个什么项目、该怎么部署」所需的最小上下文拼成一段文本。

    必须包含（存在才取）：

    - 目录树（深度<=3，跳过 .git/node_modules/__pycache__/.venv/dist/build，最多 200 条）
    - README*（截断到 MAX_FILE_BYTES）
    - 依赖与构建清单：pyproject.toml / setup.py / setup.cfg / requirements*.txt /
      package.json / pnpm-lock.yaml / yarn.lock / go.mod / Cargo.toml /
      Dockerfile / docker-compose.y*ml / Makefile / Procfile / .env.example
    - 明显的启动线索：manage.py、main.py、app.py、src/main.*、asgi.py、wsgi.py、
      nginx/*.conf、systemd/*.service、*.service 的**前 200 行**

    总长度不超过 MAX_CONTEXT_BYTES（超出按「先重要后次要」的顺序截断并在尾部标注）。
    二进制文件一律跳过（用前 4KB 里是否有 ``\\x00`` 判断）。
    """
    base = Path(root)
    if not base.is_dir():
        raise PlannerError(f"仓库目录不存在或不是目录：{base!s}")

    blocks: list[str] = [f"# 仓库上下文（ref={ref or '未指定'}）"]

    # 1) README（信息密度最高，最先占预算）
    readmes = [p for p in sorted(base.iterdir()) if p.is_file() and p.name.lower().startswith("readme")]
    for path in readmes:
        text = _read_text(path)
        if text is not None:
            blocks.append(_file_block("README", path, text, rel=path.name))

    # 2) 依赖与构建清单
    for name in _MANIFESTS:
        path = base / name
        if path.is_file():
            text = _read_text(path)
            if text is not None:
                blocks.append(_file_block("依赖/构建清单", path, text, rel=name))
    for path in _glob_files(base, ["requirements*.txt", "docker-compose.y*ml"]):
        if path.name in _MANIFESTS:
            continue
        text = _read_text(path)
        if text is not None:
            blocks.append(_file_block("依赖/构建清单", path, text, rel=path.name))

    # 3) 启动线索（只要前 200 行：入口与配置的长尾对判断部署方式没用）
    entry_paths: list[Path] = []
    for name in _ENTRY_NAMES:
        cand = base / name
        if cand.is_file():
            entry_paths.append(cand)
    entry_paths.extend(_glob_files(base, _ENTRY_GLOBS))
    entry_paths.extend(_glob_files(base, ["nginx/*.conf", "systemd/*.service", "*.service"]))
    seen_entries: set[Path] = set()
    for path in entry_paths:
        if path in seen_entries:
            continue
        seen_entries.add(path)
        text = _read_text(path, max_lines=MAX_STARTUP_LINES)
        if text is not None:
            blocks.append(
                _file_block("启动线索", path, text, rel=path.relative_to(base).as_posix())
            )

    # 4) 目录树（每字节信息量低，但缺了它模型不知道东西在哪）
    blocks.append(f"## 目录树（深度<={MAX_TREE_DEPTH}）\n{_render_tree(base)}")

    # 5) 锁文件（最后）
    for name in _LOCKFILES:
        path = base / name
        if path.is_file():
            text = _read_text(path)
            if text is not None:
                blocks.append(_file_block("锁文件", path, text, rel=name))

    marker = f"\n…（上下文已截断：超过 MAX_CONTEXT_BYTES={MAX_CONTEXT_BYTES} 上限，后面更次要的内容未包含）"
    budget = MAX_CONTEXT_BYTES - len(marker)

    out: list[str] = []
    used = 0
    truncated = False
    for block in blocks:
        piece = ("\n\n" if out else "") + block
        if used + len(piece) <= budget:
            out.append(piece)
            used += len(piece)
            continue
        room = budget - used
        if room > 0:
            out.append(piece[:room])
            used += room
        truncated = True
        break

    text = "".join(out)
    if truncated:
        text += marker
    log.debug("仓库上下文 %d 字符（ref=%s，截断=%s）", len(text), ref, truncated)
    return text


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

SYSTEM_DEPLOY_PLANNER = """你是资深运维工程师。你的任务是读懂给定仓库，产出一份**可校验、可执行、可回滚**的部署计划。

铁律：
1. 输出必须是一个**合法 JSON 对象**，不要任何解释、前言或 Markdown 代码块。
2. 字段严格照下面的 schema，**不要发明新字段**；schema 之外的键会被忽略，等于白写。
3. **sudo 只在必要时用，且必须显式标注**。远端默认以普通用户执行：
   - `service` 步骤若操作的是**系统级 unit**（`/etc/systemd/system/`），必须同时置
     `service.sudo = true`，并在 `app.service_sudo = true`。否则 `systemctl` 会以
     "Interactive authentication required" 失败——这是真机验证踩到的第一个死法。
   - 需要 root 的命令（装系统包、写 `/etc`）在 `exec.command` 里写 `sudo -n ...`；
     整份计划是否允许提权由 `deploy.allow_dangerous` 决定，写成 `sudo -n` 才能
     在免密环境下不卡在密码提示上。
4. **unit 文件必须由计划自己安装**，不能假设机器上已有：
   先 `write_file` 把 unit 模板写进 release（相对路径，如 `deploy/app.service`），
   再 `exec: sudo -n install -m 644 deploy/app.service /etc/systemd/system/app.service`，
   然后才是 `service: daemon-reload` / `enable` / `restart`。少了安装这一步，
   `restart` 会报 "Unit ... not found"。
5. **解释器与系统包前置要显式检查**：Debian/Ubuntu 默认没有 `ensurepip`，
   直接 `python3 -m venv` 会失败；先 `sudo -n apt-get install -y python3.X-venv`
   （版本号按远端 Python 版本，拿不准就装 `python3-venv`）。
6. 部署产物放在 release 目录（`releases/<时间戳>/`）里，通过 `current` 软链接原子切换；
   `.env`、上传目录、SQLite、**虚拟环境**等需要跨版本保留的东西放 `shared/`。
7. `health` 里的探针**从控制端发起**，必须能被控制端访问到：
   优先给 HTTP `url`（如 `http://<主机>:<端口>/`），给不出 url 才退到 `port`（TCP 连通）。
   不要用 `http://127.0.0.1:...` 这类只有远端自己能访问的地址。服务必须监听 `0.0.0.0`，
   否则控制端探针连不上。
8. 每个步骤都要可判定（`check`）与可回滚（`rollback`，通常是停回服务）；
   宁可步骤多一点、语义清楚，也不要写一条大而全的 `bash -c`。
9. 仓库里信息不足时，按最常见的惯例给出计划并在 `notes` 里说明假设，**不要编造不存在的文件**。
10. **打包元数据引用到的文件也要上传**。Python 项目的 `pyproject.toml` 常写着
    `readme = "README.md"`、`license = { file = "LICENSE" }`、或 `[tool.hatch.version] path = "..."`；
    这些文件不跟着上传，`pip install .` 会在"生成元数据"阶段失败（真机踩过：
    缺 `README.md` 导致 hatchling `OSError`，报错埋在构建日志里，前面十几步白跑）。
    上传 `pyproject.toml` 时，把上面这些文件一起传。
11. **启动必需的值写进 `required_inputs`，不要往 `app.env` 里填假占位符**。
    像 `FISSUE_LLM_API_KEY=sk-REPLACE-ME` 这种写法会让服务启动时直接退出
    （`ConfigError: 缺少 LLM API Key`），而报错要到第 20 步的探针才浮现。
    正确做法：`app.env` 里**不写**这个键，在 `required_inputs` 里声明
    `{key, reason, required: true, example}`，由人补齐后再部署。"""

#: 手写的 schema 说明（与 ``deploy/schema.py`` 保持一致；改契约请同步改这里与 DEPLOY.md）
SCHEMA_GUIDE = """\
DeployPlan:
  version: int = 1
  repo_slug: str            # 形如 owner/name，控制端会覆盖成真实仓库
  ref: str = "main"
  commit_sha: str | null
  app: AppSpec
  steps: [DeployStep]       # 按执行顺序
  health: [ProbeSpec]       # 至少一条，部署收尾必须全绿
  artifacts: [str]          # 要上传的本地路径（相对仓库根）
  required_inputs: [RequiredInput]   # 启动前置项，见下
  notes: str

RequiredInput:
  key: str                  # 环境变量名，例如 FISSUE_LLM_API_KEY
  reason: str = ""          # 一句话说明"拿来做什么"
  required: bool = true     # false = 有更好、没有也能跑
  example: str | null       # 取值形状提示，例如 sk-...

AppSpec:
  name: str                 # 必填，仅 [A-Za-z0-9._-]
  runtime: str = "unknown"  # python | node | static | docker | unknown
  service_name: str | null
  port: int | null          # 1-65535
  entry: str | null
  env: {str: str}

DeployStep:
  id: str                   # 必填，<=64 字符，全计划唯一
  kind: str                 # upload | write_file | exec | service | probe
  title: str = ""
  check: CheckSpec | null
  rollback: [DeployStep]    # 失败后补做的动作，不能再嵌套 rollback
  # 按 kind **只填对应的一个**规格字段，填别的会被判非法：
  upload: {src: str, dst: str}          # dst 相对 release 目录，不得以 / 开头、不得含 ..
  write: {path: str, content: str, mode: str = "0644", allow_outside_release: bool = false}
  exec: {command: str, cwd: str | null, timeout: int | null}
  service: {action: str, name: str}     # action: daemon-reload|enable|start|restart|stop；name 仅 [A-Za-z0-9_.@-]
  probe: {url: str | null, port: int | null, expect_status: int | null, expect_contains: str | null, timeout: float = 10.0}

CheckSpec:
  expect_exit_code: int = 0
  output_contains: str | null
  output_excludes: str | null

ProbeSpec（url 与 port **二选一**，不能都给也不能都不给）:
  url: str | null           # 只支持 http:// 或 https://
  port: int | null          # 1-65535
  expect_status: int | null # 100-599
  expect_contains: str | null
  timeout: float = 10.0"""


def deploy_plan_prompt(
    *,
    context: str,
    repo_slug: str,
    ref: str = "main",
    commit_sha: str | None = None,
    notes: str = "",
    app_root: str = "/opt/fissue-deploy",
    max_steps: int = 60,
) -> list[dict[str, str]]:
    """组出「读仓库 → 部署计划」的 messages（纯函数，便于单测与复用）。"""
    commit_line = f"目标提交：{commit_sha}" if commit_sha else "目标提交：未指定（按分支最新提交）"
    extra = f"\n用户补充说明：{notes}" if notes else ""
    user = f"""请为下面这个仓库生成部署计划。

仓库：{repo_slug}
分支：{ref}
{commit_line}
远端根目录：{app_root}/<owner>-<name>/
  每次部署的产物放 {app_root}/<owner>-<name>/releases/<时间戳>/
  跨版本保留的数据放 {app_root}/<owner>-<name>/shared/
  {app_root}/<owner>-<name>/current 是指向当前 release 的软链接
步骤数上限：{max_steps} 条（**超过这一上限的计划会被直接拒绝，不做截断**）{extra}

输出 JSON 的字段定义（必须严格遵守，不要新增字段）：
{SCHEMA_GUIDE}

下面是仓库上下文（目录树 + README + 依赖/构建清单 + 启动线索）：

{context}

请只输出一个 JSON 对象。"""

    return [
        {"role": "system", "content": SYSTEM_DEPLOY_PLANNER},
        {"role": "user", "content": user},
    ]


def deploy_plan_repair_prompt(previous: Any, errors: str) -> dict[str, str]:
    """把校验错误原文回喂给模型，要求它修正上一版计划。"""
    try:
        rendered = json.dumps(previous, ensure_ascii=False)[:8000]
    except (TypeError, ValueError):
        rendered = str(previous)[:8000]
    return {
        "role": "user",
        "content": (
            "上一版计划没有通过校验，错误原文如下：\n"
            f"{errors}\n\n"
            "上一版（JSON）：\n"
            f"{rendered}\n\n"
            "请**修正这些错误**，重新输出完整的一个 JSON 对象："
            "不要解释、不要只给改动片段、不要新增 schema 之外的字段。"
        ),
    }


# ---------------------------------------------------------------------------
# 计划生成与校验
# ---------------------------------------------------------------------------


def _iter_all_steps(steps: Sequence[DeployStep]) -> list[DeployStep]:
    """展开 rollback 里的步骤（回滚动作同样是会被执行的命令，必须一起校验）。"""
    out: list[DeployStep] = []
    for step in steps:
        out.append(step)
        if step.rollback:
            out.extend(_iter_all_steps(step.rollback))
    return out


def _bad_path(value: str | None) -> bool:
    """绝对路径或含 ``..`` 的都判非法（远端是 POSIX，但 Windows 风格也一并挡住）。"""
    if not value:
        return False
    text = value.replace("\\", "/")
    if text.startswith("/") or ".." in text.split("/"):
        return True
    # "C:/..." 这类盘符路径在目标机上不是相对路径
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


class DeploymentPlanner:
    """把「仓库内容 + 一次 LLM 调用」变成一份可执行前的合法 :class:`DeployPlan`。"""

    def __init__(
        self,
        client: Any,              # LLMClient（只要实现 chat_json 即可，测试里用假对象）
        repo: Any,                # RepoConfig
        *,
        app_root: str = "/opt/fissue-deploy",
        max_steps: int = 60,
        max_repairs: int = 2,
    ) -> None:
        self.client = client
        self.repo = repo
        self.app_root = app_root
        self.max_steps = max_steps
        self.max_repairs = max(0, max_repairs)
        #: 最近一次 ``plan()`` 各轮调用累计的用量（供上层落库/审计）
        self.last_usage: Usage | None = None

    # -- 校验 -------------------------------------------------------------

    def _validate(self, raw: dict[str, Any], *, ref: str, commit_sha: str | None) -> DeployPlan:
        # health 先给出人话错误：schema 也会拦，但那句「model validation failed」不够清楚
        health = raw.get("health")
        if not isinstance(health, list) or not health:
            raise PlannerError(
                "计划缺少 health 健康探针：部署收尾必须有至少一条**从控制端发起**的探针"
                "（给出外部可达的 url 或服务监听的 port），否则「成功」没有判据"
            )

        steps_raw = raw.get("steps")
        if isinstance(steps_raw, list) and len(steps_raw) > self.max_steps:
            # 不截断：截断等于执行半份计划，比失败更危险
            raise PlannerError(
                f"计划步骤数 {len(steps_raw)} 超过上限 {self.max_steps}："
                "请合并/删减步骤后重新输出（本模块不会替你截断）"
            )

        try:
            plan = DeployPlan.load(raw)
        except ValidationError as exc:
            raise PlannerError(f"计划不合 schema：{exc}") from exc

        if not plan.health:
            raise PlannerError("计划缺少 health 健康探针")
        if len(plan.steps) > self.max_steps:
            raise PlannerError(
                f"计划步骤数 {len(plan.steps)} 超过上限 {self.max_steps}（不做截断）"
            )

        for step in _iter_all_steps(plan.steps):
            spec = step.spec
            if isinstance(spec, UploadSpec) and _bad_path(spec.dst):
                raise PlannerError(
                    f"步骤 {step.id!r} 的 upload.dst 必须是 release 内的相对路径：{spec.dst!r}"
                )
            if isinstance(spec, ExecSpec) and _bad_path(spec.cwd):
                raise PlannerError(
                    f"步骤 {step.id!r} 的 exec.cwd 必须是相对路径（不得绝对路径或含 ..）：{spec.cwd!r}"
                )
            # sudo 不做任何改写：原样保留，交给 guard 层拒绝（悄悄改写会让审计失真）

        plan.repo_slug = self.repo.slug
        plan.ref = ref
        plan.commit_sha = commit_sha
        return plan

    # -- 主流程 -----------------------------------------------------------

    async def plan(
        self,
        root: str | Path,
        *,
        ref: str = "main",
        commit_sha: str | None = None,
        notes: str = "",
    ) -> DeployPlan:
        """产出并校验计划（流程见模块文档与 ``docs/DEPLOY.md`` §2）。"""
        context = collect_repo_context(root, ref=ref)
        if not context.strip():
            raise PlannerError(f"仓库里没有可用于判断部署方式的内容：{root!s}")

        messages = deploy_plan_prompt(
            context=context,
            repo_slug=self.repo.slug,
            ref=ref,
            commit_sha=commit_sha,
            notes=notes,
            app_root=self.app_root,
            max_steps=self.max_steps,
        )

        last_error = ""
        last_raw: Any = None
        usage_total: Usage | None = None

        for attempt in range(self.max_repairs + 1):
            raw, usage = await self.client.chat_json(
                messages, purpose="deploy.plan", default={}
            )
            usage_total = replace(usage) if usage_total is None else _merge(usage_total, usage)
            last_raw = raw

            if not isinstance(raw, dict) or not raw:
                last_error = (
                    "模型没有返回可解析的 JSON 对象（模型侧返回空/截断/非 JSON）。"
                    "请重新输出**完整闭合**的 JSON 对象。"
                )
            else:
                try:
                    plan = self._validate(raw, ref=ref, commit_sha=commit_sha)
                except PlannerError as exc:
                    last_error = str(exc)
                    log.warning("部署计划第 %d/%d 轮未通过校验：%s", attempt + 1, self.max_repairs + 1, last_error)
                else:
                    self.last_usage = usage_total
                    return plan

            if attempt < self.max_repairs:
                # 把校验错误原文 + 上一版一起回喂：只说「重来一次」模型会原样再犯
                messages = [*messages, deploy_plan_repair_prompt(last_raw, last_error)]

        self.last_usage = usage_total
        raise PlannerError(
            f"部署计划在 {self.max_repairs + 1} 轮后仍不合法，最后一次错误：{last_error[:2000]}"
        )


def _merge(total: Usage, part: Usage) -> Usage:
    """累加用量（不 import client 的私有 ``_merge_usage``，保持只依赖公开形状）。"""
    total.prompt_tokens += getattr(part, "prompt_tokens", 0)
    total.completion_tokens += getattr(part, "completion_tokens", 0)
    total.cost_usd += getattr(part, "cost_usd", 0.0)
    total.success = total.success and getattr(part, "success", True)
    return total