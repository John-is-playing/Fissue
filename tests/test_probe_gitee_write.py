"""``scripts/probe_gitee_write.py`` 的离线测试。

为什么这个脚本值得测
------------------
它是**判定平台写能力的诊断工具**，而 Gitee 那条「条目级写 404」的关键教训恰恰是：
**判据必须先看状态码、再看文案**——`404`（找不到资源）与 `403`（没有权限）
含义完全不同，而 Gitee 有时会把权限问题也包装成一句 404。

于是脚本里固化下来的这几件事，每一条都是踩过坑换来的，不能靠「应该是这样」交付：

* ``judge()`` 对 401 / 403 / 404 / 429 的判读各不相同——把 403 说成 404 会把人引向
  错误方向（换令牌 vs 申请权限，完全是两件事）；
* ``--number`` 按**字符串**收（Gitee 的编号是 ``IKM265`` 这种形状，按 int 收根本传不进去，
  也会让人误以为「编号映射」是另一个坑，见 BENCHMARK §4.13 / §4.16）；
* 打标签发**裸 JSON 数组**（发 ``{"labels": [...]}`` 会得到 ``400 Problems parsing JSON``——
  那句报错说的是「JSON 解析失败」，不是「字段不认识」，很容易误导）；
* 默认**只读**，真写必须显式 ``--write``。

加载方式与 ``tests/test_publish_wiki.py`` 同理：脚本不在可 import 的包内，按**文件路径**加载。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "probe_gitee_write.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_gitee_write", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 按文件路径加载的模块默认不在 sys.modules 里；先登记，避免后续 import 解析不到。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()


# ---------------------------------------------------------------------------
# 1. judge()：状态码 → 结论。先看码、再看文案。
# ---------------------------------------------------------------------------


def test_judge_treats_2xx_as_success() -> None:
    assert "通" in probe.judge(200)
    assert "通" in probe.judge(201)
    assert "通" in probe.judge(204)


def test_judge_distinguishes_401_403_404_and_429() -> None:
    """这四个码的**处置完全不同**，混成一句就把人引向错误方向。

    * 401 → 令牌无效，换一枚再试；
    * 403 → 令牌 scope 或仓库授权问题（要申请权限，不是换令牌）；
    * 404 → 平台侧拒绝（已知：Gitee 的条目级写 404，而仓库级写是通的）；
    * 429 → 限流，隔几分钟再试（加大频率只会更糟）。
    """
    assert "令牌" in probe.judge(401)
    assert "权限" in probe.judge(403)
    assert "project or enterprise" in probe.judge(404)
    assert "限流" in probe.judge(429)


def test_judge_404_does_not_say_permission() -> None:
    """404 不能被说成「没有权限」——这两件事的处置相反。

    踩坑背景：Gitee 把权限问题也包装成 404，但**我们**这边的判读必须分清
    「找不到这个端点」与「你没有权限」——前者是平台限制，后者要申请权限。
    """
    verdict = probe.judge(404)
    assert "权限" not in verdict


def test_judge_falls_back_to_the_raw_status_code() -> None:
    """认不出的码要说「未归类」并**带上原码**，别只说一句「失败」。"""
    verdict = probe.judge(418)
    assert "418" in verdict
    assert "未归类" in verdict


# ---------------------------------------------------------------------------
# 2. brief()：把响应体压成一行可读文本
# ---------------------------------------------------------------------------


def test_brief_flattens_whitespace_of_a_json_object() -> None:
    out = probe.brief({"message": "project\n  or enterprise"})
    assert "\n" not in out
    assert "project" in out and "enterprise" in out


def test_brief_truncates_and_marks_the_cut() -> None:
    out = probe.brief("x" * 500, limit=100)
    assert len(out) <= 101          # 100 个字符 + 省略号
    assert out.endswith("…")


def test_brief_keeps_short_text_intact() -> None:
    out = probe.brief("短文本", limit=300)
    assert out == "短文本"
    assert "…" not in out


def test_brief_handles_none_and_empty() -> None:
    assert probe.brief(None) == ""
    assert probe.brief("") == ""


# ---------------------------------------------------------------------------
# 3. load_token()：进程环境优先，其次仓库根的 .env
# ---------------------------------------------------------------------------


def test_load_token_prefers_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FISSUE_GITEE_TOKEN", "from-env")
    assert probe.load_token() == "from-env"


def test_load_token_falls_back_to_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """仓库根没有 .env 时（CI 与陌生机器上就是如此）不能抛异常，返回空串。"""
    monkeypatch.delenv("FISSUE_GITEE_TOKEN", raising=False)
    monkeypatch.setattr(probe, "__file__", str(tmp_path / "scripts" / "probe.py"))
    assert probe.load_token() == ""


# ---------------------------------------------------------------------------
# 4. main()：参数校验——**默认只读**，写要显式开
# ---------------------------------------------------------------------------


def test_repo_must_look_like_owner_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--repo`` 少写斜杠要直接报错（argparse → 退出码 2），而不是拿它去请求。"""
    monkeypatch.setenv("FISSUE_GITEE_TOKEN", "t")
    with pytest.raises(SystemExit) as exc:
        probe.main(["--repo", "justname"])
    assert exc.value.code == 2


