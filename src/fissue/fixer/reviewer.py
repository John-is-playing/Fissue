"""自动修复的「评审」环节：F2P 与回归门挡得住「弄坏别处」，挡不住「写得脏」。

实测（Fissue #1）：一份三关全过的补丁，把 webhook 的投递去重写成了**模块级可变全局**，
并靠「`signature_ok` 先写、`parse_event` 后读」这种调用顺序隐式传值——测试全绿，人眼一眼看出。
所以这里补一道**只看改动本身**的评审，把意见交回给修复 Agent 返修。

两条设计约束（都不只是为了好看）：

* **只看快照**：评审吃的是 diff 与改动后的文件内容，不读工作区。这样它能与回归门**并行**——
  回归门会临时挪走工作区里的测试文件再还原，谁读它的工作区谁就踩到中间态。
* **反幻觉**：每条发现必须引用**改动里原样出现的片段**、且文件必须在改动白名单里，否则丢弃。
  评审模型最典型的失效就是「为了显得尽职而编问题」，宁缺毋滥比多报几条重要得多。

严重度分三档：`critical` / `major` / `nit`；**只有前两档触发返修**，nit 只记录——否则每轮都为
一点风格来回折腾，白烧 token。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from ..ai import prompts
from ..ai.client import LLMClient, Usage
from ..config import Settings
from ..logging_setup import get_logger
from ..models import RawItem, VerifierSpec

log = get_logger(__name__)

#: 允许的严重度；认不出的降级为 ``nit``（宁可少报，也不要因为一个拼错就触发返修）。
SEVERITIES = ("critical", "major", "nit")
#: 会触发返修的严重度。
BLOCKING_SEVERITIES = ("critical", "major")


@dataclass
class ReviewFinding:
    """一条评审发现。``quote`` 是硬要求：改动里原样出现的片段。"""

    severity: str
    file: str
    quote: str
    why: str
    suggestion: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    def render(self) -> str:
        head = f"[{self.severity}] {self.file}"
        body = f"  片段：{self.quote.strip()[:200]}\n  问题：{self.why.strip()}"
        if self.suggestion.strip():
            body += f"\n  建议：{self.suggestion.strip()}"
        return f"{head}\n{body}"


@dataclass
class ReviewResult:
    """一次评审的结果。``dropped`` 记录被反幻觉过滤掉的条目（便于排查评审质量）。"""

    conclusion: str = ""
    findings: list[ReviewFinding] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    usage: Usage | None = None

    @property
    def blocking(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.blocking]

    def feedback(self) -> str:
        """给修复 Agent 的返修反馈：只带**阻断级**条目，且逐条附原文片段。"""
        items = self.blocking
        if not items:
            return ""
        head = "自动评审在你的改动里发现了这些问题（必须处理）："
        return head + "\n" + "\n".join(f.render() for f in items)

    def summary_lines(self) -> list[str]:
        """记录用的一行摘要（含 nit——它们不触发返修，但人工复核应该看得到）。"""
        lines = []
        if self.conclusion.strip():
            lines.append(self.conclusion.strip())
        lines.extend(f.render().replace("\n", " ｜ ") for f in self.findings)
        return lines


def _normalize(text: Any) -> str:
    """把文本压成「忽略空白差异」的形态，用于判断某段代码是否真的出现在改动里。"""
    return "".join(str(text or "").split())


def parse_review(
    data: dict[str, Any],
    *,
    changed_files: dict[str, str],
    diff: str = "",
    usage: Usage | None = None,
) -> ReviewResult:
    """把评审模型的 JSON 解析成结果，并做**反幻觉过滤**。

    三条过滤（任一不满足就丢弃该条，并记进 ``dropped``）：

    1. 必须给出改动白名单里的文件；
    2. 必须给出非空 ``quote``，且它**原样出现在 diff 或该文件内容里**（忽略空白差异）；
    3. 必须给出非空 ``why``。

    ``findings`` 为空是**完全合法**的结果（而且我们希望它常见）。
    """
    result = ReviewResult(conclusion=str(data.get("conclusion") or "")[:500], usage=usage)
    raw = data.get("findings")
    if not isinstance(raw, list):
        return result

    haystack = _normalize(diff) + "".join(_normalize(c) for c in changed_files.values())
    for entry in raw[:20]:
        if not isinstance(entry, dict):
            result.dropped.append(f"非对象条目：{str(entry)[:80]}")
            continue
        severity = str(entry.get("severity") or "").strip().lower()
        if severity not in SEVERITIES:
            severity = "nit"
        path = str(entry.get("file") or "").strip().replace("\\", "/").lstrip("./")
        quote = str(entry.get("quote") or "").strip()
        why = str(entry.get("why") or "").strip()
        if path not in changed_files:
            result.dropped.append(f"{path or '(未给文件)'}：不在本次改动里")
            continue
        if not quote or _normalize(quote) not in haystack:
            result.dropped.append(f"{path}：引用的片段在改动里找不到（疑似臆造）")
            continue
        if not why:
            result.dropped.append(f"{path}：没说明为什么是问题")
            continue
        result.findings.append(
            ReviewFinding(
                severity=severity,
                file=path,
                quote=quote,
                why=why,
                suggestion=str(entry.get("suggestion") or "").strip(),
            )
        )
    return result


class PatchReviewer:
    """调用 LLM 评审一份修复。"""

    def __init__(self, client: LLMClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    @property
    def cfg(self) -> Any:
        return self.settings.auto_fix

    def _client_for_review(self) -> tuple[LLMClient, bool]:
        """返回（客户端, 是否临时创建）。

        `auto_fix.review_model` 非空时另建一个**只改模型**的客户端（写与审用不同模型，不容易犯
        同一个盲点）；它即用即关，避免在长驻进程里漏掉 httpx 连接。
        """
        name = (getattr(self.cfg, "review_model", "") or "").strip()
        if not name:
            return self.client, False
        cfg = self.settings.llm.model_copy(update={"model": name})
        return LLMClient(cfg), True

    async def review(
        self,
        *,
        item: RawItem,
        spec: VerifierSpec,
        diff: str,
        changed_files: dict[str, str],
        repo_context: str = "",
    ) -> ReviewResult:
        """评审一次；拿不到可用结果时返回空结果（评审**绝不该**阻断主流程）。"""
        if not diff.strip() and not changed_files:
            return ReviewResult(conclusion="没有可评审的改动")
        client, temporary = self._client_for_review()
        try:
            data, usage = await client.chat_json(
                prompts.fix_review_prompt(
                    item,
                    diff=diff,
                    changed_files=changed_files,
                    verifier_command=spec.command or "",
                    repo_context=repo_context,
                ),
                purpose=f"review:{item.repo}",
                default={},
            )
        except Exception:                      # 评审是附加环节：失败只记录，不阻断
            log.exception("评审调用失败（不影响主流程）")
            return ReviewResult(conclusion="评审调用失败")
        finally:
            if temporary:
                with contextlib.suppress(Exception):   # 关闭失败无关紧要
                    await client.aclose()
        return parse_review(data, changed_files=changed_files, diff=diff, usage=usage)
