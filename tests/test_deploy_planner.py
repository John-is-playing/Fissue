"""``deploy/planner`` 的单测：不联网、不调真模型。

覆盖点
------
* ``collect_repo_context``：README/清单/启动线索进上下文、二进制跳过、
  超长文件截断、深目录不展开、总长不超 ``MAX_CONTEXT_BYTES``。
* ``DeploymentPlanner.plan``：正常路径、仓库字段覆盖、校验失败带错误原文重试、
  一直非法抛 ``PlannerError``、步骤超上限**不截断**、``sudo`` 原样保留。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from fissue.ai.client import Usage
from fissue.config import Platform, RepoConfig
from fissue.deploy.planner import (
    MAX_CONTEXT_BYTES,
    MAX_FILE_BYTES,
    DeploymentPlanner,
    PlannerError,
    collect_repo_context,
)
from fissue.deploy.schema import DeployPlan

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeClient:
    """只实现 ``chat_json`` 的假客户端，按顺序吐出预设回复。"""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def chat_json(
        self,
        messages: Any,
        *,
        purpose: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        default: dict[str, Any] | None = None,
        max_parse_attempts: int = 2,
    ) -> tuple[dict[str, Any], Usage]:
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0) if self.replies else {}
        return copy.deepcopy(reply), Usage(purpose=purpose, model="fake", prompt_tokens=10, completion_tokens=5)


def _repo() -> RepoConfig:
    return RepoConfig(platform=Platform.GITHUB, owner="acme", name="widget")


def _valid_plan() -> dict[str, Any]:
    return {
        "version": 1,
        "repo_slug": "should/be-overwritten",
        "ref": "not-main",
        "commit_sha": "deadbeef",
        "app": {
            "name": "widget",
            "runtime": "python",
            "service_name": "widget",
            "port": 8000,
            "entry": "main.py",
        },
        "steps": [
            {"id": "s1", "kind": "upload", "title": "传产物", "upload": {"src": ".", "dst": "app"}},
            {
                "id": "s2",
                "kind": "exec",
                "title": "装依赖",
                "exec": {"command": "python -m pip install -r requirements.txt", "cwd": "app"},
            },
            {
                "id": "s3",
                "kind": "service",
                "title": "起服务",
                "service": {"action": "restart", "name": "widget.service"},
            },
            {
                "id": "s4",
                "kind": "probe",
                "title": "探活",
                "probe": {"url": "https://widget.example.com/health", "expect_status": 200},
            },
        ],
        "health": [{"url": "https://widget.example.com/health", "expect_status": 200}],
        "artifacts": ["."],
        "notes": "按 README 推断",
    }


@pytest.fixture()
def repo_tree(tmp_path):
    """一个小仓库：README + pyproject + 二进制清单 + 启动线索 + 深目录。"""
    (tmp_path / "README.md").write_text("# Widget\n\n这是一个 FastAPI 写的服务。\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "widget"\nversion = "1.0"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    # 二进制伪装成清单：内容必须被跳过（文件名还会出现在目录树里，这是允许的）
    (tmp_path / "package.json").write_bytes(b'{"\x00binary": true}\nSECRET_BINARY_MARKER\n')
    # 超长文件：必须被截断
    (tmp_path / "requirements-dev.txt").write_text("x" * (MAX_CONTEXT_BYTES // 2), encoding="utf-8")
    # 启动线索：正文超过 200 行，只取前 200 行
    (tmp_path / "main.py").write_text("\n".join(f"line{i}" for i in range(1, 401)), encoding="utf-8")
    # 深层目录：深度超过 3 不应展开
    deep = tmp_path / "deep" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "hidden.py").write_text("DEEP_FILE_MARKER = 1\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# collect_repo_context
# ---------------------------------------------------------------------------


def test_collect_repo_context_includes_key_material(repo_tree) -> None:
    ctx = collect_repo_context(repo_tree, ref="main")

    assert "这是一个 FastAPI 写的服务。" in ctx  # README 正文
    assert "name = \"widget\"" in ctx  # pyproject
    assert "fastapi" in ctx  # requirements
    assert "## 目录树" in ctx
    assert "requirements.txt" in ctx


def test_collect_repo_context_skips_binary(repo_tree) -> None:
    ctx = collect_repo_context(repo_tree)

    assert "SECRET_BINARY_MARKER" not in ctx, "二进制文件（前 4KB 含 \\x00）必须整份跳过"
    assert "binary" not in ctx


def test_collect_repo_context_truncates_long_files(repo_tree) -> None:
    ctx = collect_repo_context(repo_tree)

    assert "已截断" in ctx
    assert "x" * (MAX_CONTEXT_BYTES // 2) not in ctx, "超过 MAX_FILE_BYTES 的文件必须被截断"


def test_collect_repo_context_startup_hint_only_first_200_lines(repo_tree) -> None:
    ctx = collect_repo_context(repo_tree)

    assert "line1\n" in ctx
    assert "line200" in ctx
    assert "line201" not in ctx, "启动线索只取前 200 行"


def test_collect_repo_context_does_not_expand_deep_dirs(repo_tree) -> None:
    ctx = collect_repo_context(repo_tree)

    assert "DEEP_FILE_MARKER" not in ctx
    assert "hidden.py" not in ctx


def test_collect_repo_context_respects_total_budget(tmp_path) -> None:
    # 单个文件会被 MAX_FILE_BYTES 截断，所以用多个大文件把总预算撑爆
    for name in ("README.md", "pyproject.toml", "requirements-1.txt", "requirements-2.txt", "requirements-3.txt"):
        (tmp_path / name).write_text("R" * (MAX_FILE_BYTES + 5000), encoding="utf-8")

    ctx = collect_repo_context(tmp_path)

    assert len(ctx) <= MAX_CONTEXT_BYTES
    assert "上下文已截断" in ctx


def test_collect_repo_context_rejects_missing_root(tmp_path) -> None:
    with pytest.raises(PlannerError):
        collect_repo_context(tmp_path / "nope")


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_plan_happy_path_overrides_repo_fields(repo_tree) -> None:
    client = FakeClient([_valid_plan()])
    planner = DeploymentPlanner(client, _repo())

    plan = await planner.plan(repo_tree, ref="release/1.0", commit_sha="abc123")

    assert isinstance(plan, DeployPlan)
    assert plan.repo_slug == "acme/widget"
    assert plan.ref == "release/1.0"
    assert plan.commit_sha == "abc123"
    assert len(client.calls) == 1
    # 提示词里带上了仓库内容与上限，且写明了真机验证过的几条硬约束
    system = client.calls[0][0]["content"]
    user = client.calls[0][-1]["content"]
    # 真机教训：系统 unit 必须由计划自己安装、service 步骤要提权、venv 前要装 python3-venv
    assert "unit 文件必须由计划自己安装" in system
    assert "service.sudo = true" in system
    assert "python3-venv" in system
    assert "不要发明新字段" in system
    assert "这是一个 FastAPI 写的服务。" in user
    assert "60" in user  # 步骤数上限写进了提示词


async def test_plan_keeps_sudo_command_untouched(repo_tree) -> None:
    """``sudo`` 不改写：偷偷删掉会让审计失真，交给 guard 层拒绝。"""
    raw = _valid_plan()
    raw["steps"].append(
        {
            "id": "s5",
            "kind": "exec",
            "exec": {"command": "sudo systemctl daemon-reload"},
        }
    )
    client = FakeClient([raw])

    plan = await DeploymentPlanner(client, _repo()).plan(repo_tree)

    exec_step = next(s for s in plan.steps if s.id == "s5")
    assert exec_step.exec is not None
    assert exec_step.exec.command == "sudo systemctl daemon-reload"


# ---------------------------------------------------------------------------
# 校验失败 → 带错误原文重试
# ---------------------------------------------------------------------------


async def test_plan_retries_with_validation_error_in_messages(repo_tree) -> None:
    bad = _valid_plan()
    bad["health"] = []  # schema 也会拦，但 planner 要先给出更清楚的话
    client = FakeClient([bad, _valid_plan()])

    plan = await DeploymentPlanner(client, _repo()).plan(repo_tree)

    assert isinstance(plan, DeployPlan)
    assert len(client.calls) == 2
    retry_text = "\n".join(m["content"] for m in client.calls[1])
    assert "缺少 health" in retry_text, "重试时必须把校验错误原文回喂给模型"
    assert "错误原文" in retry_text


async def test_plan_retries_when_schema_rejects_invalid_step(repo_tree) -> None:
    """kind=exec 却填了 upload 规格 → pydantic 校验错误也要回喂。"""
    bad = _valid_plan()
    bad["steps"][1] = {"id": "s2", "kind": "exec", "upload": {"src": ".", "dst": "x"}}
    client = FakeClient([bad, _valid_plan()])

    plan = await DeploymentPlanner(client, _repo()).plan(repo_tree)

    assert isinstance(plan, DeployPlan)
    retry_text = "\n".join(m["content"] for m in client.calls[1])
    assert "s2" in retry_text or "upload" in retry_text


async def test_plan_raises_after_max_repairs(repo_tree) -> None:
    bad = _valid_plan()
    bad["health"] = []
    client = FakeClient([bad, bad, bad])

    with pytest.raises(PlannerError) as excinfo:
        await DeploymentPlanner(client, _repo(), max_repairs=2).plan(repo_tree)

    assert len(client.calls) == 3, "max_repairs=2 意味着首轮 + 2 次修正"
    assert "health" in str(excinfo.value)


async def test_plan_raises_when_model_returns_empty(repo_tree) -> None:
    """chat_json 解析失败只返回 default（空 dict），planner 必须自己判空并重试。"""
    client = FakeClient([{}, _valid_plan()])

    plan = await DeploymentPlanner(client, _repo()).plan(repo_tree)

    assert isinstance(plan, DeployPlan)
    assert len(client.calls) == 2
    assert "JSON" in client.calls[1][-1]["content"]


async def test_plan_does_not_truncate_oversized_step_list(repo_tree) -> None:
    raw = _valid_plan()
    raw["steps"] = [
        {"id": f"e{i}", "kind": "exec", "exec": {"command": f"echo {i}"}} for i in range(5)
    ]
    client = FakeClient([raw])

    with pytest.raises(PlannerError) as excinfo:
        # max_repairs=0：一次就失败，证明没有「截断后再执行」的退路
        await DeploymentPlanner(client, _repo(), max_steps=2, max_repairs=0).plan(repo_tree)

    assert len(client.calls) == 1
    assert "超过上限" in str(excinfo.value)


async def test_plan_rejects_absolute_exec_cwd(repo_tree) -> None:
    raw = _valid_plan()
    raw["steps"][1]["exec"]["cwd"] = "/etc"
    client = FakeClient([raw])

    with pytest.raises(PlannerError) as excinfo:
        await DeploymentPlanner(client, _repo(), max_repairs=0).plan(repo_tree)

    assert "cwd" in str(excinfo.value)


async def test_plan_records_usage(repo_tree) -> None:
    raw_bad = _valid_plan()
    raw_bad["health"] = []
    client = FakeClient([raw_bad, _valid_plan()])
    planner = DeploymentPlanner(client, _repo())

    await planner.plan(repo_tree)

    assert planner.last_usage is not None
    assert planner.last_usage.prompt_tokens == 20  # 两轮累加
    assert planner.last_usage.purpose == "deploy.plan"