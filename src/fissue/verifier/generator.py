"""验证器生成器（Q7）。

流程
----
1. 把「问题描述 + 仓库上下文」交给 LLM，要求产出**可执行测试**（或降级为清单）。
2. 解析并**校验安全性**：文件路径必须相对、禁止越权路径、禁止可疑外联命令。
3. 若首轮验证器在 base 阶段行为不对（例如修复前就通过），调用 :meth:`refine`
   带着失败证据让模型修正（最多 ``verifier.max_rounds`` 轮）。

注意：生成器只负责「产出 + 静态校验」，真正的 F2P 判定由
:mod:`fissue.verifier.runner` 在沙盒里跑出来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from ..ai import prompts
from ..ai.client import LLMClient, Usage
from ..config import Settings, VerifierConfig
from ..logging_setup import get_logger
from ..models import ItemType, RawItem, VerifierKind, VerifierSpec

log = get_logger(__name__)

# 明显越权/危险的路径前缀
_FORBIDDEN_PREFIXES = ("/", "..", "~", "\\")
# 可疑命令片段：外联、提权、破坏宿主
_SUSPICIOUS_PATTERNS = [
    re.compile(r"\b(curl|wget)\b.*https?://", re.IGNORECASE),
    re.compile(r"\bnc\b\s+-", re.IGNORECASE),
    re.compile(r"\b(sudo|su)\b"),
    re.compile(r"\brm\s+-rf\s+/(?!tmp|workspace)\S*"),
    re.compile(r"\bdocker\b"),
    re.compile(r"/var/run/docker\.sock"),
    re.compile(r"\bchmod\s+777\s+/"),
    re.compile(r":\(\)\s*\{"),                      # fork bomb
]

# 允许的验证器文件扩展名
_ALLOWED_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".java", ".rb", ".rs",
    ".sh", ".bash", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".md",
}


@dataclass
class GeneratedVerifier:
    """生成结果（含用量，便于记账）。"""

    spec: VerifierSpec
    usage: Usage
    warnings: list[str]


def _safe_relpath(path: str) -> str | None:
    """校验并规范化相对路径；非法返回 None。"""
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return None
    if p.startswith(_FORBIDDEN_PREFIXES):
        return None
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return None
    if not parts:
        return None
    return "/".join(parts)


def validate_spec(spec: VerifierSpec) -> list[str]:
    """静态安全校验：返回警告列表；致命问题通过抛异常表达由调用方处理。"""
    warnings: list[str] = []

    # 1. 文件路径
    cleaned: dict[str, str] = {}
    for path, content in spec.files.items():
        safe = _safe_relpath(path)
        if safe is None:
            warnings.append(f"丢弃非法路径的文件：{path}")
            continue
        ext = ("." + safe.rsplit(".", 1)[-1].lower()) if "." in safe.rsplit("/", 1)[-1] else ""
        if ext and ext not in _ALLOWED_EXT:
            warnings.append(f"可疑扩展名（已保留但请注意）：{safe}")
        cleaned[safe] = content
    spec.files = cleaned

    # 2. 命令
    cmd = spec.command or ""
    for pattern in _SUSPICIOUS_PATTERNS:
        if pattern.search(cmd):
            warnings.append(f"命令含可疑片段（{pattern.pattern}）：{cmd[:120]}")
    for path, content in spec.files.items():
        for pattern in _SUSPICIOUS_PATTERNS[:4]:
            if pattern.search(content):
                warnings.append(f"文件 {path} 含可疑片段：{pattern.pattern}")
                break

    # 3. 结构
    if spec.kind.is_executable or spec.kind is VerifierKind.SHELL:
        if not cmd.strip():
            warnings.append("可执行验证器缺少 command")
    if spec.kind is VerifierKind.CHECKLIST and not spec.checklist:
        warnings.append("清单验证器缺少 checklist 条目")

    return warnings


class VerifierGenerator:
    """调用 LLM 生成验证器。"""

    def __init__(self, client: LLMClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.cfg: VerifierConfig = settings.verifier

    # -- 生成 -------------------------------------------------------------

    async def generate(
        self,
        item: RawItem,
        *,
        repo_context: str,
        mode: str | None = None,
        test_hint: str | None = None,
    ) -> GeneratedVerifier:
        """生成验证器（单轮）。"""
        effective_mode = mode or self.cfg.mode
        data, usage = await self.client.chat_json(
            prompts.verifier_prompt(
                item, repo_context=repo_context, mode=effective_mode, test_hint=test_hint
            ),
            purpose="verifier",
            default={},
        )
        spec = self.parse(data, item=item, mode=effective_mode)
        warnings = validate_spec(spec)
        for w in warnings:
            log.warning("验证器校验 %s：%s", item.key, w)
        return GeneratedVerifier(spec=spec, usage=usage, warnings=warnings)

    # -- 修正 -------------------------------------------------------------

    async def refine(
        self,
        spec: VerifierSpec,
        *,
        item: RawItem,
        repo_context: str,
        failure_output: str,
        round_index: int,
        test_hint: str | None = None,
    ) -> GeneratedVerifier:
        """验证器行为不对时，带着证据让模型修正。"""
        messages = prompts.verifier_prompt(
            item, repo_context=repo_context, mode=self.cfg.mode, test_hint=test_hint
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"上一轮你生成的验证器在**修复前**执行结果不符合预期（我们希望它失败，"
                    f"以此证明问题存在）。执行输出如下：\n```\n{failure_output[:4000]}\n```\n\n"
                    f"请修正验证器，使其能精确复现问题。这是第 {round_index} 轮修正。\n"
                    "注意：不要写恒真/恒假的测试；不要依赖网络；只输出 JSON。"
                ),
            }
        )
        data, usage = await self.client.chat_json(messages, purpose="verifier_refine", default={})
        new_spec = self.parse(data, item=item, mode=self.cfg.mode) if data else spec
        warnings = validate_spec(new_spec)
        return GeneratedVerifier(spec=new_spec, usage=usage, warnings=warnings)

    # -- 解析 -------------------------------------------------------------

    def parse(self, data: dict[str, Any], *, item: RawItem, mode: str) -> VerifierSpec:
        """把 LLM 返回的 JSON 解析为 :class:`VerifierSpec`，缺字段时给出可用兜底。"""
        kind = self._parse_kind(data.get("kind"), mode)

        files: dict[str, str] = {}
        raw_files = data.get("files")
        if isinstance(raw_files, dict):
            for k, v in raw_files.items():
                if isinstance(v, str) and v.strip():
                    files[str(k)] = v
        elif isinstance(raw_files, list):
            for entry in raw_files:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content")
                if entry.get("path") and isinstance(content, str) and content.strip():
                    files[str(entry["path"])] = content

        checklist: list[str] = []
        raw_check = data.get("checklist")
        if isinstance(raw_check, list):
            checklist = [str(c).strip() for c in raw_check if str(c).strip()][:20]
        elif isinstance(raw_check, str):
            checklist = [c.strip() for c in raw_check.splitlines() if c.strip()][:20]

        command = data.get("command")
        command = str(command).strip() if isinstance(command, str) and command.strip() else None

        # 兜底：可执行但没给命令时，按语言猜一个
        if kind.is_executable and not command:
            command = self._guess_command(data.get("language"), files)
            if command:
                log.info("验证器未给 command，按语言推断：%s", command)

        timeout = data.get("timeout_seconds")
        try:
            timeout_int = int(timeout) if timeout is not None else 600
        except (TypeError, ValueError):
            timeout_int = 600
        timeout_int = max(30, min(timeout_int, self.settings.sandbox.limits.timeout_seconds * 2))

        return VerifierSpec(
            kind=kind,
            name=str(data.get("name") or f"verify-{item.number}")[:120],
            language=str(data.get("language") or self._guess_language(files))[:32],
            files=files,
            command=command,
            checklist=checklist,
            expect_fail_on_base=bool(data.get("expect_fail_on_base", True)),
            expect_pass_on_fix=bool(data.get("expect_pass_on_fix", True)),
            timeout_seconds=timeout_int,
            notes=str(data.get("notes") or "")[:2000],
        )

    def _parse_kind(self, value: Any, mode: str) -> VerifierKind:
        text = str(value or "").strip().lower()
        for k in VerifierKind:
            if k.value == text:
                return k
        # 按配置模式兜底
        if mode == "checklist":
            return VerifierKind.CHECKLIST
        if not self.cfg.allow_checklist_fallback and mode == "hybrid":
            return VerifierKind.EXECUTABLE
        return VerifierKind.EXECUTABLE

    @staticmethod
    def _guess_language(files: dict[str, str]) -> str:
        for path in files:
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            mapping = {
                "py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript",
                "ts": "typescript", "go": "go", "java": "java", "rb": "ruby", "rs": "rust",
                "sh": "shell", "bash": "shell",
            }
            if ext in mapping:
                return mapping[ext]
        return "python"

    @staticmethod
    def _guess_command(language: Any, files: dict[str, str]) -> str | None:
        """按语言与文件推断运行命令（Q15/Q16 的兜底）。"""
        lang = str(language or "").lower()
        if not lang:
            lang = VerifierGenerator._guess_language(files)
        targets = list(files.keys())
        joined = " ".join(targets)

        if lang == "python":
            test_file = next((t for t in targets if t.endswith(".py") and "test" in t), None)
            if test_file:
                return f"python -m pytest {test_file} -q"
            if targets:
                return f"python {' '.join(targets)}"
        if lang in ("javascript", "typescript"):
            test_file = next((t for t in targets if "test" in t or "spec" in t), None)
            if test_file:
                return f"npx --no-install jest {test_file} --ci" if "jest" in joined else f"node {test_file}"
        if lang == "go":
            return "go test ./..."
        if lang == "java":
            return "mvn -q -B test"
        if lang == "ruby":
            return "bundle exec rspec"
        if lang == "shell" and targets:
            return f"sh {targets[0]}"
        return None
