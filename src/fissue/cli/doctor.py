"""平台能力体检：这套令牌 + 这个仓库，到底能干什么。

为什么需要它
------------
v1.3.0-b2 修掉的那个 Gitee 打标签缺陷，根因是我们把请求体发成了 dict，而平台报的是
``400 Problems parsing JSON``——「JSON 解析失败」听起来像平台不认这个接口，于是
「Gitee 不支持条目级写」这个结论在 `BENCHMARK.md` 里活了半个月。

这类问题本不该靠读文档 + 猜来回答。**判据应该是一条命令能跑出来的矩阵**：
拿真实令牌、对真实仓库，把「只读 / 打标签 / 建评论 / 建 Issue」逐项打一遍，
每项给状态码与结论。这就是 ``fissue platforms doctor``。

两条硬规矩
----------
* **默认只读**：不加 ``--write`` 绝不做任何写入。
* **写探测必须自清**：建出来的标签与评论要删掉，且删不掉时要**说出来**
  （「留下痕迹但没敢删」比「假装干净」诚实得多）。
"""

from __future__ import annotations

import contextlib
from typing import Any

import typer

from ..config import Settings
from ..errors import ConfigError, FissueError
from ..models import Platform, RepoRef
from .main import (
    CONFIG_OPT,
    ENV_OPT,
    JSON_OPT,
    REPO_OPT,
    VERBOSE_OPT,
    emit,
    emit_json,
    fail,
    load_cfg,
    platforms_app,
    print_table,
    run_async,
)

# ``platforms_app`` 定义在 ``main`` 里（与 db/sandbox/bench/auth 一致），本模块只往它挂命令。
# 反过来让 main 导入 doctor 会成环：doctor 需要 main 的 OPT 常量与输出辅助函数。

#: 写探测用的标签名。刻意带 ``doctor`` 字样，好让误留的痕迹一眼能认出来。
PROBE_LABEL = "fissue-doctor-probe"
#: 推分支探测用的分支名。带 ``doctor`` 字样，误留时一眼能认出来。
PROBE_BRANCH = "fissue-doctor-probe"


def _cleanup_workspace(ws: Any) -> None:
    """删掉探测用的工作副本（含 mkdtemp 出来的外层临时目录）。

    ``ws.cleanup()`` 只删 ``<mkdtemp>/repo``，外层目录会留下——一次探测漏一个临时目录，
    在常跑自检的机器上就会堆起来。
    """
    import shutil

    root = getattr(ws, "root", None)
    try:
        ws.cleanup()
    finally:
        parent = getattr(root, "parent", None)
        if parent is not None and getattr(parent, "name", "").startswith("fissue-repo-"):
            shutil.rmtree(parent, ignore_errors=True)


async def _probe_clone(adapter: Any, ref: RepoRef, token: str) -> dict[str, Any]:
    """克隆探测：**只读**，但它是 ``verify`` / ``fix`` 的第一道门。

    这一项最有前车之鉴：私有仓库克隆曾经因为「克隆从不递令牌 + ``GIT_ASKPASS=echo``」
    必然失败，而公开夹具一直匿名可克隆，于是这个缺陷在四套夹具上从没露过头
    （见 BENCHMARK §4.15）。克隆是唯一能无副作用验出这类问题的探测。
    """
    import asyncio

    from ..workspace import RepoWorkspace

    slug = f"{ref.owner}/{ref.name}"
    url = adapter.clone_url(ref, use_token=False)

    def work(use_token: str) -> str:
        ws = RepoWorkspace.clone(url, slug=slug, token=use_token, depth=1)
        try:
            return f"克隆成功，分支 {ws.current_branch()}"
        finally:
            _cleanup_workspace(ws)

    try:
        detail = await asyncio.to_thread(work, token)
        return {"repo": slug, "probe": "只读/克隆", "result": "✅", "detail": detail}
    except Exception as exc:
        err = _brief(exc)
        # 失败之后**必须再问一句「换成匿名呢」**：这决定了锅在谁那边。
        #   * 匿名也失败 → 私有仓库，而令牌没有项目权限（平台侧配置问题）
        #   * 匿名反而成功 → 仓库是公开的，那问题就在**我们传凭据这一步**
        # 这两种原因的处理方式完全相反，混成一句「克隆失败」等于把人引向错误方向——
        # 而「只报现象不分辨原因」正是这个命令存在的理由之外最该避免的事。
        hint = "；匿名克隆同样失败 → 私有仓库需要令牌带项目（projects）权限"
        if token:
            try:
                await asyncio.to_thread(work, "")
                hint = "；但**匿名克隆反而成功** → 问题在凭据传递（带令牌反而被拒），不是权限"
            except Exception:
                pass
        return {
            "repo": slug,
            "probe": "只读/克隆",
            "result": "❌",
            "detail": err + hint,
        }


