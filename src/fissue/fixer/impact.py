"""改动影响分析：把一份 diff 折成「改了什么、风险在哪」。

为什么需要它
------------
「只出建议」和「草稿 PR」两档的存在意义就是**让人先看清楚再决定**。可 diff 本身
是几十行加号减号，非工程用户看不出哪一行危险。真正该被指出来的只有几类信号：

* **删断言**——把 `assert` 删掉测试当然会绿，但那是让问题消失，不是修好；
* **只改测试、没改源码**——同样的味道；
* **动 CI 配置**——影响面超出这个功能；
* **改动面过大**——一次改十几个文件，评审成本高、回滚难。

判定纯粹基于 diff 文本（正则 + 计数），**不调模型**：风险提示必须稳定可复现，
不能因为换了模型就把"高风险"读成"低风险"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 测试文件判定：tests/ 目录、test_*.py、*_test.go、*.test.ts / *.spec.ts。
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/"
    r"|(^|/)test_[^/]+\.\w+$"
    r"|_test\.\w+$"
    r"|\.(test|spec)\.\w+$",
    re.IGNORECASE,
)

#: CI / 流水线配置文件：改这些的东西影响面往往超出"修一个 bug"。
_CI_HINTS = (
    ".github/workflows/",
    ".gitlab-ci",
    "jenkinsfile",
    ".circleci/",
    "azure-pipelines",
    ".travis.yml",
    "ci/",
)

#: 依赖 / 构建 / 容器配置。
_CONFIG_FILES = {
    "pyproject.toml",
    "package.json",
    "cargo.toml",
    "go.mod",
    "go.sum",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "makefile",
    "dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
}

#: 断言的各种写法。刻意宽松：宁可误报一条"请确认"，也不要漏掉"删断言换绿"。
_ASSERT = re.compile(
    r"\b(assert|assert_eq|assertEqual|assert_eq!|expect\s*\(|should\(|toBe|toEqual|"
    r"pytest\.raises|raise\s+AssertionError)\b"
)

#: 风险等级的展示文案与配色类（模板直接用）。
RISK_LABELS: dict[str, str] = {"low": "低风险", "medium": "中风险", "high": "高风险"}
RISK_CLASSES: dict[str, str] = {"low": "ok", "medium": "warn", "high": "bad"}

_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Impact:
    """一次修复的改动概览与风险判定。"""

    files: int = 0
    added: int = 0
    removed: int = 0
    test_files: int = 0
    source_files: int = 0
    #: 被删掉的、含断言的代码行数
    removed_asserts: int = 0
    touched_ci: bool = False
    touched_config: bool = False
    risk: str = "low"
    reasons: list[str] = field(default_factory=list)

    @property
    def risk_label(self) -> str:
        return RISK_LABELS.get(self.risk, self.risk)

    @property
    def risk_class(self) -> str:
        return RISK_CLASSES.get(self.risk, "stale")


def analyze(diff: str, *, changed_files: list[str] | None = None) -> Impact:
    """分析 diff（标准 unified diff）。``changed_files`` 用于 diff 为空时兜底。"""
    current = ""
    files: set[str] = set()
    added = removed = removed_asserts = 0
    test_files: set[str] = set()
    source_files: set[str] = set()
    test_changed = source_changed = 0
    touched_ci = touched_config = False

    for line in (diff or "").splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            # `+++ b/path`：新文件路径；`--- /dev/null` 表示新增，不算文件名
            path = line[4:].strip()
            if path.startswith(("a/", "b/")):
                path = path[2:]
            if path and path != "/dev/null":
                current = path
                files.add(path)
            continue
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3].removeprefix("b/")
                current = path
                files.add(path)
            continue
        if not line or line[0] not in "+-":
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue

        is_test = bool(_TEST_PATH.search(current or ""))
        lowered = (current or "").lower()
        if any(hint in lowered for hint in _CI_HINTS):
            touched_ci = True
        if lowered.rsplit("/", 1)[-1] in _CONFIG_FILES:
            touched_config = True

        if line[0] == "+":
            added += 1
        else:
            removed += 1
            if _ASSERT.search(line[1:]):
                removed_asserts += 1

        if is_test:
            test_files.add(current)
            test_changed += 1
        else:
            source_files.add(current)
            source_changed += 1

    if not files and changed_files:
        files = set(changed_files)
        test_files = {p for p in changed_files if _TEST_PATH.search(p)}
        source_files = files - test_files

    risk = "low"
    reasons: list[str] = []

    def bump(level: str, why: str) -> None:
        nonlocal risk
        if _ORDER[level] > _ORDER[risk]:
            risk = level
        reasons.append(why)

    if removed_asserts:
        bump(
            "high",
            f"删除了 {removed_asserts} 行断言——这通常是在让测试变绿，而不是修好问题；"
            "请确认这些断言确实多余",
        )
    if touched_ci:
        bump("high", "改动了 CI / 流水线配置，影响面超出这个功能本身")
    if test_files and not source_files:
        bump("high", "只改了测试文件、没动源码——修复可能只是把测试改到能过")
    elif test_files and test_changed > source_changed:
        bump("medium", "测试改动比源码改动还多，建议先确认改的是测试还是实现")
    if touched_config:
        bump("medium", "改动了依赖 / 构建配置")
    if len(files) > 5:
        bump("medium", f"一次改了 {len(files)} 个文件，评审与回滚成本都偏高")
    if source_changed > 300:
        bump("medium", f"源码改动 {source_changed} 行，超出「小修」的常见范围")
    if not reasons:
        reasons.append("只动了源码，未触碰测试 / CI / 依赖")

    return Impact(
        files=len(files),
        added=added,
        removed=removed,
        test_files=len(test_files),
        source_files=len(source_files),
        removed_asserts=removed_asserts,
        touched_ci=touched_ci,
        touched_config=touched_config,
        risk=risk,
        reasons=reasons,
    )
