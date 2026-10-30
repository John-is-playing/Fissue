"""任务结果的可读化：把作业返回的原始 dict 转成页面上能一眼看懂的样子。

## 为什么要有这一层

作业完成后，``Job.result`` 里是一坨结构化 dict（不同作业形状还不一样）。模板原先直接
``{{ job.result | tojson(indent=2) }}``——技术上没丢信息，但没人愿意为「这次扫描新增了几条」
去读一段 JSON。

这一层只做**呈现**，不改数据：

* ``headline``：一句话结论（「新增 2 条」「验证 10 条，复现 7 条」）；
* ``facts``：关键数字，横排显示；
* ``columns`` / ``rows``：逐条明细（已转成字符串，模板不用再判类型）;
* ``tone``：整体成败，决定颜色；
* 原始 dict 仍由模板折叠保留——排查时要看原样，删掉反而碍事。

## 为什么是白名单而不是「通用渲染」

每种作业的 result 形状是**约定**（由 `web/app.py` 的 `_work_*` 决定），不是任意结构。
按种类写清楚「哪几个数字重要」，页面才说得上是人话；一个通用的递归渲染器只会把
JSON 换个排版再还给用户。

遇到不认识的种类/形状时回退到「列出标量字段」——新增作业种类不必同时改这里才能跑，
只是没那么好看。
"""

from __future__ import annotations

from typing import Any

#: 明细为空时的占位行（模板据此显示「（无）」而不是一张空表）。
_EMPTY_ROW = ["（无）", "", "", "", ""]


def summarize_result(kind: str, result: dict[str, Any] | None) -> dict[str, Any]:
    """把作业结果转成可读摘要。

    :param kind: 作业种类（``scan`` / ``evaluate`` / ``verify`` / ``fix`` / ``force`` / ``flush``）
    :param result: 作业返回的原始 dict（可能是 ``None`` 或空 dict）
    """
    # 先按**种类**分派，不按形状猜：同一种作业的结果形状是约定，猜形状会在
    # 「results 为空」和「结果里恰好有个同名键」时给出错误解读。
    handlers = {
        "scan": _scan,
        "evaluate": _evaluate,
        "verify": _verify,
        "fix": _fix,
        "force": _force,
        "flush": _flush,
    }
    handler = handlers.get(kind)
    if handler is not None:
        return handler(result or {})
    return _generic(result or {})


# ---------------------------------------------------------------------------
# 各作业种类
# ---------------------------------------------------------------------------


def _scan(r: dict[str, Any]) -> dict[str, Any]:
    fetched = _int(r.get("fetched"))
    created = _int(r.get("created"))
    updated = _int(r.get("updated"))
    unchanged = _int(r.get("unchanged"))
    error = r.get("error")

    if error:
        headline = f"抓取出错：{error}"
        tone = "bad"
    elif fetched == 0:
        headline = "抓取完成，没有拉到新内容"
        tone = "warn"
    else:
        headline = f"抓取完成：新增 {created} 条、更新 {updated} 条"
        tone = "ok"

    return _view(
        headline,
        [("拉取", fetched), ("新增", created), ("更新", updated), ("未变", unchanged)],
        tone=tone,
    )


def _evaluate(r: dict[str, Any]) -> dict[str, Any]:
    evaluated = _int(r.get("evaluated"))
    failed = _int(r.get("failed"))
    rows = [
        [
            _cell(x.get("key")),
            _cell(x.get("category") or "—"),
            _cell(x.get("priority") or "—"),
            _cell(x.get("error") or ""),
        ]
        for x in (r.get("results") or [])
    ]

    if not evaluated and not failed:
        headline, tone = "没有需要评测的条目", "warn"
    elif failed:
        headline = f"评测 {evaluated + failed} 条：成功 {evaluated}、失败 {failed}"
        tone = "bad" if evaluated == 0 else "warn"
    else:
        headline, tone = f"评测完成：{evaluated} 条都拿到了结论", "ok"

    return _view(
        headline,
        [("成功", evaluated), ("失败", failed)],
        tone=tone,
        columns=["条目", "分类", "优先级", "错误"],
        rows=rows,
    )


def _verify(r: dict[str, Any]) -> dict[str, Any]:
    processed = _int(r.get("processed"))
    results = list(r.get("results") or [])
    reproduced = sum(1 for x in results if x.get("reproduced"))
    queued = sum(1 for x in results if x.get("queued"))
    errored = sum(1 for x in results if x.get("error"))

    rows = [
        [
            _cell(x.get("key")),
            "是" if x.get("reproduced") else "否",
            _cell(x.get("queued") or "—"),
            _cell(x.get("error") or ""),
        ]
        for x in results
    ]

    if processed == 0:
        headline, tone = "没有待验证的条目", "warn"
    elif errored:
        headline = f"验证 {processed} 条：复现 {reproduced}、异常 {errored}"
        tone = "warn"
    else:
        headline = f"验证完成：{processed} 条里复现 {reproduced} 条"
        tone = "ok"

    return _view(
        headline,
        [("已处理", processed), ("已复现", reproduced), ("已入队", queued), ("异常", errored)],
        tone=tone,
        columns=["条目", "已复现", "入队", "错误"],
        rows=rows,
    )


