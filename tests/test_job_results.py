"""任务结果可读化的测试。

这一层只做呈现，但它错的方式很难自己发现：结果页会把「0 条」和「没跑」显示成同一个
样子，用户以为任务白跑了。所以每个种类都钉住三件事：

* 结论句说清了「做了什么」；
* 关键数字取的是**对的字段**（不同作业形状不同，混了就是撒谎）；
* 空结果/失败结果有明确的说法，不是一片空白。
"""

from __future__ import annotations

import pytest

from fissue.web.job_results import summarize_result

# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_reports_created_and_updated() -> None:
    v = summarize_result("scan", {"fetched": 5, "created": 2, "updated": 1, "unchanged": 2})
    assert "新增 2" in v["headline"] and "更新 1" in v["headline"]
    assert v["tone"] == "ok"
    assert dict(v["facts"]) == {"拉取": "5", "新增": "2", "更新": "1", "未变": "2"}


def test_scan_with_error_is_bad_tone() -> None:
    v = summarize_result("scan", {"fetched": 0, "created": 0, "updated": 0,
                                  "unchanged": 0, "error": "token 失效"})
    assert "token 失效" in v["headline"]
    assert v["tone"] == "bad"


def test_scan_without_any_fetch_is_warn_not_ok() -> None:
    """「拉到 0 条」和「跑成功了」不是同一件事——用户要知道确实没东西。"""
    v = summarize_result("scan", {"fetched": 0, "created": 0, "updated": 0, "unchanged": 0})
    assert "没有拉到新内容" in v["headline"]
    assert v["tone"] == "warn"


def test_scan_missing_fields_default_to_zero() -> None:
    """结果里缺字段不该崩，也不该显示成空串。"""
    v = summarize_result("scan", {})
    assert dict(v["facts"])["拉取"] == "0"


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def test_evaluate_lists_per_item_results() -> None:
    v = summarize_result("evaluate", {
        "evaluated": 2, "failed": 1,
        "results": [
            {"key": "github:a/b#1", "category": "bug", "priority": "tier1", "error": None},
            {"key": "github:a/b#2", "category": None, "priority": None, "error": "模型 500"},
        ],
    })
    assert "成功 2" in v["headline"] and "失败 1" in v["headline"]
    assert v["columns"] == ["条目", "分类", "优先级", "错误"]
    assert v["rows"][0] == ["github:a/b#1", "bug", "tier1", ""]
    assert v["rows"][1][3] == "模型 500"
    assert v["tone"] == "warn"


def test_evaluate_all_failed_is_bad() -> None:
    v = summarize_result("evaluate", {"evaluated": 0, "failed": 3, "results": []})
    assert v["tone"] == "bad"


def test_evaluate_nothing_to_do() -> None:
    v = summarize_result("evaluate", {"evaluated": 0, "failed": 0, "results": []})
    assert "没有需要评测的条目" in v["headline"]
    assert v["tone"] == "warn"


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_counts_reproduced_and_queued() -> None:
    v = summarize_result("verify", {
        "processed": 3,
        "results": [
            {"key": "k1", "reproduced": True, "queued": "verify", "error": None},
            {"key": "k2", "reproduced": True, "queued": "verify", "error": None},
            {"key": "k3", "reproduced": False, "queued": None, "error": None},
        ],
    })
    facts = dict(v["facts"])
    assert facts["已处理"] == "3"
    assert facts["已复现"] == "2"
    assert facts["已入队"] == "2"
    assert v["rows"][0][1] == "是"
    assert v["rows"][2][1] == "否"


def test_verify_with_errors_is_warn() -> None:
    v = summarize_result("verify", {
        "processed": 2,
        "results": [{"key": "k1", "reproduced": False, "queued": None, "error": "沙盒超时"}],
    })
    assert "异常 1" in v["headline"]
    assert v["tone"] == "warn"


def test_verify_nothing_pending() -> None:
    v = summarize_result("verify", {"processed": 0, "results": []})
    assert "没有待验证的条目" in v["headline"]


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


def test_fix_dry_run_says_no_pr() -> None:
    """试跑不产 PR，必须说清楚——否则「试跑」看起来像什么都没做。"""
    v = summarize_result("fix", {
        "attempted": 1, "dry_run": True,
        "results": [{"key": "k1", "outcome": "needs_manual", "rounds": 2,
                     "patch_path": "/tmp/x.patch", "error": None}],
    })
    assert "试跑" in v["headline"] and "未提 PR" in v["headline"]
    assert v["rows"][0][3] == "/tmp/x.patch"


