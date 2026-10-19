"""``.pre-commit-config.yaml`` 与 CI 的**等价性**守卫。

为什么需要它
------------
本地钩子的价值全在「和 CI 跑的是同一件事」。若有人改了 CI 的检查范围（例如把 mypy
从 ``src/fissue tests scripts`` 缩成 ``src/fissue``）却忘了同步钩子，本地就一直是绿灯、
CI 才红——这正是 ``CONTRIBUTING.md`` 记过的那条教训（「没在本地按 CI 的口径跑 mypy」）
的反面。这里把两边钉在一起。

钩子用 ``language: system``，因此它调用的是本环境的 ``python -m ruff`` / ``python -m mypy``，
与 CI 同源、同版本；本文件只断言**命令形状**，不解析版本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: CI 里两条检查命令的原文，改它们时必须同步这里与 .pre-commit-config.yaml。
CI_RUFF = "python -m ruff check src tests scripts"
CI_MYPY = "python -m mypy src/fissue tests scripts"


def _hooks() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    hooks: dict[str, dict[str, Any]] = {}
    for repo in data["repos"]:
        for hook in repo["hooks"]:
            hooks[str(hook["id"])] = hook
    return hooks


def test_pre_commit_config_loads_and_uses_local_system_hooks() -> None:
    """配置能解析，且钩子走 ``language: system``（= 与 CI 同版本，而不是各钉一个 rev）。"""
    data = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    assert data["repos"], "pre-commit 配置里没有任何 repo"
    for repo in data["repos"]:
        assert repo["repo"] == "local", "钩子应当全部是 local + system，避免 rev 与 CI 错位"
        for hook in repo["hooks"]:
            assert hook["language"] == "system"


def test_mypy_hook_covers_the_same_paths_as_ci() -> None:
    """mypy 钩子必须**一次检查整个集合**（与 CI 同口径），而不是按暂存文件逐个跑。

    逐个文件跑会漏掉跨文件的类型关系（多态、重载、import 循环），跑出来的绿并不等于 CI 的绿。
    """
    mypy = _hooks()["mypy"]
    assert mypy["pass_filenames"] is False, "mypy 必须整包检查，不能按暂存文件逐个跑"
    assert str(mypy["entry"]).split()[-3:] == ["src/fissue", "tests", "scripts"]


def test_ruff_hook_runs_the_linter() -> None:
    ruff = _hooks()["ruff-check"]
    assert str(ruff["entry"]).split()[:3] == ["python", "-m", "ruff"]
    assert "check" in str(ruff["entry"])


def test_ci_still_runs_the_same_two_commands() -> None:
    """CI 里的命令原文变了就报错——那时必须同步钩子与上面的常量。"""
    ci_text = CI.read_text(encoding="utf-8")
    assert CI_MYPY in ci_text, "CI 的 mypy 命令变了，请同步 .pre-commit-config.yaml 与 test_pre_commit.py"
    assert CI_RUFF in ci_text, "CI 的 ruff 命令变了，请同步 .pre-commit-config.yaml 与 test_pre_commit.py"