async def _probe_push(adapter: Any, ref: RepoRef, token: str) -> dict[str, Any]:
    """推分支探测：``fix`` 的产出就靠这一步。

    只推一个空提交的探测分支，推成功立刻删远端分支。推送凭据的递法（不带令牌的 URL +
    子进程环境变量 + 凭据助手）与生产路径完全一致——所以这里通了，``fix`` 的推送基本就通。
    """
    import asyncio

    from ..workspace import RepoWorkspace

    slug = f"{ref.owner}/{ref.name}"
    url = adapter.clone_url(ref, use_token=False)

    def work() -> str:
        ws = RepoWorkspace.clone(url, slug=slug, token=token, depth=1)
        pushed = False
        try:
            if not ws.checkout(PROBE_BRANCH, create=True):
                raise RuntimeError("建探测分支失败")
            if not ws.write(".fissue-doctor-probe", "probe\n"):
                raise RuntimeError("写探测文件失败")
            ws.stage_all()
            commit = ws.commit("chore: fissue platforms doctor 探测提交（可删）")
            if not commit.ok:
                raise RuntimeError(f"提交失败：{ws.sanitize(commit.stderr)[:160]}")
            result = ws.push(url, PROBE_BRANCH, token=token)
            if not result.ok:
                raise RuntimeError(f"推送失败：{ws.sanitize(result.stderr)[:200]}")
            pushed = True
            return "推 + 删远端分支均通过"
        finally:
            # 无论成败都尝试删远端分支：半路失败时它可能已经被推上去了
            deleted = ws.delete_remote_branch(url, PROBE_BRANCH, token=token)
            _cleanup_workspace(ws)
            if pushed and not deleted.ok:
                raise RuntimeError(
                    f"探测分支已推上但删不掉，请手动删除 {PROBE_BRANCH}："
                    f"{ws.sanitize(deleted.stderr)[:160]}"
                )

    try:
        detail = await asyncio.to_thread(work)
        return {"repo": slug, "probe": "写/推分支", "result": "✅", "detail": detail}
    except Exception as exc:
        return {"repo": slug, "probe": "写/推分支", "result": "❌", "detail": _brief(exc)}



def _display(ok: bool | None) -> str:
    """把三态打成一个字符：``None`` 表示「没探测」（而不是「通过」）。

    这三态必须分开：把「跳过」显示成 ✅，等于把没验过的东西说成验过了。
    """
    if ok is None:
        return "－"
    return "✅" if ok else "❌"


