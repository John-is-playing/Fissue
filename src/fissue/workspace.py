"""Git 工作区：为验证与自动修复准备仓库副本。

安全与卫生
----------
* 一律浅克隆到**本地临时目录**，不碰用户现有仓库。
* **绝不**把令牌写进 git remote 配置或日志（URL 只在单次命令中内联）。
* 所有输出经 :meth:`RepoWorkspace.sanitize` 过滤后再落日志/报告。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

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
    ) -> "RepoWorkspace":
        """浅克隆到临时目录。"""
        if dest is None:
            root = Path(tempfile.mkdtemp(prefix="fissue-repo-"))
            target = root / "repo"
        else:
            target = Path(dest)
            target.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["git", "clone", "--quiet", "--no-tags"]
        if depth > 0:
            cmd += ["--depth", str(depth)]
        if branch:
            cmd += ["--branch", branch]
        cmd += [clone_url, str(target)]

        ws = cls(root=target, slug=slug, token=token, default_branch=default_branch)
        result = ws._git(cmd, cwd=None)
        if not result.ok:
            ws.cleanup()
            raise GitError(f"克隆失败：{ws.sanitize(result.stderr)[:500]}")

        # 提高沙盒友好度：容器里以 uid 1000 运行，避免 dubious ownership 报错
        ws._git(["git", "config", "core.fileMode", "false"])
        ws._git(["git", "config", "advice.detachedHead", "false"])
        return ws

    def cleanup(self) -> None:
        """删除工作副本。"""
        try:
            if self.root.exists():
                shutil.rmtree(self.root, ignore_errors=True)
        except OSError as exc:  # pragma: no cover
            log.warning("清理工作区失败：%s", exc)

    def __enter__(self) -> "RepoWorkspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    # -- git 基础 ---------------------------------------------------------

    def _git(self, args: Sequence[str], *, cwd: str | Path | None = None, timeout: int = GIT_TIMEOUT) -> GitResult:
        workdir = str(cwd) if cwd is not None else (str(self.root) if self.root.exists() else None)
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",          # 禁止交互式索要凭据（会挂死）
            "GIT_ASKPASS": "echo",
            "GCM_INTERACTIVE": "never",
        }
        try:
            proc = subprocess.run(
                list(args),
                cwd=workdir,
                capture_output=True,
                text=True,
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

    def sanitize(self, text: str) -> str:
        if not text:
            return ""
        if self.token and self.token in text:
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
            try:
                os.unlink(patch_file)
            except OSError:
                pass

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

    def push(self, url_with_token: str, branch: str, *, force: bool = False) -> GitResult:
        """推送分支。``url_with_token`` 只在本次命令中使用，不写入配置。"""
        args = ["git", "push", "--quiet"]
        if force:
            args.append("--force-with-lease")
        args += [url_with_token, f"HEAD:refs/heads/{branch}"]
        result = self._git(args, timeout=900)
        # 结果里可能带 token，必须过滤
        result.stdout = self.sanitize(result.stdout)
        result.stderr = self.sanitize(result.stderr)
        return result

    def clone_url_of(self, clone_url: str, dest_name: str) -> "RepoWorkspace":  # pragma: no cover
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
