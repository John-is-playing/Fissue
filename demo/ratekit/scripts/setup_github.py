#!/usr/bin/env python3
"""一键在 GitHub 上搭好 ratekit 测试夹具。

做四件事：
    1. 创建（或复用）``<owner>/ratekit`` 仓库
    2. 推送 ``main`` 分支（含植入的 7 处缺陷）
    3. 按 ``fixtures/issue-*.md`` 创建 10 个 Issue（含重复 / 误报 / 刷量）
    4. 推送 3 个修复分支并创建 3 个 Pull Request（1 正确 / 1 半吊子 / 1 无效）

与 ``demo/scripts/setup_github.py``（textkit）是**同一套实现**，只换了
仓库目录、fixtures 目录与默认仓库名。两个夹具互相独立，可分别推送。

为什么不用 ``gh`` CLI：它不一定装了。这个脚本只依赖 ``httpx``（Fissue 已有依赖）。

用法::

    # 1) 准备一个有权建仓库的 token（勾 repo 权限）
    export GITHUB_TOKEN=ghp_xxx

    # 2) 干跑看看会发生什么（不真的写）
    python demo/ratekit/scripts/setup_github.py --owner 你的用户名 --dry-run

    # 3) 真跑
    python demo/ratekit/scripts/setup_github.py --owner 你的用户名

    # 可选：指定仓库名 / 私有 / 跳过 PR
    python demo/ratekit/scripts/setup_github.py --owner me --name ratekit-demo --private --no-prs

注意：脚本是**可重入**的——已存在的 Issue（按标题匹配）与 PR（按 head 分支匹配）
会被跳过，所以失败后可以放心重跑。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    print("需要 httpx：pip install httpx", file=sys.stderr)
    raise SystemExit(1)

API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
DEMO_ROOT = Path(__file__).resolve().parents[1]   # demo/ratekit/
REPO_DIR = DEMO_ROOT / "repo"
FIXTURES = DEMO_ROOT / "fixtures"


def _resolve_tls_verify() -> Any:
    """决定 httpx 的 ``verify`` 参数。

    必须处理 TLS 中间人场景：企业网关 / 抓包工具（Fiddler、Charles、SteamTools…）
    会用自己的根证书重签 HTTPS。该证书在**系统信任库里**（浏览器正常），
    但不在 certifi 里，于是 httpx 默认会 ``CERTIFICATE_VERIFY_FAILED``。

    三级降级（脚本可能脱离 Fissue 单独运行，所以不能只依赖 fissue.net）：
        1. 复用 ``fissue.net`` 的上下文（与主程序行为一致）
        2. 直接尝试 ``truststore`` 接入操作系统信任库
        3. 退回 httpx 默认（certifi）
    环境变量：``FISSUE_CA_BUNDLE`` 指定 CA 文件，``FISSUE_INSECURE_SKIP_VERIFY=1`` 关闭校验。
    """
    # 显式关闭校验（仅排查）
    if str(os.environ.get("FISSUE_INSECURE_SKIP_VERIFY", "")).strip().lower() in (
        "1", "true", "yes", "on", "y"
    ):
        print("⚠️  已通过 FISSUE_INSECURE_SKIP_VERIFY 关闭 TLS 校验（仅限本地排查）", file=sys.stderr)
        return False

    # 显式指定 CA
    bundle = os.environ.get("FISSUE_CA_BUNDLE", "").strip()
    if bundle and Path(bundle).exists():
        import ssl

        return ssl.create_default_context(cafile=bundle)

    # 1) 复用主程序的实现
    try:
        sys.path.insert(0, str(DEMO_ROOT.parents[1] / "src"))
        from fissue.net import httpx_verify  # type: ignore

        return httpx_verify()
    except Exception:
        pass

    # 2) 直接上 truststore
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass

    # 3) 退回 certifi
    return True


# 脚本可能脱离 Fissue 运行，这里静默决定一次
TLS_VERIFY = _resolve_tls_verify()

# 三个 PR 分支 → 关联的 Issue（按标题子串匹配，运行时解析出真实编号）
PR_BRANCHES = [
    {
        "head": "fix/remove-tax-inverse",
        "title": "fix: remove_tax 改为 gross/(1+rate)，与 apply_tax 互逆",
        "closes_issue_title": "remove_tax 公式错误",
        "body": (
            "## 修复内容\n\n"
            "`remove_tax(gross, rate)` 此前用 `gross * (1 - rate)`，"
            "算的是「再打一次折」而不是「除以 1+税率」：\n\n"
            "```\n"
            "remove_tax(113.0, 0.13)  # 之前 98.31，现在 100.0\n"
            "```\n\n"
            "## 改动\n\n"
            "- `ratekit/tax.py`：`gross * (1 - rate)` → `gross / (1 + rate)`\n"
            "- `tests/test_tax.py`：补充反推用例与 `apply_tax` 往返用例\n\n"
            "## 验证\n\n"
            "```\n"
            "remove_tax(113.0, 0.13)                        # 100.0\n"
            "remove_tax(100.0, 0.0)                         # 100.0\n"
            "apply_tax(remove_tax(113.0, 0.13), 0.13)       # 113.0（互逆）\n"
            "```\n\n"
            "零税率路径不变，其余用例保持通过。\n"
        ),
    },
    {
        "head": "fix/weekend-and-working-days",
        "title": "fix: is_weekend 补上星期六判定",
        "closes_issue_title": "is_weekend 漏判星期六",
        "body": (
            "## 修复内容\n\n"
            "`is_weekend` 之前只判断星期日（`weekday() == 6`），"
            "现在改为 `in (5, 6)`，周六与周日都算周末。\n\n"
            "## 改动\n\n"
            "- `ratekit/duration.py`：`is_weekend` 覆盖周六；顺带整理了 `add_working_days` 的循环\n\n"
            "## 验证\n\n"
            "```\n"
            "is_weekend(date(2024, 1, 6))   # True（星期六）\n"
            "is_weekend(date(2024, 1, 7))   # True（星期日）\n"
            "```\n"
        ),
    },
    {
        "head": "fix/percent-negative-sign",
        "title": "fix: percent_of 负数百分比不再取绝对值",
        "closes_issue_title": "percent_of 对负百分比取绝对值",
        "body": (
            "## 修复内容\n\n"
            "把 `percent_of` 负数分支里的 `abs(percent)` 改掉，"
            "避免对百分比取绝对值。\n\n"
            "## 改动\n\n"
            "- `ratekit/pct.py`：`value * abs(percent) / 100` → `value * (percent * -1) / 100`\n"
            "- `tests/test_pct.py`：补充负数百分比用例\n\n"
            "## 验证\n\n"
            "```\n"
            "percent_of(200, -5)   # 10.0\n"
            "```\n"
        ),
    },
]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def run_git(*args: str, cwd: Path = REPO_DIR) -> subprocess.CompletedProcess:
    """执行 git 命令（不打印敏感信息）。"""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    # 显式 utf-8：Windows 默认按 GBK 解码，git 的中文输出会变成乱码
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )


def parse_fixture(path: Path) -> dict[str, Any]:
    """解析 fixture 文件头部注释里的元数据。

    格式::

        <!--
        title: xxx
        labels: bug, enhancement
        target: tier1
        -->
        正文...
    """
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {"title": path.stem, "labels": [], "target": ""}
    body = text

    m = re.match(r"^\s*<!--\s*(.*?)\s*-->\s*", text, re.DOTALL)
    if m:
        header = m.group(1)
        body = text[m.end():]
        for line in header.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "title":
                meta["title"] = value
            elif key == "labels":
                meta["labels"] = [x.strip() for x in value.split(",") if x.strip()]
            elif key == "target":
                meta["target"] = value

    meta["body"] = body.strip()
    return meta


# ---------------------------------------------------------------------------
# GitHub API 封装
# ---------------------------------------------------------------------------


@dataclass
class GitHub:
    """极简 REST 客户端。"""

    token: str
    dry_run: bool = False
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "fissue-demo-setup",
            },
            # 必须显式传入：httpx 默认只信 certifi，
            # 在 HTTPS 中间人环境（企业代理 / 抓包工具）下会证书校验失败。
            verify=TLS_VERIFY,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHub":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def request(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        if self.dry_run and method != "GET":
            print(f"        [dry-run] {method} {url}")
            return 200, {}
        resp = self._client.request(method, url, **kwargs)
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = resp.text
        return resp.status_code, data

    # -- 高层操作 ---------------------------------------------------------

    def whoami(self) -> str:
        code, data = self.request("GET", f"{API}/user")
        if code != 200:
            raise RuntimeError(f"token 无效（HTTP {code}）：{str(data)[:200]}")
        return data["login"]

    def repo_exists(self, owner: str, name: str) -> bool:
        code, _ = self.request("GET", f"{API}/repos/{owner}/{name}")
        return code == 200

    def create_repo(self, name: str, *, private: bool, description: str) -> str:
        code, data = self.request(
            "POST",
            f"{API}/user/repos",
            json={
                "name": name,
                "description": description,
                "private": private,
                "auto_init": False,
                "has_issues": True,
                "has_wiki": False,
            },
        )
        if code not in (200, 201):
            raise RuntimeError(f"创建仓库失败（HTTP {code}）：{str(data)[:300]}")
        return data.get("full_name", f"?/{name}")

    def list_issues(self, owner: str, name: str) -> list[dict[str, Any]]:
        code, data = self.request(
            "GET", f"{API}/repos/{owner}/{name}/issues", params={"state": "all", "per_page": 100}
        )
        return data if code == 200 and isinstance(data, list) else []

    def create_issue(self, owner: str, name: str, *, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        code, data = self.request(
            "POST",
            f"{API}/repos/{owner}/{name}/issues",
            json={"title": title, "body": body, "labels": labels},
        )
        if code not in (200, 201):
            raise RuntimeError(f"创建 Issue 失败（HTTP {code}）：{str(data)[:300]}")
        return data

    def list_pulls(self, owner: str, name: str) -> list[dict[str, Any]]:
        code, data = self.request(
            "GET", f"{API}/repos/{owner}/{name}/pulls", params={"state": "all", "per_page": 100}
        )
        return data if code == 200 and isinstance(data, list) else []

    def create_pull(
        self, owner: str, name: str, *, title: str, head: str, base: str, body: str
    ) -> dict[str, Any]:
        code, data = self.request(
            "POST",
            f"{API}/repos/{owner}/{name}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        if code not in (200, 201):
            raise RuntimeError(f"创建 PR 失败（HTTP {code}）：{str(data)[:300]}")
        return data

    def ensure_labels(self, owner: str, name: str, labels: list[str]) -> None:
        """创建缺失的标签（已存在会返回 422，忽略即可）。"""
        colors = {
            "bug": "d73a4a",
            "enhancement": "a2eeef",
            "good first issue": "7057ff",
            "needs-discussion": "fbca04",
            "urgent": "b60205",
        }
        for label in labels:
            self.request(
                "POST",
                f"{API}/repos/{owner}/{name}/labels",
                json={
                    "name": label,
                    "color": colors.get(label.lower(), "ededed"),
                    "description": "",
                },
            )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def push_branch(remote_url: str, branch: str) -> None:
    """推送单个分支到远程（URL 内嵌 token，不写进 git 配置）。"""
    result = run_git("push", "--quiet", remote_url, f"refs/heads/{branch}:refs/heads/{branch}")
    if result.returncode != 0:
        err = result.stderr.replace(remote_url, "<remote>")[:400]
        raise RuntimeError(f"推送分支 {branch} 失败：{err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="在 GitHub 上搭好 ratekit 测试夹具")
    parser.add_argument("--owner", help="GitHub 用户名（默认取 token 对应的账号）")
    parser.add_argument("--name", default="ratekit", help="仓库名（默认 ratekit）")
    parser.add_argument("--private", action="store_true", help="建为私有仓库")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际写入")
    parser.add_argument("--no-prs", action="store_true", help="只建 Issue，不建 PR")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="GitHub token")
    parser.add_argument(
        "--api",
        default=None,
        help="API 基地址（默认 https://api.github.com；测试时可指向本地 mock）",
    )
    parser.add_argument(
        "--remote-base",
        default=None,
        help="推送用的远端基地址（默认 https://github.com；测试时可指向本地裸仓库）",
    )
    args = parser.parse_args()

    # 允许通过命令行覆盖 API 基地址（模块级 API 常量已被各方法引用，这里同步更新）
    if args.api:
        global API  # noqa: PLW0603
        API = args.api.rstrip("/")
    remote_base = (args.remote_base or "https://github.com").rstrip("/")

    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    if not token and not args.dry_run:
        print("❌ 需要 GitHub token：export GITHUB_TOKEN=ghp_xxx", file=sys.stderr)
        return 1

    if not REPO_DIR.exists():
        print(f"❌ 找不到 demo 仓库目录：{REPO_DIR}", file=sys.stderr)
        return 1

    # 检查 git 状态干净
    status = run_git("status", "--porcelain")
    if status.stdout.strip():
        print(f"⚠️  ratekit 工作区不干净，建议先提交：\n{status.stdout}", file=sys.stderr)
        return 1

    with GitHub(token or "dry-run-token", dry_run=args.dry_run) as gh:
        owner = args.owner
        if not owner:
            if args.dry_run:
                owner = "<你的用户名>"
            else:
                owner = gh.whoami()
                print(f"✓ token 有效，账号：{owner}")

        repo = f"{owner}/{args.name}"
        print(f"\n目标仓库：{repo}{'（私有）' if args.private else ''}")
        print("=" * 68)

        # ---- 1) 建仓库 --------------------------------------------------
        print("\n[1/4] 创建仓库")
        if not args.dry_run and gh.repo_exists(owner, args.name):
            print(f"      已存在，跳过创建：{repo}")
        else:
            print(f"      创建 {repo} …")
            full = gh.create_repo(
                args.name,
                private=args.private,
                description="用于演示/测试 Fissue 的费率与金额计算库",
            )
            print(f"      ✓ {full}")

        # ---- 2) 推送 main ----------------------------------------------
        print("\n[2/4] 推送 main 分支")
        plain_url = f"{remote_base}/{repo}.git"
        # token 只内嵌在本次推送用的 URL 里，不写进 git 配置
        if token and "github.com" in remote_base:
            remote_url = f"https://{token}@github.com/{repo}.git"
        else:
            remote_url = plain_url
        run_git("remote", "remove", "origin")
        run_git("remote", "add", "origin", plain_url)
        if args.dry_run:
            print(f"      [dry-run] git push origin main -> {plain_url}")
        else:
            push_branch(remote_url, "main")
            print("      ✓ main 已推送")

        # ---- 3) 建 Issue -----------------------------------------------
        print("\n[3/4] 创建 Issue")
        fixtures = sorted(FIXTURES.glob("issue-*.md"))
        if not fixtures:
            print("      ⚠️  没有找到 fixtures/issue-*.md")
        existing = gh.list_issues(owner, args.name) if not args.dry_run else []
        existing_titles = {i.get("title") for i in existing}
        issue_numbers: dict[str, int] = {}

        for path in fixtures:
            meta = parse_fixture(path)
            title = meta["title"]
            labels = meta["labels"]
            if title in existing_titles:
                num = next((i.get("number", 0) for i in existing if i.get("title") == title), 0)
                issue_numbers[title] = num
                print(f"      已存在 #{num}：{title[:46]}")
                continue

            gh.ensure_labels(owner, args.name, labels)
            issue = gh.create_issue(owner, args.name, title=title, body=meta["body"], labels=labels)
            num = issue.get("number", 0)
            issue_numbers[title] = num
            print(f"      ✓ #{num} [{meta['target'][:20]}] {title[:40]}")

        # ---- 4) 建 PR --------------------------------------------------
        if args.no_prs:
            print("\n[4/4] 跳过 PR（--no-prs）")
        else:
            print("\n[4/4] 推送修复分支并创建 Pull Request")
            existing_pulls = {
                p.get("head", {}).get("ref") for p in gh.list_pulls(owner, args.name)
            } if not args.dry_run else set()

            for spec in PR_BRANCHES:
                head = spec["head"]
                if head in existing_pulls:
                    print(f"      已存在 PR（head={head}），跳过")
                    continue

                if not args.dry_run:
                    push_branch(remote_url, head)
                else:
                    print(f"      [dry-run] git push origin {head}")

                # 把 Closes #N 写进正文
                closes = next(
                    (n for t, n in issue_numbers.items() if spec["closes_issue_title"] in t), 0
                )
                body = spec["body"] + (f"\nCloses #{closes}\n" if closes else "")
                pr = gh.create_pull(
                    owner, args.name, title=spec["title"], head=head, base="main", body=body
                )
                print(f"      ✓ PR #{pr.get('number', '?')} {spec['title'][:40]}（Closes #{closes}）")

        # ---- 汇总 ------------------------------------------------------
        print("\n" + "=" * 68)
        if args.dry_run:
            print("干跑完成——以上是计划，未做任何写入。去掉 --dry-run 即可真正执行。")
        else:
            print(f"✅ 完成！仓库地址：{remote_base}/{repo}")
            print(f"   Issues：{remote_base}/{repo}/issues")
            print(f"   Pulls ：{remote_base}/{repo}/pulls")
            print("\n接下来让 Fissue 去测它：")
            print("   # 在 Fissue 的 config.yaml 里登记这个仓库，然后：")
            print(f"   fissue fetch --repo {repo} --limit 50")
            print("   fissue eval --all")
            print(f"   fissue verify --repo {repo}")
            print("   fissue flush")
            print(f"   fissue fix --repo {repo} --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
