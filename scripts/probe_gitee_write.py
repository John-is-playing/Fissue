#!/usr/bin/env python
"""Gitee 条目级写操作探测（只读先通 → 再试一笔真写）。

背景：Gitee 的**仓库级**写（建分支、提 PR、建仓库标签）实测是通的，但**条目级**写
（建/改 Issue、给条目打标签）一直被拒，报 `404 project or enterprise`，原因未定位
（换令牌、换公开库都复现）。见 ``BENCHMARK.md`` §4.16。

这个脚本只做一件事：把「同一枚令牌、同一个库，读能通、写不通」这条判据跑成可复核的一段，
并把**状态码**与**报文**分别打出来——判据必须先看状态码，再看文案：`404` 与 `403`
的含义完全不同（前者是「找不到你要的资源」，后者是「你没有权限」），而 Gitee 有时会把
权限问题也包装成一句 404。

默认**只读**。真写必须显式给 ``--write``，且创建成功后脚本会立刻把它关掉，
免得探测在仓库里留下一串测试 Issue。

用法::

    # 1) 只读探测（安全，随时可跑）
    python scripts/probe_gitee_write.py --repo John-is-playing/textkit

    # 2) 真写探测（会在目标仓库建一条 Issue，随即关闭）
    python scripts/probe_gitee_write.py --repo John-is-playing/textkit --write

    # 3) 换个写法复核：不建 Issue，只给一条已有条目打标签
    python scripts/probe_gitee_write.py --repo <owner>/<repo> --write --label-only --number IKM265

令牌取 ``FISSUE_GITEE_TOKEN``（进程环境或 ``.env``）。不要把它写进命令行——
本脚本自己读环境，不接受 ``--token``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:  # 本机有 HTTPS 中间人时，truststore 让 httpx 走系统信任库
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - 没有它也能跑，只是本机代理环境可能 TLS 失败
    pass

import httpx  # noqa: E402  (必须在 truststore 之后导入)

# 证书口径只有一份：项目里所有 httpx 客户端都必须显式传 `verify=httpx_verify()`
# （`tests/test_net.py::test_guard_all_httpx_clients_pass_verify` 会检查这一点）。
# 随手 `httpx.Client()` 在本机的 HTTPS 中间人环境下会直接证书校验失败。
from fissue.net import httpx_verify  # noqa: E402

API_BASE = "https://gitee.com/api/v5"
PROBE_TITLE = "[探测] Fissue 条目级写操作检查（本脚本会自动关闭它）"


def load_token() -> str:
    """``FISSUE_GITEE_TOKEN``：进程环境优先，其次仓库根的 ``.env``。"""
    token = (os.environ.get("FISSUE_GITEE_TOKEN") or "").strip()
    if token:
        return token
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "FISSUE_GITEE_TOKEN":
                return value.strip().strip('"').strip("'")
    return ""


def brief(payload: Any, *, limit: int = 300) -> str:
    """把响应体压成一行可读文本：够看清 Gitee 的报错文案，又不至于刷屏。"""
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = str(payload or "")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def judge(status: int) -> str:
    """把状态码翻译成结论——**先看码，再看文案**。"""
    if 200 <= status < 300:
        return "通（写操作成功）"
    if status == 401:
        return "令牌无效或过期（换一枚再试）"
    if status == 403:
        return "无权限（令牌 scope 或仓库授权问题）"
    if status == 404:
        return (
            "平台侧拒绝（`project or enterprise`）。已知：建/改 Issue 会 404，"
            "而打标签、建评论、仓库级标签是通的（BENCHMARK §4.16）"
        )
    if status == 429:
        return "限流（隔几分钟再试，别加大频率）"
    return f"未归类（HTTP {status}）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gitee 条目级写操作探测")
    parser.add_argument("--repo", required=True, help="owner/name，如 John-is-playing/textkit")
    parser.add_argument("--write", action="store_true", help="真的执行写（默认只读）")
    # Gitee 的条目编号是**字符串**（如 `IKM265`），不是整数——按 int 收会把真实编号
    # 直接拒掉，也会让人误以为「编号映射」是另一个坑（见 BENCHMARK §4.13 / §4.16）。
    parser.add_argument("--number", help="已有 Issue/PR 编号（配合 --label-only；字符串，如 IKM265）")
    parser.add_argument(
        "--label-only",
        action="store_true",
        help="只探测「给条目打标签」这一种写（实测可用；不建 Issue，需要 --number）",
    )
    parser.add_argument("--keep", action="store_true", help="创建成功后**不**自动关闭（默认会关掉）")
    args = parser.parse_args(argv)

    if "/" not in args.repo:
        parser.error("--repo 必须是 owner/name")
    token = load_token()
    if not token:
        print("✗ 没有 FISSUE_GITEE_TOKEN（进程环境与 .env 都没有）。先配好令牌再跑。")
        return 2
    if args.label_only and not args.number:
        parser.error("--label-only 需要 --number")

    api = f"{API_BASE}/repos/{args.repo}"
    params = {"access_token": token}   # Gitee 只认查询参数的那部分接口（与适配器一致）
    failures = 0

    with httpx.Client(timeout=30.0, follow_redirects=True, verify=httpx_verify()) as client:
        print(f"仓库：{args.repo}　令牌：***{token[-4:]}")
        print("\n[1/3] 只读：取仓库信息（这一步必须先通）")
        r = client.get(api, params=params)
        print(f"      HTTP {r.status_code}　{judge(r.status_code)}")
        if r.status_code >= 300:
            print(f"      报文：{brief(r.text)}")
            print("\n✗ 只读就不通——先解决令牌/仓库可见性，写操作无从谈起。")
            return 1
        data = r.json()
        print(f"      仓库全名：{data.get('full_name')}　私有：{data.get('private')}")

        if args.label_only:
            print(f"\n[2/3] 写：给 #{args.number} 打一个探测标签（issues/{args.number}/labels）")
            url = f"{api}/issues/{args.number}/labels"
            # 裸数组：Gitee 只认这个形状。发 {"labels": [...]} 会得到
            # 400 Problems parsing JSON——那句报错说的是「JSON 解析失败」，不是「字段不认识」。
            r = client.post(url, params=params, json=["fissue-probe"])
            print(f"      HTTP {r.status_code}　{judge(r.status_code)}")
            print(f"      报文：{brief(r.text)}")
            failures += 0 if r.status_code < 300 else 1
            print(
                "\n参考：同一枚令牌下，`POST /repos/{repo}/labels`（仓库级标签）实测是通的。"
                "若这一步 404 而上一步（只读）200，就说明卡在**条目级**端点，"
                "而不是令牌权限。"
            )
        else:
            print("\n[2/3] 写：建一条 Issue（issues）")
            if not args.write:
                print("      跳过：没有 --write（默认只读）。加上它才会真的建 Issue。")
            else:
                r = client.post(
                    f"{api}/issues",
                    params=params,
                    json={
                        "title": PROBE_TITLE,
                        "body": "这条 Issue 由 `scripts/probe_gitee_write.py` 创建，用于判断 Gitee 的"
                        "条目级写是否可用（见 BENCHMARK.md §4.16）。脚本默认会在创建成功后立即关闭它。",
                    },
                )
                print(f"      HTTP {r.status_code}　{judge(r.status_code)}")
                print(f"      报文：{brief(r.text)}")
                if r.status_code >= 300:
                    failures += 1
                else:
                    issue = r.json()
                    number = issue.get("number")
                    print(f"      已创建：#{number}　{issue.get('html_url')}")
                    if not args.keep and number:
                        print("\n[3/3] 清理：把它关掉（免得探测污染仓库）")
                        rc = client.patch(
                            f"{api}/issues/{number}", params=params, json={"state": "closed"}
                        )
                        print(f"      HTTP {rc.status_code}　{judge(rc.status_code)}")
                        if rc.status_code >= 300:
                            print(f"      报文：{brief(rc.text)}")
                            print(f"      ⚠️ 自动关闭失败——请手动关闭 #{number}。")
                            failures += 1

    print("\n" + "=" * 66)
    if failures:
        print("结论：写操作**没通过**。把上面的状态码与报文记进 BENCHMARK.md §4.16（先码后文案）。")
        return 1
    print("结论：本次探测通过。请把结果（状态码 + 日期 + 令牌所属账号）补进 BENCHMARK.md §4.16。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
