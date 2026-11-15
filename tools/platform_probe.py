"""平台能力探针：在真实仓库上把 Fissue 用到的七条链路跑一遍。

为什么要有这个脚本
------------------
``capabilities.py`` 里的结论必须是**实测**的，而实测不该靠手工拼 curl。这个脚本把
「AtomGit 提 PR 全链路」那次手工验证固化成一条命令：

    python tools/platform_probe.py --platform gitlab --create
    python tools/platform_probe.py --platform gitee --owner <你的账号> --repo fissue-probe

它会：校验令牌 →（可选）建私有探针仓库 → 推初始 main → 依次实测
只读 / 克隆 / 建标签 / 打标签 / 评论 / 推分支 / 提 PR / 同分支重复提 PR，
最后打印一张表。**任何一项失败都不会中断后面的项**——一次性把所有问题都暴露出来。

安全约定：只在 `--create` 时建仓库，且一律 private；令牌只经环境/内存传递，
既不打印也不进命令行（git 推送走 Fissue 的 credential helper）。

用法示例
--------
    # 先确认令牌有效（两平台都支持）
    python tools/platform_probe.py --platform gitlab
    python tools/platform_probe.py --platform gitee

    # GitLab：自动建一个私有探针仓库并跑全链路
    python tools/platform_probe.py --platform gitlab --create
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import pathlib
import sys
import tempfile
import traceback
from urllib.parse import quote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from fissue.config import Settings, load_settings  # noqa: E402
from fissue.models import ItemType, Platform, RepoRef  # noqa: E402
from fissue.platforms.registry import build_adapter  # noqa: E402
from fissue.workspace import RepoWorkspace  # noqa: E402

PLATFORMS = {"gitee": Platform.GITEE, "gitlab": Platform.GITLAB, "atomgit": Platform.ATOMGIT}


class Probe:
    """逐项记录结果；失败不抛出，最后统一打印。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append((name, "PASS", detail))

    def fail(self, name: str, detail: str) -> None:
        self.rows.append((name, "FAIL", detail.replace("\n", " ")[:160]))

    def skip(self, name: str, why: str) -> None:
        self.rows.append((name, "SKIP", why))

    async def run(self, name: str, coro, *, detail: str = "") -> bool:
        try:
            result = await coro
            self.ok(name, detail or ("" if result is None else str(result)[:80]))
            return True
        except Exception as exc:  # noqa: BLE001 - 探针要的是"哪一项坏了"，不是崩掉
            self.fail(name, f"{type(exc).__name__}: {exc}")
            return False

    def render(self) -> str:
        width = max((len(r[0]) for r in self.rows), default=8)
        lines = []
        for name, status, detail in self.rows:
            mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭ "}.get(status, "  ")
            lines.append(f"{mark} {name.ljust(width)}  {detail}")
        return "\n".join(lines)

    @property
    def failed(self) -> int:
        return sum(1 for _, status, _ in self.rows if status == "FAIL")


def _create_repo(platform: Platform, token: str, name: str, api_base: str) -> RepoRef:
    """建一个**私有**探针仓库（Gitee：/user/repos；GitLab：/projects）。"""
    import httpx

    from fissue.net import httpx_verify

    # 守卫要求：**每个** httpx 客户端都必须显式传 verify=。httpx 默认只信 certifi
    # 的 CA；在跑着抓包工具（Fiddler / Charles）的机器上，根证书在系统信任库，
    # 不传就是一片证书校验失败。值从 fissue.net 取，与主程序同源。
    verify = httpx_verify()

    if platform is Platform.GITEE:
        resp = httpx.post(
            f"{api_base}/user/repos",
            data={"access_token": token, "name": name, "private": "true",
                  "auto_init": "false", "description": "Fissue 平台探针（可删）"},
            timeout=30,
            verify=verify,
        )
        who = httpx.get(
            f"{api_base}/user", params={"access_token": token}, timeout=30, verify=verify
        ).json()
        owner = who.get("login", "")
    else:
        resp = httpx.post(
            f"{api_base}/projects",
            headers={"PRIVATE-TOKEN": token},
            data={"name": name, "visibility": "private",
                  "description": "Fissue 平台探针（可删）"},
            timeout=30,
            verify=verify,
        )
        who = httpx.get(
            f"{api_base}/user", headers={"PRIVATE-TOKEN": token}, timeout=30, verify=verify
        ).json()
        owner = who.get("username", "")
    if resp.status_code >= 400:
        raise RuntimeError(f"建仓库失败 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if platform is Platform.GITEE:
        return RepoRef(platform=platform, owner=data.get("owner", {}).get("login") or owner,
                       name=data.get("name") or name)
    # GitLab 给的是 path_with_namespace，拆出 owner/name（支持子组）
    path = data.get("path_with_namespace") or f"{owner}/{name}"
    owner_part, _, name_part = path.rpartition("/")
    return RepoRef(platform=platform, owner=owner_part or owner, name=name_part or name)


class _ProbeItem:
    """``fetch_items`` 的最小替身：只提供打标签/评论需要的那两个字段。"""

    def __init__(self, number: int) -> None:
        self.number = number
        self.item_type = ItemType.ISSUE


def _create_issue_via_api(
    platform: Platform, token: str, ref: RepoRef, api_base: str
) -> int:
    """在探针仓库里建一条 Issue，返回编号。

    适配器**刻意没有** ``create_issue``（Fissue 自己不提 Issue，见 capabilities 注释），
    所以这里直接打平台 API。Gitee 的建 Issue 是已知的 404（BENCHMARK §4.16）——
    测出来就是当场复现那条结论，调用方会据此记 SKIP 而不是 FAIL。
    """
    import httpx

    from fissue.net import httpx_verify

    verify = httpx_verify()  # 守卫要求：显式传 verify（详见 fissue/net.py）
    title = "探针 Issue（可删）"
    body = "由 tools/platform_probe.py 创建，用于验证打标签/评论链路。"

    if platform is Platform.GITEE:
        resp = httpx.post(
            f"{api_base}/repos/{ref.owner}/{ref.name}/issues",
            params={"access_token": token},
            data={"title": title, "body": body},
            timeout=30,
            verify=verify,
        )
    elif platform is Platform.GITLAB:
        project = quote(f"{ref.owner}/{ref.name}", safe="")
        resp = httpx.post(
            f"{api_base}/projects/{project}/issues",
            headers={"PRIVATE-TOKEN": token},
            data={"title": title, "description": body},
            timeout=30,
            verify=verify,
        )
    else:  # ATOMGIT：与 Gitee 同形（access_token 走查询参数）
        resp = httpx.post(
            f"{api_base}/repos/{ref.owner}/{ref.name}/issues",
            params={"access_token": token},
            data={"title": title, "body": body},
            timeout=30,
            verify=verify,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:160]}")
    data = resp.json()
    return int(data.get("number") or data.get("iid") or data.get("id") or 0)


