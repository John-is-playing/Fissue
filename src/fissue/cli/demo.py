"""演示模式：用仓库内置夹具起一个**只读**实例，不需要任何 token 或模型。

为什么需要它
------------
新用户第一次打开 Web 看到的是空页面：没有条目、没有流水线、没有任何"这东西能干什么"
的线索。而要让它有数据，得先配平台 token、模型 key、仓库列表——三件事都不轻。

这里换成另一条路：`demo/` 目录下本来就有五套对抗性夹具（60 Issue + 20 PR，
每一套都配了真实缺陷），把它们写进一个**独立 SQLite**，直接起 Web。看一眼就懂，
且完全离线：不发一次网络请求、不调一次模型。

三条硬约束
----------
1. **只读**：演示实例把 ``web.read_only`` 打开，任何会去平台上打标签/提 PR 的按钮都失效。
   演示数据是造出来的，真去操作只会报错或改到别人仓库。
2. **独立库**：数据落在 ``data/demo/<kit>/``，绝不碰 ``.env`` 指向的生产库
   （``FISSUE_DATABASE_URL`` 的优先级高于配置文件，这个坑踩过一次）。
3. **不需要模型**：``load_settings(require_llm_key=False)``，LLM 相关页面照常渲染，
   只是按下按钮会说"没配模型"——这正是新用户该看到的状态。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select

from ..config import Settings, load_settings
from ..logging_setup import get_logger
from ..models import ItemType, Platform, RawItem, RepoRef
from ..store.db import build_database
from ..store.repository import Repository
from ..store.tables import ItemRow
from .main import CONFIG_OPT, ENV_OPT, VERBOSE_OPT, demo_app, emit, fail, load_cfg

log = get_logger(__name__)

#: 仓库根下的夹具目录。安装成 wheel 后未必存在——那种情况直接明说，不假装能跑。
DEMO_ROOT = Path(__file__).resolve().parents[3] / "demo"

#: 演示实例的状态：铺开覆盖「待处理 / 在途 / 已出结论」，让三个视图都有内容。
DEMO_STATUSES = ("needs_manual", "queued", "evaluated", "verified", "fixing", "failed")

#: fixture 文件头的元信息块：``<!-- title: … labels: … -->``。
_FRONT_MATTER = re.compile(r"<!--(.*?)-->", re.S)


def available_kits() -> list[str]:
    """仓库里可用的夹具名（有 ``fixtures/`` 的目录）。"""
    if not DEMO_ROOT.is_dir():
        return []
    return sorted(
        path.name
        for path in DEMO_ROOT.iterdir()
        if path.is_dir() and (path / "fixtures").is_dir()
    )


def parse_fixture(path: Path) -> dict[str, Any]:
    """把一份 fixture markdown 拆成标题 / 标签 / 正文。

    文件头是 HTML 注释（渲染时不可见），正文才是 issue 内容。注释里没有 title 时
    退回文件名——总比空标题好，页面上能看出是哪一条。
    """
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    match = _FRONT_MATTER.search(text)
    if match:
        body = text[match.end():].lstrip()
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip()
    return {
        "title": meta.get("title") or path.stem,
        "labels": meta.get("labels", ""),
        "expect": meta.get("target", ""),
        "body": body,
        "file": path.name,
    }


def seed_demo(settings: Settings, kit: str, *, reset: bool = False) -> dict[str, Any]:
    """把 ``demo/<kit>/fixtures`` 写进演示库，返回统计。

    幂等：``upsert_item`` 按 ``(repo, number)`` 更新，重复导入只会覆盖、不会翻倍。
    ``reset=True`` 时在写之前把旧库文件删掉（由 ``demo_up`` 负责），
    这样"改过夹具想重看"只需多一个参数。
    """
    fixtures_dir = DEMO_ROOT / kit / "fixtures"
    if not fixtures_dir.is_dir():
        raise FileNotFoundError(f"没有这套夹具：{kit}（可用：{'、'.join(available_kits())}）")

    files = sorted(fixtures_dir.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"{fixtures_dir} 里没有 fixture 文件")

    db = build_database(settings)
    db.create_all()
    repo = Repository(db)
    repo_id = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="demo", name=kit))

    written = 0
    for index, path in enumerate(files, start=1):
        data = parse_fixture(path)
        repo.upsert_item(
            repo_id,
            RawItem(
                platform=Platform.GITHUB,
                repo=f"demo/{kit}",
                number=index,
                item_type=ItemType.ISSUE,
                title=data["title"][:200],
                body=data["body"],
                # 条目链接由 repo+number 生成，RawItem 里没有 url 字段
                labels=[token.strip() for token in data["labels"].split(",") if token.strip()],
            ),
        )
        written += 1

    # 状态与优先级铺开：否则看板全是 0，演示没意义。
    with db.session() as session:
        rows = sorted(
            (row for row in session.scalars(select(ItemRow)) if row.repo_slug == f"demo/{kit}"),
            key=lambda row: int(row.number or 0),
        )
        for position, row in enumerate(rows):
            row.status = DEMO_STATUSES[position % len(DEMO_STATUSES)]
            row.priority = "tier1" if position < 2 else ("tier2" if position < 4 else "none")

    return {
        "kit": kit,
        "items": written,
        "repo": f"demo/{kit}",
        "database": str(settings.database.url),
    }


def demo_settings(*, kit: str, base: Settings | None = None) -> Settings:
    """演示专用配置：独立数据目录 + 独立库 + 只读。

    直接改对象而不是走 ``overrides``：``.env`` 里的 ``FISSUE_DATABASE_URL``
    优先级更高，任何"配置里写演示库"的做法都可能被它覆盖成生产库。
    """
    # 深拷贝：演示配置**不能**改到调用方拿着的那个 Settings（它可能是共享的
    # 服务配置，改完库里只剩演示数据、只读位也翻不掉，属于最坏的一类副作用）。
    settings = (base or load_settings(require_llm_key=False)).model_copy(deep=True)
    root = Path(settings.app.data_dir).parent / "demo" / kit
    root.mkdir(parents=True, exist_ok=True)
    settings.app.data_dir = str(root)
    settings.database.url = f"sqlite:///{(root / 'demo.db').as_posix()}"
    settings.web.read_only = True
    settings.repos = []
    return settings


@demo_app.command("up")
def demo_up(
    kit: str = typer.Option("textkit", "--kit", "-k", help="夹具名（textkit/ratekit/pipekit/envkit/lockkit）"),
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8000, "--port", "-p", help="监听端口"),
    reset: bool = typer.Option(False, "--reset", help="清掉旧的演示库重新写入"),
    seed_only: bool = typer.Option(False, "--seed-only", help="只写数据、不起服务（脚本/CI 用）"),
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """用内置夹具起一个只读演示实例：一条命令看到完整流水线。"""
    kits = available_kits()
    if not kits:
        fail(
            "找不到演示夹具（demo/ 目录）。演示数据随源码仓库提供，"
            "安装成 wheel 后没有；请改用 `fissue onboard` 配置你自己的仓库。"
        )
    if kit not in kits:
        fail(f"没有这套夹具：{kit}（可用：{'、'.join(kits)}）")

    base = load_cfg(config, env, verbose=verbose, require_llm=False)
    settings = demo_settings(kit=kit, base=base)
    if reset:
        db_file = Path(settings.database.url.replace("sqlite:///", ""))
        if db_file.exists():
            db_file.unlink()
            emit(f"已清掉旧的演示库：{db_file}")

    try:
        stats = seed_demo(settings, kit)
    except FileNotFoundError as exc:
        fail(str(exc))

    emit(f"✅ 演示数据就绪：{stats['repo']} 共 {stats['items']} 条")
    emit(f"   库：{stats['database']}")
    emit("   模式：只读（不会去平台上打标签或提 PR），未配置模型（AI 按钮会提示缺 key）")
    if seed_only:
        return

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - 缺 uvicorn 时也不该说"演示成功"
        fail("缺少 uvicorn：请先 pip install -e .")

    from ..web.app import create_app

    emit(f"🌐 打开 http://{host}:{port}/  看流水线 / 条目 / 任务")
    emit("   Ctrl+C 结束（演示库留在 data/demo/，下次 --reset 可重建）")
    uvicorn.run(create_app(settings), host=host, port=port, log_level=settings.app.log_level)


@demo_app.command("kits")
def demo_kits() -> None:
    """列出可用的演示夹具。"""
    kits = available_kits()
    if not kits:
        fail("找不到演示夹具（demo/ 目录）")
    emit("可用夹具：")
    for name in kits:
        count = len(list((DEMO_ROOT / name / "fixtures").glob("*.md")))
        emit(f"  - {name}（{count} 条）")
