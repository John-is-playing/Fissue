"""既有测试回归门（Regression Gate）。

F2P 只表达**一个**方向：目标用例修复前失败、修复后通过。它表达不了
「别弄坏别的」——即 pass-to-pass 要求。本模块用**仓库自己的既有测试套件**
补齐这个方向（见 ``docs/REGRESSION-GATE.md``）。

两个关键约束：

* **成本随运行时间增长，与 Issue 数无关**：只跑仓库里已有的测试命令，
  不做任何按 Issue 数检索或 LLM 调用。
* **命令不由 LLM 现编**：优先级为 显式配置 → 仓库 ``test_hint`` → 探测。
  本门的价值在于「跑仓库自己的既有测试」，编出来的就不是既有测试了。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import RepoConfig, Settings
from ..logging_setup import get_logger
from ..models import VerifierOutcome, VerifierRun, VerifierSpec
from ..sandbox.forwarder import SandboxManager
from ..sandbox.protocol import ExecRequest, MountSpec
from ..store.repository import Repository
from ..workspace import RepoWorkspace
from .runner import CONTAINER_WORKDIR, classify_outcome

log = get_logger(__name__)

#: 回归门落库用的 stage 标识（与 base/fix 的 F2P 记录区分开）。
REGRESSION_STAGE_BASE = "regression:base"
REGRESSION_STAGE_FIX = "regression:fix"


def regression_stage(stage: str) -> str:
    """把 ``base`` / ``fix`` 映射成落库 stage（``regression:base`` …）。"""
    return f"regression:{stage}"


def resolve_regression_command(
    settings: Settings,
    repo_cfg: RepoConfig,
    workspace: RepoWorkspace,
) -> str | None:
    """按优先级解析既有测试命令；探不到返回 ``None``。

    优先级：``verifier.regression_command`` → ``repo.test_hint`` → 仓库探测
    （``pyproject.toml``/``pytest.ini`` → pytest，``package.json`` → npm test…）。
    """
    explicit = settings.verifier.regression_command
    if explicit and explicit.strip():
        return explicit.strip()
    if repo_cfg.test_hint and repo_cfg.test_hint.strip():
        return repo_cfg.test_hint.strip()
    return workspace.detect_test_command()


def track_existing_test_files(
    workspace: RepoWorkspace,
    *,
    command: str | None,
) -> list[str]:
    """列出「仓库原有测试文件」的相对路径，供回归门跑前移出 / 跑后恢复。

    只收 ``git ls-files`` 里的**已跟踪**文件，天然排除验证器新加、未被跟踪的
    测试文件（pytest 默认会扫到未跟踪文件，故不能只靠 git 列表判断）。
    """
    result = workspace._git(["git", "ls-files"])
    if not result.ok:
        log.warning("git ls-files 失败，回归门将不排除既有测试文件：%s", result.stderr[:200])
        return []
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [rel for rel in files if _looks_like_test(rel, command)]


def _looks_like_test(rel: str, command: str | None) -> bool:
    """判断一个已跟踪文件是否属于「既有测试」范畴（按命令对应的生态）。"""
    cmd = (command or "").lower()
    name = Path(rel).name.lower()
    parts = {p.lower() for p in Path(rel).parts}
    if any(k in cmd for k in ("pytest", "unittest", "tox")) or "pyproject" in cmd:
        return name.startswith("test_") or name.endswith("_test.py") or "tests" in parts
    if any(k in cmd for k in ("npm", "jest", "vitest", "yarn", "pnpm", "node ")):
        return ".test." in name or ".spec." in name or "tests" in parts or "__tests__" in parts
    if cmd.startswith("go "):
        return name.endswith("_test.go")
    if "rspec" in cmd or "rake" in cmd:
        return name.endswith("_spec.rb") or "spec" in parts or "test" in parts
    if "cargo" in cmd:
        return "tests" in parts or name.endswith(".rs")
    if "mvn" in cmd or "gradle" in cmd:
        return "test" in parts or name.startswith("test")
    # 通用兜底
    return name.startswith("test") or name.endswith("_test.py") or "test" in parts


class RegressionGate:
    """在同一工作区上多跑一条「既有测试」命令，产出回归结论。

    不新建工作区：复用调用点已经克隆好并挂载的 workspace。
    """

    def __init__(self, sandbox: SandboxManager, repo: Repository, settings: Settings) -> None:
        self.sandbox = sandbox
        self.repo = repo
        self.settings = settings

    async def run(
        self,
        *,
        item_key: str,
        workspace: RepoWorkspace,
        spec: VerifierSpec,
        repo_cfg: RepoConfig,
        stage: str,
        verifier_id: int,
    ) -> VerifierRun:
        """执行一次回归门。``stage`` 为 ``"base"`` 或 ``"fix"``。"""
        started = time.monotonic()
        stage_tag = regression_stage(stage)

        command = resolve_regression_command(self.settings, repo_cfg, workspace)
        if not command:
            run = VerifierRun(
                verifier_id=verifier_id,
                item_key=item_key,
                stage=stage_tag,
                outcome=VerifierOutcome.SKIPPED,
                error="未找到既有测试命令",
                duration_seconds=round(time.monotonic() - started, 3),
            )
            self.repo.save_verifier_run(run)
            log.info("回归门跳过 %s [%s]：未找到既有测试命令", item_key, stage_tag)
            return run

        req = ExecRequest(
            image=self.settings.sandbox.image,
            command=command,
            workdir=CONTAINER_WORKDIR,
            mounts=[
                MountSpec(source=str(workspace.root), target=CONTAINER_WORKDIR, read_only=False)
            ],
            cpus=self.settings.sandbox.limits.cpus,
            memory_mb=self.settings.sandbox.limits.memory_mb,
            pids=self.settings.sandbox.limits.pids,
            # 回归门有独立预算，通常远大于单测验证器
            timeout_seconds=self.settings.verifier.regression_timeout_seconds,
            network=self.settings.sandbox.network,
            read_only_root=self.settings.sandbox.read_only_root,
            user=self.settings.sandbox.user,
            label=f"{item_key}:{stage_tag}",
        )

        # 验证器写入的测试文件（spec.files）必须临时移出并临时屏蔽既有测试文件：
        # 跑全量套件时，验证器新加的 tests/test_reproduce.py 会被一起收集，
        # 导致 base 阶段因为验证器文件而失败，与「既有测试」语义混淆。
        excluded = self._exclude_workspace_files(workspace, command, spec)
        try:
            exec_result = await self.sandbox.run(req)
        finally:
            self._restore_workspace(workspace, excluded)

        outcome = classify_outcome(exec_result, link_ok=True)
        artifacts_dir = None
        if self.settings.sandbox.keep_artifacts:
            artifacts_dir = str(self._save_artifacts(item_key, stage_tag, exec_result))

        run = VerifierRun(
            verifier_id=verifier_id,
            item_key=item_key,
            stage=stage_tag,
            outcome=outcome,
            exit_code=getattr(exec_result, "exit_code", None),
            stdout=getattr(exec_result, "stdout", "") or "",
            stderr=(getattr(exec_result, "stderr", "") or "")
            + (f"\n[{exec_result.error}]" if getattr(exec_result, "error", None) else ""),
            duration_seconds=round(time.monotonic() - started, 3),
            artifacts_dir=artifacts_dir,
            error=getattr(exec_result, "error", None),
        )
        self.repo.save_verifier_run(run)
        log.info(
            "回归门执行 %s [%s] -> %s（exit=%s，%.1fs）",
            item_key, stage_tag, outcome.value, run.exit_code, run.duration_seconds,
        )
        return run

    # -- 内部 -------------------------------------------------------------

    def _exclude_workspace_files(
        self, workspace: RepoWorkspace, command: str, spec: VerifierSpec
    ) -> dict[str, bytes]:
        """把验证器写入的测试文件临时移出工作区，返回快照供恢复。

        只移出 ``spec.files``（验证器自己的产物），**不动**被跟踪的既有测试：
        后者是仓库原生文件，删掉会让整个套件报 import 错误，反而制造假红。
        """
        snapshot: dict[str, bytes] = {}
        # 用 git ls-files 认定「仓库原生测试」：与验证器文件同名的绝不能移出，
        # 否则等于替仓库删掉了自己的测试（假红的另一种来源）。
        tracked = set(track_existing_test_files(workspace, command=command))
        for rel in set(spec.files):
            if rel in tracked:
                log.warning("验证器文件与既有测试同名，回归门不排除：%s", rel)
                continue
            target = _resolve_in_workspace(workspace.root, rel)
            if target is None or not target.is_file():
                continue
            try:
                snapshot[rel] = target.read_bytes()
                target.unlink()
            except OSError as exc:  # pragma: no cover - 磁盘异常
                log.warning("移出验证器文件失败 %s：%s", rel, exc)
        return snapshot

    def _restore_workspace(self, workspace: RepoWorkspace, snapshot: dict[str, bytes]) -> None:
        """无论成败都恢复被移出的验证器文件。"""
        for rel, data in snapshot.items():
            target = _resolve_in_workspace(workspace.root, rel)
            if target is None:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as exc:  # pragma: no cover - 磁盘异常
                log.warning("恢复验证器文件失败 %s：%s", rel, exc)

    def _save_artifacts(self, item_key: str, stage: str, exec_result: object) -> Path:
        """把回归门输出落盘，便于人工复核。"""
        safe_key = item_key.replace(":", "_").replace("/", "_")
        # stage 形如 ``regression:base``：Windows 下 ':' 不是合法目录名，须替换
        safe_stage = stage.replace(":", "_")
        out_dir = self.settings.data_dir / "artifacts" / safe_key / safe_stage
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "stdout.log").write_text(
            getattr(exec_result, "stdout", "") or "", encoding="utf-8"
        )
        (out_dir / "stderr.log").write_text(
            getattr(exec_result, "stderr", "") or "", encoding="utf-8"
        )
        (out_dir / "meta.json").write_text(
            json.dumps(
                {
                    "exit_code": getattr(exec_result, "exit_code", None),
                    "ok": getattr(exec_result, "ok", None),
                    "timed_out": getattr(exec_result, "timed_out", None),
                    "duration_seconds": getattr(exec_result, "duration_seconds", None),
                    "error": getattr(exec_result, "error", None),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return out_dir


def _resolve_in_workspace(root: Path, rel: str) -> Path | None:
    """把仓库内相对路径解析为工作区内路径；越界返回 ``None``。"""
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


__all__ = [
    "REGRESSION_STAGE_BASE",
    "REGRESSION_STAGE_FIX",
    "RegressionGate",
    "regression_stage",
    "resolve_regression_command",
    "track_existing_test_files",
]