def _brief(exc: BaseException, limit: int = 120) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _resolve_targets(settings: Settings, specs: list[str]) -> list[RepoRef]:
    """把 CLI 传的仓库标识解析成 RepoRef 列表。

    没写平台前缀时先去配置里找同名仓库——``doctor`` 的常见用法是「体检我现在配的那几个」，
    不该逼人把 ``platform:owner/name`` 再抄一遍。
    """
    if not specs:
        return [
            RepoRef(platform=r.platform, owner=r.owner, name=r.name)
            for r in settings.enabled_repos()
        ]

    from ..platforms.registry import parse_repo_ref

    out: list[RepoRef] = []
    for spec in specs:
        try:
            out.append(parse_repo_ref(spec, None))
            continue
        except ConfigError:
            pass
        matches = [
            r for r in settings.enabled_repos() if f"{r.owner}/{r.name}" == spec.strip()
        ]
        if len(matches) != 1:
            raise ConfigError(
                f"仓库 '{spec}' 不在配置里，请写成 platform:owner/name"
                f"（如 github:{spec}）"
            ) from None
        ref = matches[0]
        out.append(RepoRef(platform=ref.platform, owner=ref.owner, name=ref.name))
    return out


async def _probe_read(adapter: Any, ref: RepoRef) -> list[dict[str, Any]]:
    """只读探测：这些**做不通就说明令牌或可见性有问题**，写探测也不必再试。"""
    rows: list[dict[str, Any]] = []
    slug = f"{ref.platform.value}:{ref.owner}/{ref.name}"

    def add(name: str, ok: bool | None, detail: str) -> None:
        rows.append({"repo": slug, "probe": name, "result": _display(ok), "detail": detail})

    try:
        who = await adapter.whoami()
        add("只读/whoami", True, f"账号 {who}")
    except Exception as exc:
        add("只读/whoami", False, _brief(exc))

    try:
        branch = await adapter.get_default_branch(ref)
        add("只读/默认分支", True, branch)
    except Exception as exc:
        add("只读/默认分支", False, _brief(exc))

    try:
        # fetch_items 是**异步迭代器**（分批拉取），不能直接 await——写成 await 会得到
        # async_generator，然后在这里报一句看不懂的类型错误。
        count = 0
        async for _item in adapter.fetch_items(ref, limit=1):
            count += 1
        add("只读/条目", True, f"能读到 {count} 条")
    except Exception as exc:
        add("只读/条目", False, _brief(exc))

    try:
        labels = await adapter.fetch_repo_labels(ref)
        add("只读/仓库标签", True, f"{len(labels)} 个")
    except Exception as exc:
        add("只读/仓库标签", False, _brief(exc))

    return rows