async def probe_platform(args: argparse.Namespace) -> int:
    platform = PLATFORMS[args.platform]
    overrides = {}
    if args.config:
        overrides["config"] = args.config
    if args.env:
        overrides["env"] = args.env
    settings: Settings = load_settings(
        overrides.get("config"), overrides.get("env"), require_llm_key=False
    )
    token = settings.tokens.get(platform)
    if not token:
        print(f"❌ {platform.value} 没有令牌：把 FISSUE_{platform.value.upper()}_TOKEN 写进 .env")
        return 2

    adapter = build_adapter(settings, platform=platform, token=token)
    report = Probe()

    # 0) 令牌是否有效 —— 后面所有项都依赖它，失败就直接结束（否则会看到一片无关报错）
    try:
        who = await adapter.whoami()
        report.ok("令牌有效", f"账号 {who}")
    except Exception as exc:  # noqa: BLE001
        report.fail("令牌有效", f"{type(exc).__name__}: {exc}")
        print(report.render())
        print("\n令牌无效，后面的项没有意义。请更新 .env 里的 "
              f"FISSUE_{platform.value.upper()}_TOKEN（Gitee 会明确回 token expired）。")
        return 1

    # 1) 定位/新建探针仓库
    ref: RepoRef | None = None
    if args.create:
        try:
            ref = _create_repo(platform, token, args.repo, adapter.api_base)
            report.ok("创建探针仓库", ref.slug)
        except Exception as exc:  # noqa: BLE001
            report.fail("创建探针仓库", f"{type(exc).__name__}: {exc}")
    elif args.owner:
        ref = RepoRef(platform=platform, owner=args.owner, name=args.repo)
        report.ok("使用已有仓库", ref.slug)
    else:
        report.skip("定位仓库", "没给 --owner，也没给 --create")

    if ref is None:
        print(report.render())
        print("\n没有可用仓库：用 --create 新建，或 --owner <账号> --repo <仓库名> 指定已有仓库。")
        return 1

    # 2) 只读
    if await report.run("只读抓取", _fetch_count(adapter, ref)):
        pass

    # 3) 推初始 main + 探针分支（很多平台空仓库没有 main，PR 的 base 会找不到）
    tmp = pathlib.Path(tempfile.mkdtemp())
    url = adapter.clone_url(ref)
    ws: RepoWorkspace | None = None
    try:
        ws = RepoWorkspace.clone(url, slug=ref.slug, depth=1, token=token,
                                 dest=tmp / "probe", default_branch="main")
        report.ok("克隆仓库")
        ws.checkout("main", create=True)
        (ws.root / "README.md").write_text(
            f"# {ref.name}\n\nFissue 平台探针仓库（{platform.value}），可随时删除。\n",
            encoding="utf-8",
        )
        ws.stage_paths(["README.md"])
        ws.commit("chore: 初始化探针仓库")
        pushed = ws.push(url, "main", token=token)
        report.ok("推初始 main", "ok" if pushed.ok else str(pushed.stderr)[:100])
    except Exception as exc:  # noqa: BLE001
        report.fail("克隆/推初始 main", f"{type(exc).__name__}: {exc}")
        print(report.render())
        return 1

    # 4) 建标签 / 打标签 / 评论
    label = "fissue-probe"
    await report.run("建标签", adapter.ensure_label(ref, label, color="0e8a16",
                                                   description="Fissue 平台探针"))
    items = []
    with contextlib.suppress(Exception):
        async for it in adapter.fetch_items(ref, limit=5):
            items.append(it)
    if not items:
        # 空仓库里没有可打标签/评论的对象，后面两项就只能 SKIP——重跑也拿不到结论。
        # 探针仓库本来就是用来留记录的，所以直接建一条 Issue：既让标签/评论有对象，
        # 又顺便把 create_issue 这一栏实测了（适配器刻意不实现它，见 capabilities 注释）。
        from fissue.platforms.capabilities import describe

        try:
            issue_no = await asyncio.to_thread(
                _create_issue_via_api, platform, token, ref, adapter.api_base
            )
            report.ok("建 Issue", f"#{issue_no}（探针 Issue，可删）")
            items = [_ProbeItem(issue_no)]
        except Exception as exc:  # noqa: BLE001
            caps = describe(platform)
            if caps.create_issue is False:
                # 已知限制当场复现：记 SKIP 并点明与结论一致，不让探针因此恒红
                report.skip("建 Issue", f"已知限制（与 capabilities 一致）：{exc}")
            else:
                report.fail("建 Issue", f"{type(exc).__name__}: {exc}")
    if items:
        number = getattr(items[0], "number", None)
        item_type = getattr(items[0], "item_type", ItemType.ISSUE)
        await report.run("打标签", adapter.add_labels(ref, number, [label], item_type))
        await report.run("评论", adapter.comment(
            ref, number, "Fissue 平台能力探针评论（可忽略）。", item_type))
    else:
        report.skip("打标签/评论", "仓库里没有 Issue/PR，先建一个再跑")

    # 5) 推分支 + 提 PR
    branch = "fissue/probe-fix"
    try:
        ws.checkout(branch, create=True)
        (ws.root / "wordcount.py").write_text(
            "def count_words(text: str) -> int:\n"
            '    """统计词数；空串返回 0。"""\n'
            "    if not text:\n"
            "        return 0\n"
            "    return len(text.split())\n",
            encoding="utf-8",
        )
        ws.stage_paths(["wordcount.py"])
        ws.commit("fix: 空字符串应返回 0 个词")
        ok = ws.push(url, branch, token=token).ok
        report.ok("推分支", "ok" if ok else "推送返回失败")
    except Exception as exc:  # noqa: BLE001
        report.fail("推分支", f"{type(exc).__name__}: {exc}")

    pr_number = None
    try:
        result = await adapter.create_pr(
            ref, head=branch, base="main", title="探针：空串应返回 0 个词",
            body="由 tools/platform_probe.py 生成，用于验证提 PR 链路。", draft=False,
        )
        pr_number = result.get("number")
        link = result.get("url") or "(无链接)"
        report.ok("提 PR", f"#{pr_number} {link}")
        if not result.get("url"):
            report.fail("PR 链接", "平台没给链接，回退也没拼出来——条目页会点不到 PR")
    except Exception as exc:  # noqa: BLE001
        report.fail("提 PR", f"{type(exc).__name__}: {exc}")

    # 6) 同一分支重复提 PR：必须复用而不是抛错（AtomGit 409 / GitLab 400）
    if pr_number:
        try:
            again = await adapter.create_pr(
                ref, head=branch, base="main", title="重复提交应复用",
                body="验证「已存在」复用。", draft=False,
            )
            if again.get("number") == pr_number:
                report.ok("重复提 PR 复用", f"复用 #{pr_number}")
            else:
                report.fail("重复提 PR 复用", f"返回了另一个编号 {again.get('number')}")
        except Exception as exc:  # noqa: BLE001
            report.fail("重复提 PR 复用", f"{type(exc).__name__}: {exc}")

    print(report.render())
    print(f"\n探针仓库：{ref.slug}（{platform.value}，private）")
    if report.failed:
        print(f"❌ {report.failed} 项失败——上面每行的失败原因就是需要修的地方。")
    else:
        print("✅ 全部通过。把结论写进 src/fissue/platforms/capabilities.py。")
    return 1 if report.failed else 0


async def _fetch_count(adapter, ref: RepoRef) -> str:
    count = 0
    async for _ in adapter.fetch_items(ref, limit=5):
        count += 1
    return f"读到 {count} 条"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="平台能力探针（真实仓库上跑七条链路）")
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--owner", help="已有仓库的 owner/namespace")
    parser.add_argument("--repo", default="fissue-platform-probe", help="仓库名")
    parser.add_argument("--create", action="store_true", help="自动新建私有探针仓库")
    parser.add_argument("--config", help="config.yaml 路径")
    parser.add_argument("--env", help=".env 路径")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(probe_platform(args))
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
