"""``scripts/check_docs.py`` 的离线测试。

为什么值得给一个「查死链的脚本」写用例
--------------------------------------
这个脚本的输出是**一条 CI 门禁**：它一旦误报，所有人每次提交都要面对一条红叉，
最后大家的处理方式是「把那行链接删掉」或者「给 CI 加个 continue-on-error」——门禁就此失效。
反过来它一旦漏报，死链会继续烂在文档里，而下一个人根本不知道有这东西存在。
两种失败都比「没有这个脚本」更糟，所以口径要钉死：

* **行号必须是原文行号**：脚本靠「把代码块替换成等长空白」来保住位置，如果哪天有人改成
  直接删掉代码块，报错会指到十行之前——看着像真的，其实是算错了；
* **代码里的链接不算链接**：文档里大量示例把 ``[a](b)`` 写在反引号里；
* **外部链接不查**：CI 不能因为别人的站点抽风而变红；
* **锚点默认只警告**：锚点依赖标题文字，比文件存在更容易误判，不该一上来就阻断。

加载方式与 ``test_release.py`` / ``test_publish_wiki.py`` 一致：脚本不在可 import 的包里，
按**文件路径**加载。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_docs.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # **必须先登记进 sys.modules**：`@dataclass` 处理字段类型时要靠
    # `sys.modules[cls.__module__]` 找当前模块的命名空间；按文件路径加载的模块默认
    # 不在 sys.modules 里，装饰器会当场 `AttributeError: 'NoneType' ...`。
    # 与 `tests/test_publish_wiki.py` 同一个理由。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_docs = _load()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 解析：什么算链接
# ---------------------------------------------------------------------------


def test_external_and_anchor_only_links_are_out_of_scope() -> None:
    """http(s) / mailto / 纯页内锚点不进相对链接清单——前者要联网，后者另有一套规则。"""
    text = (
        "[a](https://example.com/x) [b](http://example.com) "
        "[c](mailto:x@example.com) [d](#local-heading)"
    )

    assert check_docs.iter_links(text, Path("doc.md")) == []


def test_links_inside_code_are_skipped() -> None:
    """行内代码与围栏代码块里的链接是**示例文字**，不是文档结构。"""
    text = """真实 [ok](a.md)

行内示例 `[假](../不存在.md)` 不算。

```
[也不](../同样不存在.md)
```
"""
    targets = [link.target for link in check_docs.iter_links(text, Path("doc.md"))]

    assert targets == ["a.md"]


def test_line_numbers_survive_stripped_code_blocks() -> None:
    """代码块被抹成空白后，后面链接的行号仍指向原文的那一行。

    这条用例是**回护**：``strip_code`` 必须补等长空白而不是删除。写成删除的实现
    在「能跑通」这一点上和正确实现没有区别，只是报错时会指到别处。
    """
    text = """line one

```
code
code
```

[dead](missing.md)
"""
    links = check_docs.iter_links(text, Path("doc.md"))

    assert len(links) == 1
    assert links[0].line == 8, "代码块吃掉 5 行后，链接仍在第 8 行"


# ---------------------------------------------------------------------------
# 锚点口径
# ---------------------------------------------------------------------------


def test_slugify_matches_the_anchors_already_used_in_docs() -> None:
    """抽 ``docs/USAGE.md`` 顶部目录里真在用的两个锚点当回归样本。

    这两条不是编的：``## 6. 配置模板`` 与 ``## 4. 业务规则（config.yaml）`` 就是原文标题，
    对应的锚点分别是 ``#6-配置模板`` 与 ``#4-业务规则-configyaml``。
    """
    assert check_docs.slugify("6. 配置模板") == "6-配置模板"
    assert check_docs.slugify("4. 业务规则（config.yaml）") == "4-业务规则configyaml"
    assert check_docs.slugify("  7. 核心流程（五步）  ") == "7-核心流程五步"


def test_heading_anchors_are_collected_from_atx_headings(tmp_path: Path) -> None:
    page = _write(
        tmp_path / "page.md",
        "# 标题一\n\n正文\n\n## 二级 标题\n\n```\n## 代码里的不是标题\n```\n",
    )

    assert check_docs.anchors_of(page) == {"标题一", "二级-标题"}


# ---------------------------------------------------------------------------
# 体检结果
# ---------------------------------------------------------------------------


def test_missing_target_is_reported(tmp_path: Path) -> None:
    doc = _write(tmp_path / "doc.md", "[x](nope.md)\n")

    problems = check_docs.check_file(doc)

    assert [(p.kind, p.target, p.line) for p in problems] == [("missing", "nope.md", 1)]


def test_existing_target_and_good_anchor_pass(tmp_path: Path) -> None:
    _write(tmp_path / "target.md", "# 存在的标题\n")
    doc = _write(tmp_path / "doc.md", "[ok](target.md#存在的标题) [f](target.md)\n")

    assert check_docs.check_file(doc) == []


def test_bad_anchor_is_only_a_warning_by_default(tmp_path: Path) -> None:
    """锚点写错默认不阻断，``--anchors`` 才升级为错误。

    理由：锚点随标题文字走，改标题是常有的事，而这类误报会让人把门禁关掉；
    死链（文件没了）才是硬错误。
    """
    _write(tmp_path / "target.md", "# 存在的标题\n")
    doc = _write(tmp_path / "doc.md", "[bad](target.md#不存在的标题)\n")

    assert check_docs.main([str(doc)]) == 0
    assert check_docs.main([str(doc), "--anchors"]) == 1


def test_broken_link_makes_the_check_fail(tmp_path: Path) -> None:
    doc = _write(tmp_path / "doc.md", "[x](gone.md)\n")

    assert check_docs.main([str(doc)]) == 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def test_collect_files_deduplicates_overlapping_roots(tmp_path: Path) -> None:
    """`docs` 与 `docs/wiki` 一起传时，同一文件只检查一次。"""
    page = _write(tmp_path / "docs" / "wiki" / "A.md", "# A\n")
    _write(tmp_path / "docs" / "B.md", "# B\n")

    files = check_docs.collect_files([str(tmp_path / "docs"), str(tmp_path / "docs" / "wiki")])

    assert sorted(p.name for p in files) == ["A.md", "B.md"]
    assert page.resolve() in files


def test_repository_docs_are_clean_today() -> None:
    """把真仓库当样本跑一遍：这是本脚本存在的意义，也是它自己的回归测试。

    它会在**下一处死链被写进去的那一刻**变红，而不是等某天有人点开 Wiki 发现 404。
    """
    assert check_docs.main(["--quiet"]) == 0