"""依赖声明的守卫：``pyproject.toml`` 里声明的包，代码要么真的在用，要么在豁免名单里。

为什么需要它
------------
这类漂移**没有任何东西会拦**：声明了却从未被 import 的包会一直躺在依赖里，每个新装环境
的人都要为它付下载时间与供应链面积，而任何地方都不会提示。

实测发现过五个（docker SDK / apscheduler / tenacity / alembic / pydantic-settings）——
各自的原因都不一样（docker 只需要宿主机的 CLI；apscheduler 只在「第三方日志降噪」的
字符串名单里出现过；tenacity/alembic/pydantic-settings 是功能自己实现掉了），但它们有一个
共同点：**没人发现**。

对照着看：``.env.example`` 与「代码真的读了哪些环境变量」之间是有交叉校验的
（见 ``tests/test_env.py``），所以那个方向不会漂；依赖这个方向一直没人守。

判据
----
* 从 ``pyproject.toml`` 读出声明的名字（运行依赖 + dev 两组）；
* 用 AST 扫 ``src/fissue`` 与 ``tests`` 里所有**顶层 import**；
* 声明的每个包要么其 import 名出现在扫描结果里，要么在这个文件顶部 :data:`_EXEMPT` 里
  **带着理由**登记。

豁免名单不能变成垃圾桶：另一条用例会断言名单里的每一项至今仍然被声明着（删掉的包必须
从名单里一起删），于是它只会随着「真的不该被 import」的包增长。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC = REPO_ROOT / "src" / "fissue"
TESTS = REPO_ROOT / "tests"

#: 包名 → 实际 import 名（只在两者不同时才需要登记）。
_IMPORT_NAME = {
    "pyyaml": "yaml",              # import yaml
    "python-dotenv": "dotenv",     # import dotenv
}

#: 声明了但**代码里不会 import** 的包 → 为什么。每一项都必须站得住脚，否则守卫就白设了。
_EXEMPT = {
    "psycopg": (
        "必需但无法静态发现：SQLAlchemy 按 URL 里的 dialect 动态加载驱动"
        "（postgresql+psycopg://），代码里没有 `import psycopg`"
    ),
    "pymysql": (
        "同上：MySQL 走 mysql+pymysql://，由 SQLAlchemy 按 dialect 动态加载，"
        "代码里没有 `import pymysql`"
    ),
    "jinja2": "由 fastapi.templating.Jinja2Templates 使用，代码里不直接 import",
    "pytest-asyncio": "pytest 插件：靠 entry point 加载（asyncio_mode=auto），代码里不 import",
    "pytest-cov": "pytest 插件：靠 entry point 加载（--cov 参数），代码里不 import",
    "pytest-timeout": "pytest 插件：靠 entry point 加载（pyproject 里的 timeout 配置），代码里不 import",
    "ruff": "命令行 linter，只运行不 import",
    "mypy": "命令行类型检查器，只运行不 import",
    "pre-commit": "命令行工具，只在本地提交前钩子里运行，代码里不 import",
}


# ---------------------------------------------------------------------------
# 读声明
# ---------------------------------------------------------------------------


def _name_of(spec: str) -> str:
    """``"psycopg[binary]>=3.1"`` → ``"psycopg"``。"""
    name = re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def _requirements_by_regex(text: str) -> list[str]:
    """低版本 Python 的退路：只认 ``dependencies = [...]`` 与 ``dev = [...]`` 两个数组。

    为什么需要退路：``tomllib`` 是 3.11 才有的，而 CI 矩阵里有 **3.10**
    （``requires-python >= 3.10``）。这不是通用 TOML 解析器，是「够用」的：
    它只认这两个数组里的 ``"名字[extras]>=版本",`` 形式，并且**自带体检**——
    调用方会检查结果里必须有 httpx / fastapi / pytest，免得正则哪天失效后
    悄悄返回一个空列表、把守卫变成摆设。
    """
    specs: list[str] = []
    for header in ("dependencies", "dev"):
        match = re.search(rf"^{header}\s*=\s*\[(.*?)^\s*\]", text, re.M | re.S)
        assert match, f"pyproject.toml 里找不到 {header} 数组——正则失效了"
        specs.extend(re.findall(r'"([^"]+)"', match.group(1)))
    return specs


def _requirements_by_toml(text: str) -> list[str]:
    tomllib = pytest.importorskip("tomllib", reason="3.11+ 才有 tomllib")
    data = tomllib.loads(text)
    project = data["project"]
    return list(project["dependencies"]) + list(project.get("optional-dependencies", {}).get("dev", []))


def _declared_specs() -> list[str]:
    """读真文件：优先 tomllib，3.10 上退回正则。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    try:
        import tomllib as _tomllib  # noqa: F401  —— 只为探测可用性
    except ModuleNotFoundError:
        specs = _requirements_by_regex(text)
    else:
        specs = _requirements_by_toml(text)
    _sanity_check(specs)
    return specs


def _sanity_check(specs: list[str]) -> None:
    """解析结果的体检：挡住「解析器坏了 → 返回空列表 → 守卫静默失效」。"""
    names = {_name_of(spec) for spec in specs}
    for must in ("httpx", "fastapi", "pydantic", "pytest", "respx"):
        assert must in names, f"依赖解析结果里没有 {must}——解析器坏了：{sorted(names)}"
    assert len(names) >= 15, f"声明的依赖只有 {len(names)} 个，明显不对：{sorted(names)}"


def _declared_names() -> set[str]:
    return {_name_of(spec) for spec in _declared_specs()}


# ---------------------------------------------------------------------------
# 扫真实用到的 import
# ---------------------------------------------------------------------------


