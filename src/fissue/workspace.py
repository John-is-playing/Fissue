"""Git 工作区：为验证与自动修复准备仓库副本。

安全与卫生
----------
* 一律浅克隆到**本地临时目录**，不碰用户现有仓库。
* **绝不**把令牌写进 git remote 配置或日志（URL 只在单次命令中内联）。
* 所有输出经 :meth:`RepoWorkspace.sanitize` 过滤后再落日志/报告。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import FissueError
from .logging_setup import get_logger

log = get_logger(__name__)

GIT_TIMEOUT = 600


class GitError(FissueError):
    """git 操作失败。"""


@dataclass
class GitResult:
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class RepoWorkspace:
    """一个仓库的本地工作副本。"""

    root: Path
    slug: str = ""
    token: str = field(default="", repr=False)
    default_branch: str = "main"
    #: 克隆时用的**不带令牌**的上游 URL。:meth:`harden_git` 重建 `.git/config` 时用它写回
    #: `remote.origin.url`，而不是回头去读那份可能已被容器篡改的配置。
    origin_url: str = ""

    # -- 生命周期 ---------------------------------------------------------

    @classmethod
    def clone(
        cls,
        clone_url: str,
        *,
        slug: str = "",
        branch: str | None = None,
        depth: int = 1,
        token: str = "",
        dest: str | Path | None = None,
        default_branch: str = "main",
    ) -> RepoWorkspace:
        """浅克隆到临时目录。"""
        if dest is None:
            root = Path(tempfile.mkdtemp(prefix="fissue-repo-"))
            target = root / "repo"
        else:
            target = Path(dest)
            target.parent.mkdir(parents=True, exist_ok=True)

        # 私有仓库必须递凭据：`clone_url` 不带令牌、`_git` 又写死
        # `GIT_ASKPASS=echo`（禁止交互式索要），所以原先**从不**带凭据——公开仓库
        # 匿名克隆照常成功，私有仓库则必然 `HTTP Basic: Access denied`（实测：极狐的
        # Private 仓库，同一个令牌 `git clone` 直接成功，只有这里失败）。
        # 递法与 `push` 完全一致：令牌经**子进程环境变量 + 凭据助手**，绝不进命令行。
        cmd = ["git"]
        extra_env: dict[str, str] = {}
        if token:
            # 助手脚本里只出现**环境变量名**，令牌本身不在命令行上。
            # 普通字符串拼接（不是 f-string）：脚本末尾有字面量 `}`，f-string 里要写 `}}`。
            helper = (
                "!f(){ printf 'username=%s\\npassword=%s\\n' oauth2 "
                '"$' + cls.PUSH_TOKEN_ENV + '"; };f'
            )
            # `-c` 是 **git 本体**的选项，必须排在子命令**之前**——`git clone … -c x`
            # 会被当成子命令的参数而报「未知开关」。本项目在 push 上踩过一次。
            cmd += ["-c", f"credential.helper={helper}"]
            extra_env[cls.PUSH_TOKEN_ENV] = token
        cmd += ["clone", "--quiet", "--no-tags"]
        if depth > 0:
            cmd += ["--depth", str(depth)]
        if branch:
            cmd += ["--branch", branch]
        cmd += [clone_url, str(target)]

        ws = cls(
            root=target,
            slug=slug,
            token=token,
            default_branch=default_branch,
            origin_url=clone_url,
        )
        result = ws._git(cmd, cwd=None, extra_env=extra_env)
        if not result.ok:
            ws.cleanup()
            raise GitError(f"克隆失败：{ws.sanitize(result.stderr)[:500]}")

        # 克隆完立刻加固：`.git/hooks` 与 `.git/config` 待会儿就要暴露给沙盒容器。
        # 顺带解决「容器里以 uid 1000 运行 → dubious ownership」的 fileMode 问题
        # （原先单独写 `core.fileMode=false`，现在由 :meth:`harden_git` 一并写进重建的配置）。
        ws.harden_git()
        return ws

    def cleanup(self) -> None:
        """删除工作副本。"""
        try:
            if self.root.exists():
                shutil.rmtree(self.root, ignore_errors=True)
        except OSError as exc:  # pragma: no cover
            log.warning("清理工作区失败：%s", exc)

    def __enter__(self) -> RepoWorkspace:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    # -- git 基础 ---------------------------------------------------------

    def _git(self, args: Sequence[str], *, cwd: str | Path | None = None, timeout: int = GIT_TIMEOUT, extra_env: dict[str, str] | None = None) -> GitResult:
        workdir = str(cwd) if cwd is not None else (str(self.root) if self.root.exists() else None)
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",          # 禁止交互式索要凭据（会挂死）
            "GIT_ASKPASS": "echo",
            "GCM_INTERACTIVE": "never",
        }
        if extra_env:
            env.update(extra_env)
        try:
            proc = subprocess.run(
                self._harden_argv(args),
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return GitResult(False, -1, "", f"git 超时：{' '.join(args[:3])}…")
        except OSError as exc:
            return GitResult(False, -1, "", f"无法执行 git：{exc}")
        return GitResult(
            proc.returncode == 0,
            proc.returncode,
            self.sanitize(proc.stdout),
            self.sanitize(proc.stderr),
        )

    #: 宿主 git 一律把钩子目录指向一个**不是目录**的路径（`os.devnull` 是文件）。
    #: 用 `-c` 在命令行上给：它的优先级高于仓库级 `core.hooksPath`，因此 `.git/config`
    #: 里被塞进去的值盖不住它。
    NO_HOOKS_PATH = os.devnull

    @classmethod
    def _harden_argv(cls, args: Sequence[str]) -> list[str]:
        """在 `git` 与子命令之间插入全局选项：禁钩子。

        为什么必须这么做：`.git` 所在的工作区对沙盒容器是**可写**的（验证器要把生成的
        测试文件写进工作区），而容器里跑的是不可信 PR 代码与模型生成的验证器。它只要写下
        `.git/hooks/pre-push`，宿主随后的 `git push` 就会执行它——而那次 push 的子进程
        环境里**正带着平台令牌**（见 :meth:`push`）。那等于容器逃逸到宿主。
        """
        items = list(args)
        if not items or items[0] != "git":
            return items
        return ["git", "-c", f"core.hooksPath={cls.NO_HOOKS_PATH}", *items[1:]]

    @staticmethod
    def _safe_config_value(value: str, *, limit: int = 200) -> str | None:
        """把要写回 `.git/config` 的值收敛成安全的单行值；不安全就返回 `None`。

        为什么需要：这些值来自**容器写得进**的 `.git/config`，而 git 配置是**按行**解析的。
        git 允许值里带字面换行（`git config --get` 会原样输出），`.strip()` 只去首尾——
        于是 `user.name = "x\\n[core]\\n\\tfsmonitor = sh -c '…'"` 会被原样写回，在「重建后」
        的配置里凭空多出一个执行入口（`fsmonitor`、`filter.*.clean`、`credential.helper`
        都会被宿主随后的 `git add` / `git commit` / `git push` 执行）。
        """
        if not value or len(value) > limit:
            return None
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
            return None
        if value.lstrip().startswith(("[", ";", "#")):
            return None
        return value

    def harden_git(self) -> None:
        """把 `.git` 里「容器写得进、宿主会执行」的部分收回自己控制。

        容器以与宿主同一 uid 跑（见 :meth:`clone` 的注释），对挂载给它的工作区可写，于是
        `.git/hooks`、`.git/config`、`.gitattributes` 都在它的影响范围内——而这些恰好是宿主
        git 的**执行入口**：

        * `hooks/`（`post-checkout`、`pre-push`…）；
        * `core.hooksPath` / `core.fsmonitor` / `credential.helper` / `include.path`；
        * `filter.*.clean|smudge|process`——`git add` 时执行，而宿主正要 `git add`；
        * `diff.*.textconv` / `diff.external`——`git diff` 时执行，用于产出补丁。

        做法是**重建** `.git/config`：只写由我们决定内容的几项（URL 取克隆时记下的那份，
        不回头读可能已被篡改的配置），再删掉整个 `.git/hooks`。被塞进来的键一个都活不下来。

        `user.name` / `user.email` 会从原配置带过来——它们是提交元数据、不是执行入口。
        但**同样是不可信输入**（配置对容器可写）：只接受安全的单行值，含换行/控制字符就
        丢弃。丢弃不影响提交——:meth:`commit` 自己用 `-c user.name` / `-c user.email` 带身份。

        调用点两处：克隆之后一次；每次沙盒跑过、要再动宿主 git 之前一次
        （见 `fixer.autofix.PRCreator._commit_and_push`）。
        """
        gitdir = self.root / ".git"
        # 正常克隆出来的是真目录。若被换成了符号链接，就**什么都别动**：先是顺着链接去写
        # 会把宿主上别的文件覆盖掉，二是这种状态本身可疑。钩子那条还有 `-c core.hooksPath`
        # 兜底（见 :meth:`_harden_argv`），不依赖这里。
        if not gitdir.is_dir() or gitdir.is_symlink():
            return
        # 两个值都来自**容器写得进**的配置，一律当不可信输入：只接受安全的单行值。
        url = self._safe_config_value(
            self.origin_url
            or self._git(["git", "config", "--local", "--get", "remote.origin.url"]).stdout.strip()
        )
        if url is None:
            log.warning("丢弃不可信的 origin url（含换行/控制字符）：%s", self.slug or self.root)
        ident = [
            (k, v)
            for k in ("user.name", "user.email")
            if (
                v := self._safe_config_value(
                    self._git(["git", "config", "--local", "--get", k]).stdout.strip()
                )
            )
            is not None
        ]

        hooks = gitdir / "hooks"
        if hooks.is_symlink():
            hooks.unlink(missing_ok=True)
        else:
            shutil.rmtree(hooks, ignore_errors=True)

        lines = [
            "# 由 RepoWorkspace.harden_git 重写：只保留我们自己决定内容的项。",
            "[core]",
            "\trepositoryformatversion = 0",
            "\tfilemode = false",
            "\tbare = false",
            "\tlogallrefupdates = true",
        ]
        if url:
            lines += [
                '[remote "origin"]',
                f"\turl = {url}",
                "\tfetch = +refs/heads/*:refs/remotes/origin/*",
            ]
        if ident:
            lines.append("[user]")
            lines += [f"\t{k.split('.', 1)[1]} = {v}" for k, v in ident]
        config = gitdir / "config"
        # 先删再写：`write_text` 会跟随符号链接，而 `.git/config` 正是容器碰得到的路径。
        config.unlink(missing_ok=True)
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    #: 短于此长度的「令牌」不参与脱敏（理由同平台适配器：把一两个字符做全局
    #: 替换会把 git 的正常报错也打成一堆 `***`）。
    MIN_REDACT_LEN = 8

    def sanitize(self, text: str) -> str:
        if not text:
            return ""
        if self.token and len(self.token) >= self.MIN_REDACT_LEN and self.token in text:
            text = text.replace(self.token, "***")
        return text

    # -- 分支与提交 -------------------------------------------------------

    def current_branch(self) -> str:
        r = self._git(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        return r.stdout.strip() if r.ok else ""

    def checkout(self, ref: str, *, create: bool = False) -> bool:
        args = ["git", "checkout"]
        if create:
            args += ["-b", ref]
        else:
            args.append(ref)
        r = self._git(args)
        if not r.ok:
            log.warning("checkout %s 失败：%s", ref, r.stderr[:300])
        return r.ok

    def fetch(self, remote: str = "origin", refspec: str | None = None, *, depth: int = 1) -> bool:
        args = ["git", "fetch", "--quiet", remote]
        if refspec:
            args.append(refspec)
        if depth > 0:
            args += ["--depth", str(depth)]
        return self._git(args).ok

    def reset_hard(self, ref: str = "HEAD") -> bool:
        return self._git(["git", "reset", "--hard", ref]).ok

    def log_oneline(self, count: int = 5, ref: str = "HEAD") -> str:
        r = self._git(["git", "--no-pager", "log", f"-{count}", "--oneline", ref])
        return r.stdout.strip()

    # -- 补丁 -------------------------------------------------------------

    def apply_patch(self, patch: str, *, three_way: bool = True) -> tuple[bool, str]:
        """把补丁应用到工作区（``git apply``）。"""
        if not patch.strip():
            return False, "补丁为空"
        # 必须以**二进制**写入：文本模式在 Windows 上会把 \n 转成 \r\n，
        # 于是 patch 里每行都多一个 CR，`git apply` 的上下文行匹配不上，
        # 报 "patch does not apply"（平台的 diff 本身是纯 LF）。
        data = patch.encode("utf-8").replace(b"\r\n", b"\n")
        with tempfile.NamedTemporaryFile("wb", suffix=".patch", delete=False) as fh:
            fh.write(data)
            patch_file = fh.name
        try:
            args = ["git", "apply", "--whitespace=nowarn"]
            if three_way:
                args.append("--3way")
            args.append(patch_file)
            r = self._git(args)
            if not r.ok:
                # 3way 失败时退回普通 apply
                r = self._git(["git", "apply", "--whitespace=nowarn", patch_file])
            return r.ok, self.sanitize(r.stderr or r.stdout)[:2000]
        finally:
            with contextlib.suppress(OSError):
                os.unlink(patch_file)

    def diff(self, *, base: str | None = None, staged: bool = False) -> str:
        args = ["git", "--no-pager", "diff", "--no-color"]
        if staged:
            args.append("--staged")
        if base:
            args.append(base)
        r = self._git(args, timeout=120)
        return r.stdout

    def diff_stat(self) -> str:
        r = self._git(["git", "--no-pager", "diff", "--stat"], timeout=120)
        return r.stdout

    # -- 文件读写 ---------------------------------------------------------

    def list_files(self, *, limit: int = 400) -> list[str]:
        r = self._git(["git", "ls-files"], timeout=120)
        if not r.ok:
            return []
        files = [line for line in r.stdout.splitlines() if line.strip()]
        return files[:limit]

    def read(self, rel: str, *, max_bytes: int = 200_000) -> str:
        path = self._resolve(rel)
        if not path.exists() or not path.is_file():
            return ""
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")

    def write(self, rel: str, content: str) -> Path:
        """写入文件。

        ``newline=""`` 很关键：默认行为会在 Windows 上把 ``\\n`` 翻译成 ``\\r\\n``，
        导致「模型给的补丁」与「实际写入的字节」不一致，git diff 里到处是换行噪音。
        """
        path = self._resolve(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def exists(self, rel: str) -> bool:
        return self._resolve(rel).exists()

    def delete(self, rel: str) -> bool:
        path = self._resolve(rel)
        if path.is_file():
            path.unlink()
            return True
        return False

    def _resolve(self, rel: str) -> Path:
        """把仓库内相对路径解析为绝对路径，并阻止越权访问。"""
        clean = str(rel).replace("\\", "/").strip()
        if clean.startswith("/") or any(seg == ".." for seg in clean.split("/")):
            raise GitError(f"非法路径：{rel}")
        target = (self.root / clean).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise GitError(f"路径越出工作区：{rel}")
        return target

    def changed_files(self) -> list[str]:
        r = self._git(["git", "status", "--porcelain"], timeout=120)
        if not r.ok:
            return []
        out: list[str] = []
        for line in r.stdout.splitlines():
            if len(line) > 3:
                out.append(line[3:].strip().strip('"'))
        return out

    # -- 提交与推送 -------------------------------------------------------

    def stage_all(self) -> bool:
        return self._git(["git", "add", "-A"]).ok

    def stage_paths(self, paths: list[str]) -> bool:
        """只暂存**指定**路径。

        提交修复分支时不能用 ``git add -A``：验证器（含它生成的测试文件）在本地
        执行时会**写进工作区**，``-A`` 会把它们一起暂存、提交、推送进 PR——等于把
        判定用的标尺公开到上游仓库。dry-run 暴露不了这个问题：补丁走 ``git diff``，
        而 git diff 不含未跟踪文件（``git status --porcelain`` 才会列出来）。
        """
        clean = [p for p in paths if p]
        if not clean:
            return False
        return self._git(["git", "add", "--", *clean]).ok

    def reset_index(self) -> bool:
        """把索引重置回 HEAD，**不动工作区文件**。

        容器对 `.git` 可写，而 `git commit` 提交的是**索引**、不是工作区：它往索引里塞点
        东西，「只 stage Agent 改过的文件」这条护栏就被绕过了——那些内容会跟着补丁一起被
        推送出去。先 `read-tree --reset` 把索引拉回 HEAD（不带 `-u`，所以工作区原样保留），
        随后 `stage_paths` 再按白名单重新 stage。
        """
        return self._git(["git", "read-tree", "--reset", "HEAD"]).ok

    def staged_files(self) -> list[str]:
        """索引里当前有哪些文件（也就是这次 commit 会包含的文件）。取不到时返回空列表。"""
        r = self._git(["git", "--no-pager", "diff", "--cached", "--name-only"])
        if not r.ok:
            return []
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def assert_on_branch(self, branch: str) -> str | None:
        """确认当前就在 ``branch`` 上；不在就返回一句可读的报错，在就返回 ``None``。

        为什么提交前必须确认：`.git/HEAD` 也在容器可写范围内，被改成别的分支之后，
        `git commit` / `git push` 会作用到另一个分支上，而调用方以为推的是自己的修复分支。
        """
        current = self.current_branch()
        if current != branch:
            return (
                f"当前分支是 {current or '(未知)'}、不是预期分支 {branch}，拒绝提交"
                "（.git 可能被沙盒里的代码动过）"
            )
        return None

    def commit(self, message: str, *, author: str = "Fissue Bot <bot@fissue.local>") -> GitResult:
        return self._git(
            [
                "git",
                "-c", f"user.name={author.split('<')[0].strip()}",
                "-c", f"user.email={author.split('<')[-1].rstrip('>')}",
                "-c", "commit.gpgsign=false",
                "commit", "--no-verify", "-m", message,
            ]
        )

    #: 传给 git 凭据助手的环境变量名。令牌只经它传递，绝不写进命令行参数——
    #: 命令行对同机所有用户可见（`ps`、`/proc/<pid>/cmdline`），环境变量只有本用户可读。
    PUSH_TOKEN_ENV = "FISSUE_GIT_PUSH_TOKEN"

    def _git_credential_args(self, token: str) -> tuple[list[str], dict[str, str]]:
        """令牌 → git 的 ``-c credential.helper`` 与子进程环境（两者都不上命令行）。

        助手脚本里只出现**环境变量名**，令牌本身不在命令行上。
        这里用普通字符串拼接而不是 f-string：脚本末尾有字面量 `}`，在 f-string
        里得写成 `}}`，容易看错。
        """
        if not token:
            return [], {}
        helper = (
            "!f(){ printf 'username=%s\\npassword=%s\\n' oauth2 "
            '"$' + self.PUSH_TOKEN_ENV + '"; };f'
        )
        return ["-c", f"credential.helper={helper}"], {self.PUSH_TOKEN_ENV: token}

    def _probe_remote_branch(self, url: str, branch: str, token: str = "") -> GitResult:
        """`git ls-remote` 探一次远端分支（只读）。"""
        cred, extra_env = self._git_credential_args(token)
        return self._git(
            ["git", *cred, "ls-remote", "--heads", url, f"refs/heads/{branch}"],
            timeout=120,
            extra_env=extra_env,
        )

    def remote_branch_sha(self, url: str, branch: str, token: str = "") -> str | None:
        """问远端该分支当前的 sha。

        **三种结果要分开**（真跑踩过这个坑）：

        * ``"<sha>"``——远端当前的 sha；
        * ``""``——确认远端**没有**这个分支（可以当新分支推）；
        * ``None``——**问不到**（网络/认证/超时）。这与「不存在」含义完全不同：把它当成
          「不存在」去普通推送，会得到一个难懂的 ``fetch first``（我在真跑里就是这么
          被绕了一轮）。
        """
        res = self._probe_remote_branch(url, branch, token)
        if not res.ok:
            return None
        out = (res.stdout or "").strip()
        return out.split()[0] if out else ""

    def push(self, url: str, branch: str, *, force: bool = False, token: str = "") -> GitResult:
        """推送分支。

        ``url`` **不带令牌**；令牌由 ``token`` 参数提供，经**子进程环境变量 +
        凭据助手**交给 git。这样它不会出现在命令行参数里（那是同机任何用户都能
        看到的），也不会被写进 git 配置或 remote。

        ``force=True`` 用的是**精确租约**，不是裸 ``--force-with-lease``：后者拿本地的
        remote-tracking ref 当期望值，而这里的推送目标是 URL、修复分支又从未 fetch 过，
        本地根本没有那个 ref——只要远端**已有**同名分支，git 就判 ``stale info`` 拒绝。
        真跑实测（三个探针仓库）：那些修复分支是 10-09 留下的，于是 6 次推送全被拒、
        PR 一个也没建出来。现在改为**先问远端要当前 sha**：拿得到就用它做租约（只允许
        覆盖「我们刚看到的那个状态」），确认没有就是新分支（本来无可覆盖），普通推送即可。

        **问不到时不允许强推**，直接响亮失败：否则要么把「问不到」当成「不存在」去普通
        推送（表现为难懂的 ``fetch first``），要么真去覆盖别人的提交。让调用方重试，
        比假装成功或给出误导性错误好。
        """
        # `-c` 是 **git 本体**的选项，必须排在子命令**之前**。
        # 这里曾写成 `git push --quiet --force-with-lease -c credential.helper=…`，
        # 于是 git 直接报「未知开关 `c'」——推送全线失败、PR 一个也建不出来。
        # 真提 PR 时实测撞到：Agent 明明修好了（验证器与回归门都通过），死在推送。
        cred, extra_env = self._git_credential_args(token)
        args = ["git", *cred, "push", "--quiet", "--no-verify"]
        if force:
            probe = self._probe_remote_branch(url, branch, token)
            if not probe.ok:
                why = self.sanitize(probe.stderr)[:200] or f"rc={probe.returncode}"
                return GitResult(
                    False,
                    probe.returncode,
                    "",
                    f"无法确认远端分支当前状态（{why}），拒绝强推以免覆盖别人的提交；请重试。",
                )
            current = (probe.stdout or "").strip()
            if current:
                args.append(f"--force-with-lease=refs/heads/{branch}:{current.split()[0]}")
        args += [url, f"HEAD:refs/heads/{branch}"]
        result = self._git(args, timeout=900, extra_env=extra_env)
        # 兜底：万一 git 把凭据回显进了输出
        result.stdout = self.sanitize(result.stdout)
        result.stderr = self.sanitize(result.stderr)
        return result

    def delete_remote_branch(self, url: str, branch: str, *, token: str = "") -> GitResult:
        """删除远端分支。

        用途只有一个：``fissue platforms doctor --probe-push`` 推完探测分支后清场。
        因此它刻意**不做**「分支不存在时也当成功」的伪宽容——调用方要知道真实结果。

        令牌递法与 :meth:`push` / :meth:`clone` 完全一致：**不带令牌的 URL** + 子进程环境
        变量 + 凭据助手，令牌既不进命令行也不写进 git 配置。
        """
        cred, extra_env = self._git_credential_args(token)
        result = self._git(
            [
                "git",
                *cred,
                "push",
                "--quiet",
                "--no-verify",
                url,
                "--delete",
                f"refs/heads/{branch}",
            ],
            timeout=120,
            extra_env=extra_env,
        )
        result.stdout = self.sanitize(result.stdout)
        result.stderr = self.sanitize(result.stderr)
        return result

    def clone_url_of(self, clone_url: str, dest_name: str) -> RepoWorkspace:  # pragma: no cover
        """从当前工作区复制一份（多分支并行验证用）。"""
        target = self.root.parent / dest_name
        shutil.copytree(self.root, target, dirs_exist_ok=False)
        return RepoWorkspace(root=target, slug=self.slug, token=self.token, default_branch=self.default_branch)

    # -- 信息 -------------------------------------------------------------

    def file_tree(self, *, limit: int = 300) -> list[str]:
        """目录树（用于给 LLM 上下文），跳过 .git 与常见大目录。"""
        out: list[str] = []
        skip_dirs = {".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__", "target"}
        for path in sorted(self.root.rglob("*")):
            if len(out) >= limit:
                break
            rel = path.relative_to(self.root)
            if any(part in skip_dirs for part in rel.parts):
                continue
            if path.is_dir():
                out.append(f"{rel}/")
            else:
                out.append(str(rel))
        return out

    def detect_language(self) -> str:
        """按扩展名统计主语言（决定验证器怎么写）。"""
        counts: dict[str, int] = {}
        ext_map = {
            ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
            ".go": "go", ".java": "java", ".rb": "ruby", ".rs": "rust", ".c": "c",
            ".cpp": "cpp", ".cs": "csharp", ".php": "php", ".sh": "shell",
        }
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", "node_modules", "dist", "build", "target"} for part in path.parts):
                continue
            lang = ext_map.get(path.suffix.lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        if not counts:
            return "unknown"
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def detect_test_command(self, hint: str | None = None) -> str | None:
        """探测项目的测试命令（Q15/Q16 的确定性兜底）。"""
        if hint:
            return hint
        root = self.root
        if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists() or (root / "tests").is_dir():
            return "python -m pytest -q"
        if (root / "package.json").exists():
            try:
                import json

                pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
                scripts = pkg.get("scripts") or {}
                if "test" in scripts:
                    return "npm test --silent"
            except Exception:
                pass
            return "npx --no-install jest --ci"
        if (root / "go.mod").exists():
            return "go test ./..."
        if (root / "pom.xml").exists():
            return "mvn -q -B test"
        if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
            return "gradle test --console=plain"
        if (root / "Gemfile").exists():
            return "bundle exec rspec"
        if (root / "Cargo.toml").exists():
            return "cargo test --quiet"
        if (root / "Makefile").exists():
            return "make test"
        return None

    def readme(self, *, limit: int = 6000) -> str:
        for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
            path = self.root / name
            if path.exists() and path.is_file():
                try:
                    return path.read_text(encoding="utf-8", errors="replace")[:limit]
                except OSError:
                    continue
        return ""

    def api_signatures(self, *, max_files: int = 40, limit: int = 80) -> str:
        """提取仓库里 Python 模块的**公开 API 签名**（给 LLM 当契约用）。

        为什么需要它：只有文件树和 README 时，模型会**猜** API 的形状——
        envkit #12 实测，生成的验证器对 ``TTLCache`` 用了下标赋值
        ``cache["a"] = 1``，还在 ``stats()`` 的返回值上取 ``.hits`` 属性
        （实际返回 dict），结果处处自错、反复重生成也修不好。
        把真实签名摆出来，模型就不必猜。

        只输出公开名（不含下划线开头），最多 ``max_files`` 个文件、``limit`` 个成员，
        避免上下文无限膨胀。解析失败的文件直接跳过。
        """
        return extract_api_signatures(
            self.list_files(limit=max_files * 4), self.read,
            max_files=max_files, limit=limit,
        )

    def call_graph(self, *, max_files: int = 40, limit: int = 60) -> str:
        """仓库内的跨模块调用链摘要（给 LLM 定位根因用）。

        pipekit #3 实测：报错点在 ``stats.count_batches``，根因却在
        ``filterops.in_ranges``。只给文件树与 API 签名时，Agent 会盯着报错点
        所在文件反复改、零进展；把调用链摆出来才能顺着链路走到根因文件。
        """
        return extract_call_graph(
            self.list_files(limit=max_files * 4), self.read,
            max_files=max_files, limit=limit,
        )


def extract_api_signatures(
    files: Sequence[str],
    read: Any,
    *,
    max_files: int = 40,
    limit: int = 80,
) -> str:
    """从 Python 文件的**公开 API** 里抽出签名，渲染成给 LLM 的契约文本。

    ``read`` 是「相对路径 → 文件内容」的可调用对象（便于测试注入）。
    只取 ``.py`` 且不在 tests/ 目录下的文件；只列公开名（不以 ``_`` 开头），
    并附上顶层常量的字面值——常量往往正是契约的一部分
    （例如某个 ``_UNIT_FACTORS`` 被改成公开名时，模型需要知道它长什么样）。
    """
    import ast

    chunks: list[str] = []
    used = 0
    for rel in files:
        if not rel.endswith(".py"):
            continue
        parts = rel.replace("\\", "/").split("/")
        if "tests" in parts or "test" in parts or parts[0].startswith("."):
            continue
        if used >= max_files:
            break
        try:
            src = read(rel)
        except Exception:            # 读不到就跳过，不影响主流程
            continue
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        members: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                members.append(f"def {node.name}{_format_signature(node)}")
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                bases = [ast.unparse(b) for b in node.bases]
                head = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
                methods = [
                    f"    def {m.name}{_format_signature(m)}"
                    for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not m.name.startswith("_")
                ]
                members.append("\n".join([head, *methods]) if methods else head)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                name, value = _module_constant(node)
                if name and value is not None:
                    members.append(f"{name} = {value}")
            if len(members) >= limit:
                break

        if members:
            used += 1
            chunks.append(f"# {rel}\n" + "\n".join(members))

    return "\n\n".join(chunks)


def extract_call_graph(
    files: Sequence[str],
    read: Any,
    *,
    max_files: int = 40,
    limit: int = 60,
) -> str:
    """抽取 Python 模块间的**跨模块调用边**，渲染成给 LLM 的调用链摘要。

    为什么需要它：pipekit #3 实测——报错点在 ``stats.count_batches``，根因却在
    ``filterops.in_ranges``。Agent 只盯着报错点所在的文件，连续 10 轮重写同一个
    文件、零进展。把「谁调用谁」直接摆出来，模型才有可能**顺着链路**走到真正的
    根因文件，而不是在一个改对了却仍失败的局部最优里空转。

    只保留「被调方属于本仓库模块」的边，滤掉 ``json.dumps`` 这类标准库/三方调用。
    ``read`` 是「相对路径 → 文件内容」的可调用对象（便于测试注入）。
    """
    import ast

    # 文件路径 → 模块短名（``pipekit/filterops.py`` → ``filterops``）
    modules: dict[str, str] = {}
    for rel in files:
        if not rel.endswith(".py"):
            continue
        parts = rel.replace("\\", "/").split("/")
        if "tests" in parts or "test" in parts or parts[0].startswith("."):
            continue
        if parts[-1] == "__init__.py":
            continue
        modules[rel] = parts[-1][:-3]
    if not modules:
        return ""
    known = set(modules.values())

    lines: list[str] = []
    used = 0
    for rel, mod in modules.items():
        if used >= max_files or len(lines) >= limit:
            break
        try:
            src = read(rel)
            tree = ast.parse(src or "")
        except Exception:            # 读不到 / 语法错误 → 跳过，不影响主流程
            continue
        used += 1
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            callees: list[str] = []
            for sub in ast.walk(node):
                callee = _called_qualified_name(sub)
                if callee and callee.split(".")[0] in known and callee not in callees:
                    callees.append(callee)
            if callees:
                lines.append(f"- {mod}.{node.name} → " + "、".join(callees))

    return "\n".join(lines[:limit])


def _called_qualified_name(node: Any) -> str | None:
    """把一次调用还原成 ``模块.函数``（只认 ``mod.func(...)`` 且 ``mod`` 是名字）。"""
    import ast

    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def _format_signature(node: Any) -> str:
    """把函数节点渲染成 ``(参数) -> 返回类型``（取不到也不报错）。"""
    import ast

    try:
        args = ast.unparse(node.args)
    except Exception:                # pragma: no cover
        args = "..."
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:            # pragma: no cover
            ret = ""
    return f"({args}){ret}"


def _module_constant(node: Any) -> tuple[str, str | None]:
    """顶层常量赋值 → ``(名字, 字面值文本)``；非常量或私有名返回 ``(name, None)``。"""
    import ast

    target: ast.expr | None
    value: ast.expr | None
    if isinstance(node, ast.AnnAssign):
        target, value = node.target, node.value
    else:
        target, value = (node.targets[0] if node.targets else None), node.value
    if not isinstance(target, ast.Name) or target.id.startswith("_"):
        return "", None
    if value is None:
        return target.id, None
    try:
        text = ast.unparse(value)
    except Exception:                # pragma: no cover
        return target.id, None
    if len(text) > 200:              # 太长的不放，避免上下文膨胀
        return target.id, None
    return target.id, text


def stage_verifier_files(workspace: RepoWorkspace, files: dict[str, str]) -> list[str]:
    """把验证器文件写入工作区，返回写入的相对路径列表。"""
    written: list[str] = []
    for rel, content in files.items():
        workspace.write(rel, content)
        written.append(rel)
    return written


def make_patch_from_files(changes: dict[str, str], *, removes: Iterable[str] = ()) -> str:  # pragma: no cover
    """从「路径→内容」直接构造 unified diff（无 git 时的兜底，不常用）。"""
    import difflib

    chunks: list[str] = []
    for path, content in changes.items():
        old = ""
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        chunks.extend(diff)
    return "".join(chunks)
