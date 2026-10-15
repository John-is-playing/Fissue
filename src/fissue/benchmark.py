"""跑分基线：把「一次测量的全部前提与结果」冻结成机器可读的记录。

为什么需要它
------------
``BENCHMARK.md`` §5 自己承认：**「冻结版本的可复现跑分不存在」**。
而本文档的所有数字都来自「开发期累计运行」——期间 Fissue 自身在改代码，
不同夹具实际上跑在**不同版本**的 Fissue 上，再叠加 LLM 的非确定性，
导致那些数字**无法回答**一个最基本的问题：

    这次改动，让结果变好了还是变坏了？

本模块把「测量」变成可对比的快照：一次 capture 记录

* **前提**：git commit + 是否有未提交改动、模型与采样参数、关键配置指纹；
* **结果**：各夹具的条目分布、各阶段 outcome 计数、F2P/回归门/修复结果。

于是 ``diff`` 能给出**逐项增量**，而不是靠人眼比对两份 Markdown 表格。

设计取舍
--------
* **只记计数与分布，不记正文/stdout**：基线要能长期、廉价地进版本库；
  单条证据仍在 ``data/artifacts/`` 与数据库里。
* **明确区分「可比」与「不可比」**：commit 或模型不同时，``diff`` 会显式标出
  ``comparable=False``——免得拿两个不该比的东西当回归结论。这正是原文档
  「非冻结」这一局限要被消灭的地方。
* 前提缺失时**如实标注**（例如 ``git`` 不可用），不假装完整。
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .models import VerifierOutcome
from .store.db import Database
from .store.tables import (
    BatchConclusionRow,
    EvaluationRow,
    FixAttemptRow,
    ItemRow,
    VerifierRow,
    VerifierRunRow,
)

log = get_logger(__name__)

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 前提：版本 / 模型 / 配置
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """项目根目录（``src/fissue/benchmark.py`` → 上溯三级）。"""
    return Path(__file__).resolve().parents[2]


def git_state(root: Path | None = None) -> dict[str, Any]:
    """当前 git 状态：commit、分支、是否有未提交改动、改动文件数。

    为什么要 ``dirty``：脏工作区上的测量**不可复现**——代码随时可能变。
    带上它，比对时才能判断「两份基线到底是不是同一份代码跑出来的」。
    """
    base = root or project_root()

    def _git(*args: str) -> str:
        try:
            r = subprocess.run(
                ["git", *args], cwd=str(base), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    commit = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git("status", "--porcelain")
    changed = [ln for ln in porcelain.splitlines() if ln.strip()]
    return {
        "commit": commit or None,
        "branch": branch or None,
        "dirty": bool(changed),          # 有未提交改动 = 不可复现
        "dirty_files": len(changed),
    }


def config_fingerprint(settings: Any) -> dict[str, Any]:
    """关键配置指纹：影响测量结果的那些开关与阈值。

    刻意只取**判定口径相关**的项（而非整份 config）：
    它要能回答「两次跑分为什么不一样」，又不能在 diff 里被无关噪音淹没。
    """
    return {
        "llm": {
            "model": settings.llm.model,
            "fallback_model": settings.llm.fallback_model,
            # base_url 只留域名，避免把内网地址写进版本库
            "base_url_host": _host_of(settings.llm.base_url),
            "temperature": settings.llm.temperature,
            "max_tokens": settings.llm.max_tokens,
            "concurrency": settings.llm.concurrency,
        },
        "verifier": {
            "mode": settings.verifier.mode,
            "require_f2p": settings.verifier.require_f2p,
            "max_rounds": settings.verifier.max_rounds,
            # 回归门档位直接决定「假修复拦不拦得住」，必须进指纹
            "regression_gate": settings.verifier.regression_gate,
            "concurrency": settings.verifier.concurrency,
            "item_retries": settings.verifier.item_retries,
        },
        "auto_fix": {
            "enabled": settings.auto_fix.enabled,
            "agent_max_rounds": settings.auto_fix.agent_max_rounds,
            "stall_rounds": settings.auto_fix.stall_rounds,
            "on_failure": settings.auto_fix.on_failure,
            "pr_strategy_mode": settings.auto_fix.pr_strategy.mode,
        },
        "fix_policy": {
            "tier_strategy": settings.fix_policy.tier_strategy,
            "tier1_min_importance": settings.fix_policy.tier1.min_importance,
            "tier1_max_difficulty": settings.fix_policy.tier1.max_difficulty,
            "tier2_min_importance": settings.fix_policy.tier2.min_importance,
            "only_issues": settings.fix_policy.only_issues,
        },
        "sandbox": {
            "enabled": settings.sandbox.enabled,
            "runtime": settings.sandbox.runtime,
            "image": settings.sandbox.image,
            "network": settings.sandbox.network,
        },
        "repos": sorted(r.slug for r in settings.repos if r.enabled),
    }


def _host_of(url: str | None) -> str:
    if not url:
        return ""
    text = str(url)
    for sep in ("//",):
        if sep in text:
            text = text.split(sep, 1)[1]
    return text.split("/", 1)[0]


# ---------------------------------------------------------------------------
# 结果：从库里聚合
# ---------------------------------------------------------------------------


def _counts(values: list[str]) -> dict[str, int]:
    """按值计数，只保留出现过的键（保证 diff 里不出现一排 0）。"""
    return dict(sorted(Counter(v for v in values if v).items()))


def aggregate(db: Database, *, repo_slug: str | None = None) -> dict[str, Any]:
    """聚合当前库里的测量结果（只读）。

    分四块：条目分布、评测、验证器与各阶段运行、自动修复。
    每块都是**可逐键求差**的计数/均值，便于 ``diff_baselines`` 机械比对。
    """
    from sqlalchemy import select

    with db.session() as s:
        item_stmt = select(ItemRow)
        if repo_slug:
            item_stmt = item_stmt.where(ItemRow.repo_slug == repo_slug)
        items = list(s.scalars(item_stmt))
        keys = [i.key for i in items]

        evals = list(
            s.scalars(select(EvaluationRow).where(EvaluationRow.item_key.in_(keys)))
        ) if keys else []
        # 只取每个条目的**最新**一次评测（与文档「取最新一次结论」口径一致）
        latest_eval: dict[str, EvaluationRow] = {}
        for e in evals:
            cur = latest_eval.get(e.item_key)
            if cur is None or e.id > cur.id:
                latest_eval[e.item_key] = e

        verifiers = list(
            s.scalars(select(VerifierRow).where(VerifierRow.item_key.in_(keys)))
        ) if keys else []
        runs = list(
            s.scalars(select(VerifierRunRow).where(VerifierRunRow.item_key.in_(keys)))
        ) if keys else []
        fixes = list(
            s.scalars(select(FixAttemptRow).where(FixAttemptRow.item_key.in_(keys)))
        ) if keys else []
        conclusions = list(
            s.scalars(select(BatchConclusionRow).where(BatchConclusionRow.item_key.in_(keys)))
        ) if keys else []

    # 评测维度均值（只统计拿到最新评测的条目）
    dims: dict[str, list[int]] = {}
    for e in latest_eval.values():
        scores = e.scores or {}
        for name, payload in scores.items():
            if isinstance(payload, dict) and isinstance(payload.get("score"), (int, float)):
                dims.setdefault(name, []).append(int(payload["score"]))

    return {
        "items": {
            "total": len(items),
            # 覆盖了哪些仓库——`diff` 判断「两次测量是否同一范围」的依据。
            # 少了它，范围变化会被读成能力变化（见 `diff` 里的说明）。
            "by_repo": _counts([i.repo_slug for i in items]),
            "by_item_type": _counts([i.item_type for i in items]),
            "by_category": _counts([i.category for i in items]),
            "by_priority": _counts([i.priority for i in items]),
            "by_status": _counts([i.status for i in items]),
        },
        "evaluation": {
            "evaluated_items": len(latest_eval),
            "by_category": _counts([e.category for e in latest_eval.values()]),
            "by_priority": _counts([e.priority for e in latest_eval.values()]),
            "spam_or_duplicate": sum(
                1 for e in latest_eval.values()
                if (e.spam or {}).get("is_spam") or (e.spam or {}).get("is_duplicate")
            ),
            "avg_scores": {k: round(sum(v) / len(v), 1) for k, v in sorted(dims.items()) if v},
        },
        "verifier": {
            "verifiers": len(verifiers),
            "f2p_satisfied": sum(1 for v in verifiers if v.f2p_satisfied),
            "by_kind": _counts([v.kind for v in verifiers]),
            "runs_total": len(runs),
            "by_stage": _counts([r.stage for r in runs]),
            # 这个交叉表是 BENCHMARK §2.2 那张表的数据源
            "by_stage_outcome": {
                f"{stage}/{outcome}": n
                for (stage, outcome), n in sorted(
                    Counter((r.stage, r.outcome) for r in runs).items()
                )
            },
            "base_error_runs": sum(
                1 for r in runs if r.stage == "base" and r.outcome == VerifierOutcome.ERROR.value
            ),
        },
        "fix": {
            "attempts": len(fixes),
            "by_outcome": _counts([f.outcome for f in fixes]),
            "patches": sum(1 for f in fixes if f.patch_path),
            "prs": sum(1 for f in fixes if f.pr_url),
            "by_rounds": _counts([str(f.rounds) for f in fixes]),
        },
        "conclusions": {
            "total": len(conclusions),
            "by_verdict": _counts([c.verdict for c in conclusions]),
        },
    }


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def capture(
    settings: Any,
    db: Database,
    *,
    repo_slug: str | None = None,
    label: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """冻结一份跑分基线（只读数据库，不触发任何抓取/评测）。"""
    from . import __version__

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "fissue_version": __version__,
        "scope": {"repo": repo_slug or "(全部启用的仓库)"},
        "provenance": {
            "git": git_state(root),
            "config": config_fingerprint(settings),
        },
        "result": aggregate(db, repo_slug=repo_slug),
    }
    record["reproducible"] = not record["provenance"]["git"]["dirty"]
    return record


def save(record: dict[str, Any], path: str | Path) -> Path:
    """落盘（统一 LF、UTF-8、结尾换行，便于进版本库与 diff）。"""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    # newline="" 避免 Windows 上写成 CRLF：基线要能跨平台干净 diff
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return p


def load(path: str | Path) -> dict[str, Any]:
    with open(Path(path).expanduser(), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 对比
# ---------------------------------------------------------------------------


def _flat(prefix: str, value: Any, out: dict[str, Any]) -> dict[str, Any]:
    """把嵌套 dict 摊平成 ``a.b.c -> 标量``，供逐项求差。"""
    if isinstance(value, dict):
        for k, v in value.items():
            _flat(f"{prefix}.{k}" if prefix else str(k), v, out)
    else:
        out[prefix] = value
    return out


def _repo_set(record: dict[str, Any]) -> set[str] | None:
    """基线覆盖的仓库集合；该基线**没记录**这一项时返回 None。

    「没记录」与「记录为空」必须分开：旧 schema 的基线没有
    ``result.items.by_repo``，那是无从判断，不是「一个仓库都没覆盖」。
    """
    by_repo = ((record.get("result") or {}).get("items") or {}).get("by_repo")
    if not isinstance(by_repo, dict) or not by_repo:
        return None
    return {str(k) for k in by_repo}


def _brief(names: list[str], *, limit: int = 3) -> str:
    """名字列表的简短形式：最多列 ``limit`` 个，其余用「等 N 个」收尾。"""
    head = "、".join(names[:limit])
    return head if len(names) <= limit else f"{head} 等 {len(names)} 个"


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """对比两份基线，返回逐项增量与「是否可比」的判定。

    ``comparable=False`` 的三种情形都必须显式说出原因——原文档「非冻结」
    这一局限，正是要靠这里被消灭：不允许拿不可比的两次测量当回归结论。
    """
    reasons: list[str] = []
    g_old = (old.get("provenance") or {}).get("git") or {}
    g_new = (new.get("provenance") or {}).get("git") or {}
    if old.get("schema_version") != new.get("schema_version"):
        reasons.append(
            f"schema 版本不同（{old.get('schema_version')} vs {new.get('schema_version')}）"
        )
    if g_old.get("commit") != g_new.get("commit"):
        reasons.append(f"commit 不同（{str(g_old.get('commit'))[:8]} → {str(g_new.get('commit'))[:8]}）")
    if g_old.get("dirty") or g_new.get("dirty"):
        # 注意：这里不能用生成器里残留的循环变量（会 NameError），显式取名。
        dirty_sides = [
            (name, g.get("dirty_files"))
            for name, g in (("旧", g_old), ("新", g_new))
            if g.get("dirty")
        ]
        detail = "、".join(f"{name}基线 {files} 个文件" for name, files in dirty_sides)
        reasons.append(f"存在未提交改动（{detail}）")

    c_old = (old.get("provenance") or {}).get("config") or {}
    c_new = (new.get("provenance") or {}).get("config") or {}
    model_changed = (c_old.get("llm") or {}).get("model") != (c_new.get("llm") or {}).get("model")
    if model_changed:
        reasons.append(
            f"模型不同（{(c_old.get('llm') or {}).get('model')} → {(c_new.get('llm') or {}).get('model')}）"
        )

    # 条目集（覆盖了哪些仓库）——比 commit / 模型更容易被忽略，却同样能让增量失去意义：
    # 两次测量若覆盖的仓库不同，那么「条目数 ↓47」「fix 通过 ↓55」这类增量里混的是
    # **范围变化**，不是能力变化。实测踩过：旧基线含 textkit/ratekit/pipekit，新基线
    # 只剩 envkit/lockkit，增量几乎全是范围差异，而当时只 flag 了 commit 与模型。
    repos_old = _repo_set(old)
    repos_new = _repo_set(new)
    if repos_old is None and repos_new is None:
        pass                                   # 两侧都没记录 → 无从判断，不妄下结论
    elif repos_old is None or repos_new is None:
        # 只有一侧记录 → 无法确认范围一致。宁可保守：判为不可比并说清原因，
        # 而不是默认可比、让范围差异悄悄混进「回归」结论里。
        side = "旧" if repos_old is None else "新"
        reasons.append(f"{side}基线未记录仓库集（schema 较早），无法确认两次测量的范围是否一致")
    elif repos_old != repos_new:
        only_old = sorted(repos_old - repos_new)
        only_new = sorted(repos_new - repos_old)
        parts = []
        if only_old:
            parts.append(f"旧多出 {_brief(only_old)}")
        if only_new:
            parts.append(f"新多出 {_brief(only_new)}")
        reasons.append(f"仓库集不同（{'；'.join(parts)}）")

    # 配置指纹里除模型外的差异（这些直接改变判定口径）
    cfg_deltas: list[str] = []
    flat_old = _flat("", c_old, {})
    flat_new = _flat("", c_new, {})
    for key in sorted(set(flat_old) | set(flat_new)):
        if key.startswith("llm.model"):
            continue
        if flat_old.get(key) != flat_new.get(key):
            cfg_deltas.append(f"{key}: {flat_old.get(key)!r} → {flat_new.get(key)!r}")

    # 结果逐项求差（前缀带 ``result``，便于在输出里一眼看出是哪一块）
    r_old = _flat("result", old.get("result") or {}, {})
    r_new = _flat("result", new.get("result") or {}, {})
    deltas: list[dict[str, Any]] = []
    for key in sorted(set(r_old) | set(r_new)):
        before = r_old.get(key)
        after = r_new.get(key)
        if before == after:
            continue
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            deltas.append({"key": key, "before": before, "after": after, "delta": round(after - before, 3)})
        else:
            deltas.append({"key": key, "before": before, "after": after, "delta": None})

    return {
        "comparable": not reasons,
        "not_comparable_because": reasons,
        "config_deltas": cfg_deltas,
        "changed": deltas,
        "summary": {
            "items": _delta(deltas, "result.items.total"),
            "base_fail": _delta(deltas, "result.verifier.by_stage_outcome.base/fail"),
            "base_error": _delta(deltas, "result.verifier.base_error_runs"),
            "fix_pass": _delta(deltas, "result.verifier.by_stage_outcome.fix/pass"),
            "f2p_satisfied": _delta(deltas, "result.verifier.f2p_satisfied"),
            "patches": _delta(deltas, "result.fix.patches"),
            "prs": _delta(deltas, "result.fix.prs"),
        },
    }


def _delta(deltas: list[dict[str, Any]], key: str) -> int | float | None:
    for d in deltas:
        if d["key"] == key and isinstance(d["delta"], (int, float)):
            return d["delta"]
    return None


def render_diff(d: dict[str, Any], *, max_items: int = 40) -> str:
    """把对比结果渲染成可读文本（CLI 用）。"""
    lines: list[str] = []
    if d["comparable"]:
        lines.append("✅ 可比：commit / 模型 / 工作区状态一致")
    else:
        lines.append("⚠️  不可比——以下前提不同，结论不能当回归依据：")
        lines.extend(f"   - {r}" for r in d["not_comparable_because"])

    if d["config_deltas"]:
        lines.append("")
        lines.append("配置差异（除模型外）：")
        lines.extend(f"   - {c}" for c in d["config_deltas"])

    lines.append("")
    s = d["summary"]
    labels = [
        ("items", "条目数"),
        ("base_fail", "base 失败(可复现)"),
        ("base_error", "base error(环境错)"),
        ("fix_pass", "fix 通过"),
        ("f2p_satisfied", "F2P 成立"),
        ("patches", "产出补丁"),
        ("prs", "真实 PR"),
    ]
    lines.append("关键指标增量（新 − 旧）：")
    for key, name in labels:
        v = s.get(key)
        if v is None:
            lines.append(f"   {name:<18} —（该项无变化或缺失）")
        else:
            arrow = "↑" if v > 0 else ("↓" if v < 0 else "=")
            lines.append(f"   {name:<18} {arrow} {v:+}")

    if d["changed"]:
        lines.append("")
        lines.append(f"全部变化项（{len(d['changed'])} 条，最多显示 {max_items}）：")
        for item in d["changed"][:max_items]:
            if item["delta"] is None:
                lines.append(f"   {item['key']}: {item['before']} → {item['after']}")
            else:
                lines.append(f"   {item['key']}: {item['before']} → {item['after']} ({item['delta']:+})")
        if len(d["changed"]) > max_items:
            lines.append(f"   …（另有 {len(d['changed']) - max_items} 条）")
    else:
        lines.append("")
        lines.append("（结果完全一致，无变化项）")
    return "\n".join(lines)