def test_fix_counts_prs() -> None:
    v = summarize_result("fix", {
        "attempted": 2, "dry_run": False,
        "results": [
            {"key": "k1", "outcome": "success", "pr_url": "https://gh/pr/1"},
            {"key": "k2", "outcome": "failed", "error": "改不动"},
        ],
    })
    assert "提了 1 个 PR" in v["headline"]
    assert dict(v["facts"])["出 PR"] == "1"
    assert v["tone"] == "ok"


def test_fix_nothing_to_fix() -> None:
    v = summarize_result("fix", {"attempted": 0, "dry_run": True, "results": []})
    assert "没有可修的条目" in v["headline"]


# ---------------------------------------------------------------------------
# force
# ---------------------------------------------------------------------------


def test_force_reports_processed() -> None:
    v = summarize_result("force", {
        "processed": 2,
        "results": [
            {"key": "k1", "status": "fix_queued", "note": "已进待修"},
            {"key": "k2", "error": "PR 没有自动修复流程"},
        ],
    })
    assert "强制修复 2 条" in v["headline"]
    assert dict(v["facts"])["失败"] == "1"


def test_force_all_failed_is_bad() -> None:
    v = summarize_result("force", {
        "processed": 2,
        "results": [{"key": "k1", "error": "x"}, {"key": "k2", "error": "y"}],
    })
    assert v["tone"] == "bad"


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


def test_flush_sums_conclusions() -> None:
    v = summarize_result("flush", {
        "results": [
            {"queue": "verify", "flushed": 3, "conclusions": 3, "error": None},
            {"queue": "fix_bug", "flushed": 2, "conclusions": 2, "error": None},
        ],
    })
    facts = dict(v["facts"])
    assert facts["刷出"] == "5"
    assert facts["结论"] == "5"
    assert "刷出 5 条结论" in v["headline"]


def test_flush_skipped_queues_explain_why() -> None:
    """没到阈值不是失败，但要让用户看见「为什么没刷」。"""
    v = summarize_result("flush", {
        "results": [{"queue": "verify", "flushed": 0, "reason": "未到阈值（3/10）"}],
    })
    assert "没有刷出结论" in v["headline"]
    assert v["rows"][0][3] == "未到阈值（3/10）"
    assert v["tone"] == "warn"


def test_flush_error_is_bad() -> None:
    v = summarize_result("flush", {
        "results": [{"queue": "verify", "flushed": 0, "error": "LLM 超时"}],
    })
    assert v["tone"] == "bad"


# ---------------------------------------------------------------------------
# 兜底与边界
# ---------------------------------------------------------------------------


def test_unknown_kind_lists_scalar_fields() -> None:
    """新增作业种类时不必同时改这里才能跑——列出能看懂的部分。"""
    v = summarize_result("bench", {"frozen": 12, "note": "ok", "nested": [1, 2]})
    facts = dict(v["facts"])
    assert facts["frozen"] == "12"
    assert facts["note"] == "ok"
    assert "nested" not in facts
    assert "结构化字段" in v["headline"]


def test_none_result_is_safe() -> None:
    v = summarize_result("scan", None)
    assert v["headline"]
    assert v["facts"]


def test_empty_result_for_unknown_kind() -> None:
    v = summarize_result("whatever", {})
    assert v["headline"] == "完成（没有返回数据）"


def test_complete_job_always_has_a_headline() -> None:
    """每个种类、每种空/满输入都必须给出一句话，否则页面上是一片空白。"""
    payloads: tuple[dict[str, object] | None, ...] = (None, {}, {"results": []})
    for kind in ("scan", "evaluate", "verify", "fix", "force", "flush", "unknown"):
        for payload in payloads:
            v = summarize_result(kind, payload)
            assert v["headline"].strip(), f"{kind} / {payload} 没有结论句"
            assert v["tone"] in {"ok", "warn", "bad"}


@pytest.mark.parametrize("column_count", [3, 4, 5])
def test_rows_match_their_column_count(column_count: int) -> None:
    """行宽与列数必须一致，否则表格会错位。"""
    cases = {
        3: ("force", {"processed": 1, "results": [{"key": "k", "status": "s", "note": "n"}]}),
        4: ("verify", {"processed": 1, "results": [
            {"key": "k", "reproduced": True, "queued": "q", "error": None}]}),
        5: ("fix", {"attempted": 1, "dry_run": False, "results": [
            {"key": "k", "outcome": "success", "rounds": 1, "pr_url": "u", "error": None}]}),
    }
    kind, payload = cases[column_count]
    v = summarize_result(kind, payload)
    assert len(v["columns"]) == column_count
    for row in v["rows"]:
        assert len(row) == column_count, f"{kind} 的行宽是 {len(row)}，列数是 {column_count}"