async def _probe_write(
    adapter: Any, ref: RepoRef, number: str | None
) -> list[dict[str, Any]]:
    """写探测：**每一项都在 finally 里自清**，删不掉就把这句话写进结果。

    只探测「可逆」的三种写。建 Issue 不做——它会在仓库里留一条要人情的记录，
    而它恰恰是已知会 404 的那一项（见 BENCHMARK §4.16），探测价值低于代价。

    顺序有讲究：**仓库标签放到最后删**。Gitee 在「给条目打标签」时会**自动创建**
    同名仓库标签，先删后建就等于白删——第一次实测就是这样留下了一个
    ``fissue-doctor-probe``（13 个标签 vs 原来 12 个），而命令当时还报了「建 + 删均通过」。
    """
    rows: list[dict[str, Any]] = []
    slug = f"{ref.platform.value}:{ref.owner}/{ref.name}"

    def add(name: str, ok: bool | None, detail: str) -> None:
        rows.append({"repo": slug, "probe": name, "result": _display(ok), "detail": detail})

    # ① 仓库级标签：先建（这一步同时验证「仓库级写」到底通不通）
    repo_label_ok = True
    repo_label_err = ""
    try:
        await adapter.ensure_label(ref, PROBE_LABEL, description="Fissue 体检用，可删")
    except Exception as exc:
        repo_label_ok = False
        repo_label_err = _brief(exc)

    # ② 条目打标签：加 → 移除
    if number is None:
        add("写/条目打标签", None, "跳过：需要 --number（Gitee 编号是字符串，如 IKM265）")
        add("写/条目评论", None, "跳过：需要 --number")
    else:
        added = False
        try:
            await adapter.add_labels(ref, number, [PROBE_LABEL])
            added = True
        except Exception as exc:
            add("写/条目打标签", False, _brief(exc))
        if added:
            cleanup = ""
            try:
                await adapter.remove_label(ref, number, PROBE_LABEL)
            except Exception as exc:
                cleanup = f"；⚠️ 清理失败，请手动移除 {PROBE_LABEL}：{_brief(exc)}"
            add("写/条目打标签", True, "加 + 移除均通过" + cleanup)

        # ③ 条目评论：建 → 删（按正文逐字定位，对不上就什么都不删）
        body = "fissue platforms doctor 探测评论（本命令会自动删除）"
        posted = False
        try:
            await adapter.comment(ref, number, body)
            posted = True
        except Exception as exc:
            add("写/条目评论", False, _brief(exc))
        if posted:
            cleanup = ""
            try:
                removed = await adapter.delete_comment_by_body(ref, number, body)
                if not removed:
                    cleanup = "；⚠️ 没找到刚发的评论，可能残留在仓库里，请手动删除"
            except Exception as exc:
                cleanup = f"；⚠️ 清理失败，请手动删除那条探测评论：{_brief(exc)}"
            add("写/条目评论", True, "建 + 删除均通过" + cleanup)

    # ④ 最后清场：**以「实际还在不在」为准**，不看删除调用报没报错
    if repo_label_ok:
        detail = "建 + 删均通过"
        try:
            await adapter.delete_repo_label(ref, PROBE_LABEL)
        except Exception as exc:
            repo_label_err = _brief(exc)
        try:
            still_there = PROBE_LABEL in set(await adapter.fetch_repo_labels(ref))
        except Exception:
            # 查不清就按「可能残留」报——谎报干净比报残留更糟
            still_there = True
        if still_there:
            detail = f"建成功，但⚠️ 清不掉，请手动删除标签 {PROBE_LABEL}：{repo_label_err}"
        add("写/仓库标签", True, detail)
    else:
        add("写/仓库标签", False, repo_label_err)

    return rows


def _summary(rows: list[dict[str, Any]]) -> tuple[int, int]:
    failed = [r for r in rows if r["result"] == "❌"]
    skipped = [r for r in rows if r["result"] == "－"]
    return len(failed), len(skipped)


