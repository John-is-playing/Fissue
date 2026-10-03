"""导出：把评测结果输出为 JSON / Markdown。

设计：导出是**纯函数**（从仓储读数据 → 返回字符串），写文件由调用方决定，
方便 CLI、API、Web 复用同一套渲染逻辑。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..models import (
    Action,
    Category,
    Evaluation,
    ItemType,
    Platform,
    RawItem,
)
from ..store.repository import Repository

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def item_to_dict(
    item: RawItem,
    evaluation: Evaluation | None = None,
    *,
    batch: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """单条目 → 可序列化 dict。"""
    data: dict[str, Any] = {
        "key": item.key,
        "platform": item.platform.value,
        "repo": item.repo,
        "number": item.number,
        "type": item.item_type.value,
        "url": item_url(item),
        "title": item.title,
        "state": item.state,
        "labels": item.labels,
        "author": item.author,
        "author_kind": item.author_kind.value,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "comments": len(item.comments),
        "linked_issues": item.linked_issues,
    }
    if item.item_type is ItemType.PR:
        data["merge"] = {
            "merged": item.merged,
            "draft": item.is_draft,
            "mergeable": item.mergeable,
            "base_branch": item.base_branch,
            "head_branch": item.head_branch,
            "head_repo": item.head_repo,
            "additions": item.additions,
            "deletions": item.deletions,
            "changed_files": item.changed_files,
        }
    if evaluation is not None:
        data["evaluation"] = {
            "scores": evaluation.scores.as_dict(),
            "dimensions": {
                name: evaluation.scores.get(name).model_dump(mode="json")
                for name in ("authenticity", "importance", "feasibility", "pr_quality", "difficulty")
                if evaluation.scores.get(name) is not None
            },
            "category": evaluation.category.value,
            "action": evaluation.action.value,
            "priority": evaluation.priority.value,
            "labels_suggested": evaluation.labels_suggested,
            "summary": evaluation.summary,
            "spam": evaluation.spam.model_dump(mode="json"),
            "model": evaluation.model,
            "tokens": {
                "prompt": evaluation.prompt_tokens,
                "completion": evaluation.completion_tokens,
            },
            "cost_usd": round(evaluation.cost_usd, 6),
            "evaluated_at": _iso(evaluation.created_at),
        }
    if batch is not None:
        data["batch_conclusion"] = {
            "verdict": batch.verdict,
            "labels": batch.labels,
            "priority": batch.priority.value,
            "reason": batch.reason,
            "confidence": batch.confidence,
        }
    if extra:
        data.update(extra)
    return data


def item_url(item: RawItem) -> str:
    """条目的 Web 链接。"""
    owner, _, name = item.repo.partition("/")
    num = item.number
    match item.platform:
        case Platform.GITHUB:
            path = "issues" if item.item_type is ItemType.ISSUE else "pull"
            return f"https://github.com/{owner}/{name}/{path}/{num}"
        case Platform.GITEE:
            path = "issues" if item.item_type is ItemType.ISSUE else "pulls"
            return f"https://gitee.com/{owner}/{name}/{path}/{num}"
        case Platform.ATOMGIT:
            path = "issues" if item.item_type is ItemType.ISSUE else "pulls"
            return f"https://atomgit.com/{owner}/{name}/{path}/{num}"
        case Platform.GITLAB:
            path = "issues" if item.item_type is ItemType.ISSUE else "merge_requests"
            return f"https://gitlab.com/{owner}/{name}/-/{path}/{num}"
    return f"{item.platform.value}:{item.repo}#{num}"  # pragma: no cover


def export_json(
    repo: Repository,
    *,
    repo_slug: str | None = None,
    status: Any | None = None,
    category: Category | None = None,
    limit: int = 1000,
    indent: int = 2,
) -> str:
    """导出条目 + 评测结论为 JSON 字符串。"""
    items = repo.list_items(repo_slug=repo_slug, status=status, category=category, limit=limit)
    keys = [i.key for i in items]
    evals = repo.evaluations_for(keys)
    payload = {
        "generated_at": _iso(datetime.now().astimezone()),
        "count": len(items),
        "summary": summarize(repo, repo_slug=repo_slug),
        "items": [
            item_to_dict(i, evals.get(i.key), batch=repo.latest_batch_conclusion(i.key))
            for i in items
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=indent, default=str)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_ACTION_CN = {
    Action.FIX_NOW: "立即修复",
    Action.TRIAGE: "分诊",
    Action.ANSWER: "答疑",
    Action.BACKLOG: "挂号待排",
    Action.CLOSE: "建议关闭",
    Action.NEEDS_INFO: "需要更多信息",
}

_CATEGORY_CN = {
    Category.BUG: "BUG",
    Category.FEATURE: "FEATURE",
    Category.UNKNOWN: "未分类",
}

# 维度中文名（报告给中文维护者看，直观很多）
_DIM_CN = {
    "authenticity": "真实性",
    "importance": "重要性",
    "feasibility": "可行性",
    "pr_quality": "PR 质量",
    "difficulty": "修复难度",
}


def export_markdown(
    repo: Repository,
    *,
    repo_slug: str | None = None,
    status: Any | None = None,
    category: Category | None = None,
    limit: int = 200,
    title: str | None = None,
    include_body: bool = False,
) -> str:
    """导出为 Markdown 报告。"""
    items = repo.list_items(repo_slug=repo_slug, status=status, category=category, limit=limit)
    keys = [i.key for i in items]
    evals = repo.evaluations_for(keys)
    stats = summarize(repo, repo_slug=repo_slug)

    lines: list[str] = []
    head = title or ("Fissue 评测报告" + (f" · {repo_slug}" if repo_slug else ""))
    lines.append(f"# {head}")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}")
    lines.append("")

    # 概览统计
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---|")
    lines.append(f"| 条目总数 | {stats['total']} |")
    for name, count in stats["by_type"].items():
        lines.append(f"| {name.upper()} | {count} |")
    for name, count in stats["by_category"].items():
        lines.append(f"| 分类 {_CATEGORY_CN.get(Category(name), name)} | {count} |")
    for name, count in stats["by_priority"].items():
        lines.append(f"| 优先级 {name} | {count} |")
    if stats["avg_scores"]:
        for dim, val in stats["avg_scores"].items():
            lines.append(f"| 平均{_DIM_CN.get(dim, dim)} | {val} |")
    lines.append("")

    # 明细
    lines.append("## 明细")
    lines.append("")
    for item in items:
        ev = evals.get(item.key)
        lines.append(f"### #{item.number} {item.title}")
        lines.append("")
        lines.append(f"- 类型：`{item.item_type.value}` · 平台：{item.platform.display} · 状态：`{item.state}`")
        lines.append(f"- 链接：{item_url(item)}")
        lines.append(f"- 作者：{item.author}（{item.author_kind.value}）· 标签：{', '.join(item.labels) or '无'}")
        if ev is not None:
            scores = " / ".join(f"{_DIM_CN.get(k, k)}={v}" for k, v in ev.scores.as_dict().items())
            lines.append(f"- 评分：{scores}")
            lines.append(
                f"- 分类：{_CATEGORY_CN.get(ev.category, ev.category.value)} · "
                f"优先级：`{ev.priority.value}` · 建议动作：{_ACTION_CN.get(ev.action, ev.action.value)}"
            )
            if ev.labels_suggested:
                lines.append(f"- 建议标签：{', '.join(ev.labels_suggested)}")
            if ev.spam.suspicious:
                lines.append(f"- ⚠️ 疑似：{'重复' if ev.spam.is_duplicate else ''}{'刷量' if ev.spam.is_spam else ''} "
                             f"{('（' + '；'.join(ev.spam.reasons) + '）') if ev.spam.reasons else ''}")
            if ev.summary:
                lines.append(f"- 结论：{ev.summary}")
        concl = repo.latest_batch_conclusion(item.key)
        if concl is not None:
            lines.append(f"- 批量结论：`{concl.verdict}`（{concl.reason}）")
        if include_body and item.body:
            lines.append("")
            lines.append("```text")
            lines.append(item.body.strip()[:4000])
            lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 统计与写文件
# ---------------------------------------------------------------------------


def summarize(repo: Repository, *, repo_slug: str | None = None) -> dict[str, Any]:
    """概览统计。"""
    items = repo.list_items(repo_slug=repo_slug, limit=100000)
    by_type = Counter(i.item_type.value for i in items)
    by_category = Counter(i.category.value for i in items)
    by_priority = Counter(i.priority.value for i in items)
    by_status = Counter(i.status.value for i in items)

    evals = repo.evaluations_for([i.key for i in items])
    dims: dict[str, list[int]] = {}
    for ev in evals.values():
        for name, val in ev.scores.as_dict().items():
            dims.setdefault(name, []).append(val)
    avg_scores = {k: round(sum(v) / len(v), 1) for k, v in dims.items() if v}

    return {
        "total": len(items),
        "by_type": dict(by_type),
        "by_category": dict(by_category),
        "by_priority": dict(by_priority),
        "by_status": dict(by_status),
        "avg_scores": avg_scores,
    }


def write_export(content: str, path: str | Path) -> Path:
    """把导出内容写入文件（自动建目录）。"""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def export(
    repo: Repository,
    fmt: str = "json",
    *,
    out: str | Path | None = None,
    **kwargs: Any,
) -> Path | str:
    """统一导出入口：``fmt`` 为 json/markdown/md。"""
    fmt_norm = fmt.lower()
    if fmt_norm in ("json",):
        content = export_json(repo, **kwargs)
    elif fmt_norm in ("markdown", "md"):
        content = export_markdown(repo, **kwargs)
    else:
        raise ValueError(f"不支持的导出格式：{fmt}（可选 json / markdown）")
    if out:
        return write_export(content, out)
    return content


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None