def _fix(r: dict[str, Any]) -> dict[str, Any]:
    attempted = _int(r.get("attempted"))
    dry_run = bool(r.get("dry_run"))
    results = list(r.get("results") or [])
    prs = [x for x in results if x.get("pr_url")]
    errored = [x for x in results if x.get("error")]

    rows = [
        [
            _cell(x.get("key")),
            _cell(x.get("outcome") or "—"),
            _cell(x.get("rounds") if x.get("rounds") is not None else "—"),
            _cell(x.get("pr_url") or x.get("patch_path") or x.get("branch") or "—"),
            _cell(x.get("error") or ""),
        ]
        for x in results
    ]

    if attempted == 0:
        headline, tone = "没有可修的条目", "warn"
    elif dry_run:
        # dry-run 不产 PR，但产补丁——说清楚这一点，否则「试跑」看起来像什么都没做。
        headline = f"试跑修复 {attempted} 条（只产补丁，未提 PR）"
        tone = "warn" if errored else "ok"
    elif prs:
        headline = f"修复 {attempted} 条：提了 {len(prs)} 个 PR"
        tone = "ok"
    else:
        headline = f"修复 {attempted} 条：没有提出 PR"
        tone = "bad" if errored else "warn"

    return _view(
        headline,
        [("尝试", attempted), ("出 PR", len(prs)), ("出错", len(errored))],
        tone=tone,
        columns=["条目", "结果", "轮次", "PR / 补丁 / 分支", "错误"],
        rows=rows,
    )


def _force(r: dict[str, Any]) -> dict[str, Any]:
    processed = _int(r.get("processed"))
    results = list(r.get("results") or [])
    errored = [x for x in results if x.get("error")]

    rows = [
        [
            _cell(x.get("key")),
            _cell(x.get("status") or "—"),
            _cell(x.get("note") or x.get("error") or ""),
        ]
        for x in results
    ]

    if processed == 0:
        headline, tone = "没有处理任何条目", "warn"
    elif errored:
        headline = f"强制修复 {processed} 条：{len(errored)} 条失败"
        tone = "bad" if len(errored) == processed else "warn"
    else:
        headline = f"强制修复完成：{processed} 条已推进修复流程"
        tone = "ok"

    return _view(
        headline,
        [("已处理", processed), ("失败", len(errored))],
        tone=tone,
        columns=["条目", "状态", "说明"],
        rows=rows,
    )


def _flush(r: dict[str, Any]) -> dict[str, Any]:
    results = list(r.get("results") or [])
    flushed = sum(_int(x.get("flushed")) for x in results)
    conclusions = sum(_int(x.get("conclusions")) for x in results)
    errored = [x for x in results if x.get("error")]

    rows = [
        [
            _cell(x.get("queue")),
            _cell(_int(x.get("flushed"))),
            _cell(_int(x.get("conclusions"))),
            _cell(x.get("reason") or x.get("error") or ""),
        ]
        for x in results
    ]

    if not results:
        headline, tone = "没有队列需要处理", "warn"
    elif errored:
        headline = f"flush {len(results)} 个队列：{len(errored)} 个出错"
        tone = "bad"
    elif flushed == 0:
        headline, tone = "flush 完成：本次没有刷出结论（多数队列未到阈值）", "warn"
    else:
        headline = f"flush 完成：刷出 {flushed} 条结论"
        tone = "ok"

    return _view(
        headline,
        [("队列", len(results)), ("刷出", flushed), ("结论", conclusions)],
        tone=tone,
        columns=["队列", "刷出", "结论", "原因 / 错误"],
        rows=rows,
    )


def _generic(r: dict[str, Any]) -> dict[str, Any]:
    """兜底：把标量字段列成「键 = 值」。

    不认识的作业种类不该在页面上变成一块空白——列出能看懂的部分，剩下的仍可从
    折叠的原始数据里查。
    """
    if not r:
        return _view("完成（没有返回数据）", [], tone="ok")
    facts = [
        (_pretty_key(k), _cell(v))
        for k, v in r.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    ]
    nested = [k for k, v in r.items() if isinstance(v, (list, dict))]
    headline = "完成"
    if nested:
        headline += f"（另有 {len(nested)} 个结构化字段，见原始数据）"
    return _view(headline, facts, tone="ok")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _view(
    headline: str,
    facts: list[tuple[str, Any]],
    *,
    tone: str = "ok",
    columns: list[str] | None = None,
    rows: list[list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "headline": headline,
        "facts": [(label, _cell(value)) for label, value in facts],
        "tone": tone,
        "columns": columns or [],
        "rows": rows or [],
    }


def _int(value: Any) -> int:
    """取整数；取不到就当 0（结果里缺字段不该让整页渲染失败）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _pretty_key(key: str) -> str:
    """``total_tokens`` → ``total tokens``（兜底视图里给可读的字段名）。"""
    return key.replace("_", " ")