async def _run(
    settings: Settings,
    targets: list[RepoRef],
    *,
    write: bool,
    number: str | None,
    probe_push: bool,
) -> list[dict[str, Any]]:
    from ..platforms.registry import build_adapter

    rows: list[dict[str, Any]] = []
    adapters: dict[Platform, Any] = {}
    try:
        for ref in targets:
            token = settings.tokens.get(ref.platform)
            if not token:
                rows.append(
                    {
                        "repo": f"{ref.platform.value}:{ref.owner}/{ref.name}",
                        "probe": "令牌",
                        "result": "❌",
                        "detail": f"没有 {ref.platform.display} 令牌"
                        f"（FISSUE_{ref.platform.value.upper()}_TOKEN），其余项无法探测",
                    }
                )
                continue

            adapter = adapters.get(ref.platform)
            if adapter is None:
                adapter = build_adapter(settings, platform=ref.platform)
                adapters[ref.platform] = adapter

            read_rows = await _probe_read(adapter, ref)
            rows.extend(read_rows)
            read_ok = all(r["result"] != "❌" for r in read_rows)

            # 克隆：只读，所以不需要 --write。它验的是 `verify` / `fix` 的第一道门。
            if read_ok:
                rows.append(await _probe_clone(adapter, ref, token))

            if write and read_ok:
                rows.extend(await _probe_write(adapter, ref, number))
            elif write:
                rows.append(
                    {
                        "repo": f"{ref.platform.value}:{ref.owner}/{ref.name}",
                        "probe": "写/全部",
                        "result": "－",
                        "detail": "跳过：只读探测就没通，写探测没有意义",
                    }
                )

            # 推分支：默认不探，因为它会在远端短暂留下一个分支。
            if probe_push and read_ok:
                rows.append(await _probe_push(adapter, ref, token))
            elif probe_push:
                rows.append(
                    {
                        "repo": f"{ref.platform.value}:{ref.owner}/{ref.name}",
                        "probe": "写/推分支",
                        "result": "－",
                        "detail": "跳过：只读探测就没通",
                    }
                )
            else:
                rows.append(
                    {
                        "repo": f"{ref.platform.value}:{ref.owner}/{ref.name}",
                        "probe": "写/推分支",
                        "result": "－",
                        "detail": "未探测：加 --probe-push 才做（会在远端短暂留一个分支）",
                    }
                )

            # 建 PR：刻意不探测。它要留一条「需人处理」的 PR（关闭 + 删源分支），
            # 代价明显高于收益——推分支通过之后，剩下的失败点只有平台侧的参数形状，
            # 那个用一次真 `fissue fix --dry-run` 之外没有太好的替代品。
            rows.append(
                {
                    "repo": f"{ref.platform.value}:{ref.owner}/{ref.name}",
                    "probe": "写/建 PR",
                    "result": "－",
                    "detail": "刻意不探测：会留下需人关闭的 PR，代价高于收益（推送通过即已覆盖主要风险）",
                }
            )
    finally:
        for adapter in adapters.values():
            # 关不掉连接不该影响体检结论（进程结束前 OS 会收回），但也不该静默——
            # 用 suppress 只压掉异常本身，不吞掉上下文。
            with contextlib.suppress(Exception):
                await adapter.aclose()
    return rows


@platforms_app.command("doctor")
def doctor(
    repo: list[str] = REPO_OPT,
    config: str | None = CONFIG_OPT,
    env: str | None = ENV_OPT,
    verbose: bool = VERBOSE_OPT,
    write: bool = typer.Option(
        False, "--write", help="额外做写探测（会建标签/评论并自动删除）"
    ),
    number: str | None = typer.Option(
        None, "--number", help="写探测针对的 Issue 编号（Gitee 是字符串，如 IKM265）"
    ),
    probe_push: bool = typer.Option(
        False,
        "--probe-push",
        help="探测推送权限（克隆 → 推探测分支 → 删远端分支；会在远端短暂留一个分支）",
    ),
    json_out: bool = JSON_OPT,
) -> None:
    """体检各平台：令牌是否有效、只读链路是否通、写操作到底能做什么。

    默认**只读**（克隆也是只读，所以无需开关）。``--write`` 才做写探测，
    ``--probe-push`` 才验推送权限；每一项都在结束后自动清理痕迹。

    退出码：有探测失败为 1，否则 0（「跳过」不算失败）。
    """
    try:
        settings = load_cfg(config, env, verbose=verbose, require_llm=False)
        targets = _resolve_targets(settings, repo)
        if not targets:
            fail("没有可体检的仓库：配置里没有启用仓库，也没给 --repo")
            return
        rows = run_async(
            _run(
                settings,
                targets,
                write=write,
                number=(number or None),
                probe_push=probe_push,
            )
        )
    except FissueError as exc:
        fail(str(exc))
        return

    if json_out:
        emit_json(rows)
    else:
        print_table(rows, ["repo", "probe", "result", "detail"], title="平台能力体检")
        failed, skipped = _summary(rows)
        if failed:
            emit(f"\n❌ {failed} 项失败（见上表；跳过 {skipped} 项）")
        else:
            emit(f"\n✅ 全部通过（跳过 {skipped} 项）")
        if not write:
            emit("提示：加 --write --number <编号> 可探测打标签与建评论（会自动清理）")

    failed, _skipped = _summary(rows)
    if failed:
        raise SystemExit(1)