def test_label_only_requires_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--label-only`` 没给 ``--number`` 要报错——不然它无从下手。"""
    monkeypatch.setenv("FISSUE_GITEE_TOKEN", "t")
    with pytest.raises(SystemExit) as exc:
        probe.main(["--repo", "o/r", "--write", "--label-only"])
    assert exc.value.code == 2


def test_missing_token_exits_with_code_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有令牌要说清「先去配令牌」并给非零退出码，别去发一个注定失败的请求。"""
    monkeypatch.delenv("FISSUE_GITEE_TOKEN", raising=False)
    monkeypatch.setattr(probe, "__file__", "/nonexistent/scripts/probe.py")
    assert probe.main(["--repo", "o/r"]) == 2


def test_number_is_accepted_as_a_non_numeric_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gitee 的编号是 ``IKM265`` 这种形状——**按 int 收根本传不进去**。

    这条曾经被记成「编号映射 bug」（BENCHMARK §4.13），而真相只是参数类型收窄了。
    """
    monkeypatch.setenv("FISSUE_GITEE_TOKEN", "t")
    seen: dict[str, object] = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {"full_name": "o/r", "private": False}

    class _Client:
        def __init__(self, **_kw: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def get(self, url: str, *, params: dict[str, object] | None = None) -> _Resp:
            return _Resp()

        def post(self, url: str, *, params: dict[str, object] | None = None, json: object = None) -> _Resp:
            seen["url"] = url
            seen["json"] = json
            return _Resp()

    monkeypatch.setattr(probe.httpx, "Client", _Client)
    # 只探到「只读」那一步就够验证参数收窄了：--label-only 分支会带上编号。
    rc = probe.main(["--repo", "o/r", "--write", "--label-only", "--number", "IKM265"])

    assert rc == 0
    assert "IKM265" in str(seen.get("url"))


def test_label_probe_posts_a_bare_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """打标签必须发**裸数组**——发 ``{"labels": [...]}`` 会得到 ``400 Problems parsing JSON``。

    那句报错说的是「JSON 解析失败」而不是「字段不认识」，极易让人误判成
    「平台不支持这个接口」。这条断言盯的是**请求体的类型**，不是「请求发出去了」，
    否则把形状改回 dict 也照样过。
    """
    monkeypatch.setenv("FISSUE_GITEE_TOKEN", "t")
    seen: dict[str, object] = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {"full_name": "o/r", "private": False}

    class _Client:
        def __init__(self, **_kw: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def get(self, url: str, *, params: dict[str, object] | None = None) -> _Resp:
            return _Resp()

        def post(self, url: str, *, params: dict[str, object] | None = None, json: object = None) -> _Resp:
            seen["json"] = json
            return _Resp()

    monkeypatch.setattr(probe.httpx, "Client", _Client)
    probe.main(["--repo", "o/r", "--write", "--label-only", "--number", "IKM265"])

    assert isinstance(seen.get("json"), list), "打标签必须发裸 JSON 数组"


def test_write_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """不给 ``--write`` 就**不许**建 Issue——探测默认不能往仓库里留东西。"""
    monkeypatch.setenv("FISSUE_GITEE_TOKEN", "t")
    posted: list[str] = []

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"full_name": "o/r", "private": False}

    class _Client:
        def __init__(self, **_kw: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def get(self, url: str, *, params: dict[str, object] | None = None) -> _Resp:
            return _Resp()

        def post(self, url: str, *, params: dict[str, object] | None = None, json: object = None) -> _Resp:
            posted.append(url)
            return _Resp()

        def patch(self, url: str, *, params: dict[str, object] | None = None, json: object = None) -> _Resp:
            posted.append(url)
            return _Resp()

    monkeypatch.setattr(probe.httpx, "Client", _Client)
    rc = probe.main(["--repo", "o/r"])

    assert rc == 0
    assert posted == [], "默认只读：不该发出任何写请求"