def _imported_modules() -> set[str]:
    """``src/fissue`` 与 ``tests`` 里所有顶层 import 的模块名（含函数内与 try 里的）。"""
    modules: set[str] = set()
    for root in (SRC, TESTS):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
    return modules


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


def test_every_declared_dependency_is_used_or_exempt() -> None:
    """声明的每个包要么被代码 import，要么在豁免名单里**带着理由**。

    这条挂了通常意味着两种情况之一：往 pyproject 加了一个实际没用上的包（请删掉），
    或者删掉了某个包的最后一次使用（请连同声明一起删）。
    """
    declared = _declared_names()
    imported = _imported_modules()
    unused = sorted(
        name
        for name in declared
        if name not in _EXEMPT
        and _IMPORT_NAME.get(name, name.replace("-", "_")) not in imported
    )
    assert not unused, (
        "这些包声明为依赖，但代码里从没 import 过："
        + "、".join(unused)
        + "。请从 pyproject.toml 删掉；若它确实必需但无法静态发现"
        "（动态加载 / 只在命令行用），就登记进 tests/test_packaging.py 的 _EXEMPT 并写清理由。"
    )


def test_exemption_list_has_no_stale_entries() -> None:
    """豁免名单不能变成垃圾桶：里面的每一项都必须**仍然**是声明的依赖。

    删掉某个包（比如这次删的 docker / apscheduler / tenacity / alembic /
    pydantic-settings）却忘了同步名单，名单就会慢慢变成一份没人读的考古现场。
    """
    stale = sorted(set(_EXEMPT) - _declared_names())
    assert not stale, f"豁免名单里的这些包已经不在依赖声明里了，请从 _EXEMPT 删掉：{stale}"


def test_exemptions_state_a_reason() -> None:
    """每条豁免都要写清理由——没有理由的豁免等于把守卫关掉。"""
    for name, reason in _EXEMPT.items():
        assert len(reason) >= 10, f"豁免 {name} 的理由太短，说不清为什么：{reason!r}"


def test_parser_sees_both_dependency_groups() -> None:
    """解析器要真的读到两组依赖（运行 + dev），而不是只读到一组。"""
    names = _declared_names()
    assert {"httpx", "fastapi", "sqlalchemy", "psycopg", "jinja2"} <= names
    assert {"pytest", "respx", "ruff", "mypy"} <= names
    assert "fissue" not in names, "自己不该出现在依赖列表里"


def test_regex_fallback_agrees_with_tomllib() -> None:
    """两条解析路径必须给出一致的结果（CI 的 3.10 走正则、3.12 走 tomllib）。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    by_regex = {_name_of(spec) for spec in _requirements_by_regex(text)}
    by_toml = {_name_of(spec) for spec in _requirements_by_toml(text)}
    assert by_regex == by_toml


def test_regex_fallback_on_a_small_snippet() -> None:
    """正则路径的单元测试：它在 3.10 上才被走到，别让「只在 CI 某一档才会发现」成为盲区。"""
    snippet = (
        "[project]\n"
        "name = \"x\"\n"
        "dependencies = [\n"
        "    \"httpx>=0.27\",\n"
        "    # 注释里的引号不该被当成依赖\n"
        "    \"truststore>=0.9\",\n"
        "    \"psycopg[binary]>=3.1\",\n"
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\n"
        "    \"pytest>=8.1\",\n"
        "]\n"
    )
    assert {_name_of(spec) for spec in _requirements_by_regex(snippet)} == {
        "httpx",
        "truststore",
        "psycopg",
        "pytest",
    }


def test_no_undeclared_third_party_imports_are_required_at_runtime() -> None:
    """反向检查（只覆盖 src）：运行时代码 import 的第三方模块必须在声明里。

    测试目录**不在**检查范围内——测试可以依赖运行依赖自带的间接包（比如 httpx 带来的
    certifi），那是另一件事。这条守的是「别人 `pip install fissue` 之后跑得起来」。
    """
    stdlib = _stdlib_names()
    declared = _declared_names()
    missing: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            # 行号必须在这里取：`node` 已被上面两条分支窄化，出了分支它就只是 `ast.AST`。
            line = node.lineno
            for name in names:
                if name in stdlib or name == "fissue":
                    continue
                dist = _DIST_OF.get(name, name.replace("_", "-"))
                if dist not in declared:
                    missing.append(f"{path.relative_to(REPO_ROOT)}:{line} import {name}")
    assert not missing, (
        "运行时代码 import 了没有声明的第三方包（`pip install fissue` 会缺它）：\n  "
        + "\n  ".join(sorted(set(missing)))
    )


#: import 名 → 包名（与 :data:`_IMPORT_NAME` 反向，另加几个只有 import 名不同的）。
_DIST_OF = {value: key for key, value in _IMPORT_NAME.items()}


def _stdlib_names() -> frozenset[str]:
    import sys

    return frozenset(sys.stdlib_module_names)


# ---------------------------------------------------------------------------
# 类型标记
# ---------------------------------------------------------------------------


def test_the_package_ships_a_py_typed_marker() -> None:
    """``src/fissue/py.typed`` 必须存在（否则下游用户拿不到类型信息）。

    本项目对 ``src/fissue`` 强制 mypy 且 CI 阻断，但**下游**用户能不能用上这些类型，
    取决于这个空标记文件；没有它，mypy 会把 fissue 当成无类型包，用户侧检查直接失效。
    它又极容易在重构目录时被顺手删掉——而删掉之后**本仓库自己的 CI 完全无感**，
    所以钉一条在这里。
    """
    assert (SRC / "py.typed").is_file(), "缺少 src/fissue/py.typed——下游用户将拿不到类型信息"
