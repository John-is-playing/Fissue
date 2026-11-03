"""UI 重写后的**结构性**回归测试。

没有浏览器可用（本机 Edge 无头模式完全无输出），所以这里不测"好不好看"，
只测那些"一旦破坏就会静默坏掉"的约束：

1. 每个页面都能渲染，且都带上了 token 样式与主题开关；
2. **零外部请求**：字体自托管，内网/离线部署不会退化成默认字体或白屏；
3. 模板里用到的类在 CSS 里都有定义（重写后最容易漏的一类问题——
   类名拼错不会报错，只是那一块没有样式）；
4. 深色主题对浅色主题的**变量全覆盖**（漏一个就表现为"切深色后某块还是白的"）；
5. 组件层不写死颜色（只允许 printing 那一处例外）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fissue.web.app import create_app

TEMPLATES = Path("src/fissue/web/templates")
STATIC = Path("src/fissue/web/static")

#: 页面清单：(路径, 断言里应出现的标记文本)
PAGES = [
    ("/", None),
    ("/items", None),
    ("/pipeline", None),
    ("/jobs", None),
    ("/repos", None),
    ("/settings", None),
    ("/ops", None),
    ("/ops/guide?repo_slug=acme/web", None),
    ("/setup", None),
]


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def _css() -> str:
    return "".join((STATIC / name).read_text(encoding="utf-8") for name in ("tokens.css", "app.css"))


# ---------------------------------------------------------------------------
# 1. 渲染：每页都带 token 样式与主题开关
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,_", PAGES)
def test_pages_render_with_theme(client, path, _):
    r = client.get(path, follow_redirects=False)
    assert r.status_code in (200, 303), f"{path} → {r.status_code}"
    if r.status_code != 200:
        return
    assert "tokens.css" in r.text, f"{path} 没有引入 token 样式（组件层的变量会全部落空）"
    assert "theme.js" in r.text
    assert 'data-theme' in r.text


def test_login_page_uses_design_system(client):
    r = client.get("/login", follow_redirects=False)
    assert r.status_code in (200, 303)
    if r.status_code == 200:
        # 登录页是独立布局，但同样要带 token 与主题脚本
        assert "tokens.css" in r.text
        assert "theme.js" in r.text


# ---------------------------------------------------------------------------
# 2. 零外部请求
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["tokens.css", "app.css"])
def test_css_has_no_remote_urls(name):
    text = (STATIC / name).read_text(encoding="utf-8")
    remote = re.findall(r"url\(\s*['\"]?(https?://[^)\"']+)", text)
    assert remote == [], f"{name} 里出现了外部字体/图片：{remote}"


def test_templates_do_not_load_remote_assets():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in re.findall(r'(?:src|href)="(https?://[^"]+)"', text):
            # 文档链接（<a href>）允许，资源加载不允许
            assert not match.startswith(("https://cdn", "http://cdn")), f"{path.name}: {match}"


def test_fonts_are_vendored():
    fonts = sorted(p.name for p in (STATIC / "fonts").glob("*.woff2"))
    assert fonts, "字体文件没有随包发布（离线部署会退化成系统字体）"
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    for name in fonts:
        assert f"/static/fonts/{name}" in css, f"{name} 没有 @font-face 引用"


# ---------------------------------------------------------------------------
# 3. 类名覆盖
# ---------------------------------------------------------------------------


def test_every_template_class_is_defined():
    """模板用到的类必须在 CSS 里存在。

    动态拼接的类（`status-bar-{{ name }}`）会留下 `status-` 这种前缀，跳过它们。
    """
    defined = set(re.findall(r"\.([A-Za-z][\w-]*)", _css()))
    used: set[str] = set()
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for attr in re.findall(r'class="([^"]*)"', text):
            attr = re.sub(r"\{%.*?%\}", " ", attr)
            attr = re.sub(r"\{\{.*?\}\}", " ", attr)
            for token in attr.split():
                if re.fullmatch(r"[A-Za-z][\w-]*", token):
                    used.add(token)
    missing = sorted(
        name for name in used - defined if not name.endswith("-") and name not in {"status-bar"}
    )
    assert missing == [], f"以下类在模板里用了但 CSS 里没有定义：{missing}"


# ---------------------------------------------------------------------------
# 4. 深色主题变量全覆盖
# ---------------------------------------------------------------------------


def test_dark_theme_defines_every_light_variable():
    """深色块必须覆盖浅色块里的每个变量。

    漏一个是**静态检查抓不到**的：页面照常渲染，只是切到深色后那一块还是浅色
    （或者文字与底色撞色）。所以逐名比对。
    """
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    light_block = re.search(r":root\s*\{(.*?)\}", css, re.S)
    dark_block = re.search(r'html\[data-theme="dark"\]\s*\{(.*?)\}', css, re.S)
    assert light_block and dark_block

    def names(block: str) -> set[str]:
        return set(re.findall(r"(--[\w-]+)\s*:", block))

    light, dark = names(light_block.group(1)), names(dark_block.group(1))
    # 字体栈、间距、圆角、过渡这些与主题无关，允许只在 :root 定义
    theme_irrelevant = {
        n for n in light
        if n.startswith(("--font-", "--sp-", "--r-", "--lh-", "--fs-", "--transition"))
        or n in {"--side-w", "--wrap-max", "--header-h"}
    }
    missing = sorted(light - dark - theme_irrelevant)
    assert missing == [], f"深色主题缺少这些变量：{missing}"


# ---------------------------------------------------------------------------
# 5. 组件层不写死颜色
# ---------------------------------------------------------------------------


def test_app_css_has_no_color_literals():
    """组件层只引用 token。

    唯一允许的例外是打印样式里的 `#ccc`：打印没有主题，需要一个中性的边框色。
    """
    text = (STATIC / "app.css").read_text(encoding="utf-8")
    literals = re.findall(r"#[0-9a-fA-F]{3,8}", text)
    assert literals == ["#ccc"], f"组件层出现了写死的颜色：{literals}"


def test_tokens_define_warm_palette():
    """浅色底与主文字必须走暖色调，而不是纯白/纯黑。

    这是"纸感"的基础：纯 #fff 底 + 纯 #000 字会立刻变成普通后台。
    """
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    for token in ("--bg: #fbfaf8", "--text: #2c2825"):
        assert token in css, f"色板缺少暖色基准值：{token}"