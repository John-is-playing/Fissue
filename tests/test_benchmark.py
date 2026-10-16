"""跑分基线测试：冻结、对比、以及「是否可比」的前提判定。

不触网、不碰 Docker：只读临时 SQLite，git 状态用打桩或真实仓库。

为什么这些用例值得写：BENCHMARK.md §5 自认「冻结版本的可复现跑分不存在」，
导致文档里的数字**无法回答「这次改动变好还是变坏」**。本模块是把那个洞补上，
所以「可比性判定」是本文件的重点——判错比不判更糟。
"""

from __future__ import annotations

import json
from pathlib import Path

import fissue.benchmark as bm
from fissue.models import (
    Category,
    DimensionScore,
    Evaluation,
    FixAttempt,
    FixOutcome,
    ItemType,
    Platform,
    Priority,
    RawItem,
    RepoRef,
    Scores,
    VerifierKind,
    VerifierOutcome,
    VerifierRun,
    VerifierSpec,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _seed(repo, number: int = 1, *, category: Category = Category.BUG,
          priority: Priority = Priority.TIER1) -> RawItem:
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="requests"))
    item = RawItem(
        platform=Platform.GITHUB, repo="psf/requests", number=number,
        item_type=ItemType.ISSUE, title=f"条目 {number}", body="正文",
        labels=["bug"], author="u1",
    )
    repo.upsert_item(rid, item)
    repo.save_evaluation(
        item.key,
        Evaluation(
            category=category, priority=priority, model="test-model", summary="s",
            scores=Scores(
                authenticity=DimensionScore(score=90),
                importance=DimensionScore(score=80),
                difficulty=DimensionScore(score=20),
            ),
        ),
    )
    return item


def _add_run(repo, item_key: str, *, stage: str, outcome: VerifierOutcome,
             verifier_id: int | None = None) -> None:
    repo.save_verifier_run(
        VerifierRun(
            verifier_id=verifier_id, item_key=item_key, stage=stage,
            outcome=outcome, exit_code=0 if outcome is VerifierOutcome.PASS else 1,
        )
    )


def _clean_git(commit: str = "a" * 40) -> dict:
    return {"commit": commit, "branch": "main", "dirty": False, "dirty_files": 0}


def _dirty_git(commit: str = "a" * 40) -> dict:
    return {"commit": commit, "branch": "main", "dirty": True, "dirty_files": 7}


# ---------------------------------------------------------------------------
# 前提：git 状态与配置指纹
# ---------------------------------------------------------------------------


def test_git_state_reads_real_repo() -> None:
    """在真实仓库里应能取到 commit；字段齐全，缺 git 也不崩。"""
    state = bm.git_state()

    assert set(state) == {"commit", "branch", "dirty", "dirty_files"}
    assert state["commit"] is None or len(state["commit"]) == 40
    assert isinstance(state["dirty"], bool)
    assert isinstance(state["dirty_files"], int)


def test_git_state_survives_missing_repo(tmp_path: Path) -> None:
    """不是 git 仓库时如实返回 None，而不是抛异常。"""
    state = bm.git_state(tmp_path)

    assert state["commit"] is None
    assert state["dirty"] is False


def test_config_fingerprint_has_decision_fields_and_no_secrets(settings) -> None:
    """指纹要含判定口径字段，且**不得**把 api_key / 完整 base_url 写进去。"""

    cfg = settings.model_copy(deep=True)
    cfg.llm.api_key = "sk-super-secret-value"
    cfg.llm.base_url = "https://internal-gateway.example.com/v1"

    fp = bm.config_fingerprint(cfg)
    blob = json.dumps(fp, ensure_ascii=False)

    # 影响判定口径的项必须在
    assert fp["verifier"]["regression_gate"]
    assert "require_f2p" in fp["verifier"]
    assert "stall_rounds" in fp["auto_fix"]
    assert fp["llm"]["model"]
    assert fp["llm"]["temperature"] == settings.llm.temperature

    # 密钥与内网路径不得泄漏
    assert "sk-super-secret-value" not in blob
    assert "internal-gateway" not in blob or "/v1" not in blob
    assert fp["llm"]["base_url_host"] == "internal-gateway.example.com"


# ---------------------------------------------------------------------------
# 结果：聚合
# ---------------------------------------------------------------------------


def test_aggregate_counts_seeded_data(db, repo) -> None:
    """聚合口径：条目分布、最新评测、各阶段 outcome、修复结果都要对上。"""
    a = _seed(repo, 1, category=Category.BUG, priority=Priority.TIER1)
    b = _seed(repo, 2, category=Category.FEATURE, priority=Priority.NONE)
    spec = VerifierSpec(kind=VerifierKind.EXECUTABLE, name="v", command="pytest -q",
                        files={"tests/t.py": "def test_x(): assert 1"})
    vid = repo.save_verifier(a.key, spec)

    _add_run(repo, a.key, stage="base", outcome=VerifierOutcome.FAIL, verifier_id=vid)
    _add_run(repo, a.key, stage="fix", outcome=VerifierOutcome.PASS, verifier_id=vid)
    # verifier_runs.verifier_id 是 NOT NULL：即便沙盒执行本身出错（error），
    # 这条 run 也仍挂在某个验证器上（真实数据里 base error 同样带 verifier_id）。
    vid_b = repo.save_verifier(b.key, spec)
    _add_run(repo, b.key, stage="base", outcome=VerifierOutcome.ERROR, verifier_id=vid_b)

    repo.save_fix_attempt(
        FixAttempt(item_key=a.key, outcome=FixOutcome.NEEDS_MANUAL, rounds=3,
                   patch_path="data/patches/x.patch")
    )

    res = bm.aggregate(db)

    assert res["items"]["total"] == 2
    assert res["items"]["by_item_type"] == {"issue": 2}
    assert res["items"]["by_category"] == {"bug": 1, "feature": 1}
    assert res["evaluation"]["evaluated_items"] == 2
    assert res["evaluation"]["avg_scores"]["importance"] == 80.0
    # 两个条目各挂一个验证器（base error 那条也必须挂在验证器上）
    assert res["verifier"]["verifiers"] == 2
    assert res["verifier"]["f2p_satisfied"] == 0
    assert res["verifier"]["runs_total"] == 3
    assert res["verifier"]["by_stage"] == {"base": 2, "fix": 1}
    assert res["verifier"]["by_stage_outcome"]["base/fail"] == 1
    assert res["verifier"]["by_stage_outcome"]["fix/pass"] == 1
    # base error 是「环境故障」的独立指标（pipekit 15 次全是它）
    assert res["verifier"]["base_error_runs"] == 1
    assert res["fix"]["attempts"] == 1
    assert res["fix"]["patches"] == 1
    assert res["fix"]["prs"] == 0


def test_aggregate_on_empty_db_is_all_zero(db) -> None:
    """空库要出全 0 基线而不是崩（--repo 写错/首跑时很常见）。"""
    res = bm.aggregate(db)

    assert res["items"]["total"] == 0
    assert res["evaluation"]["evaluated_items"] == 0
    assert res["verifier"]["runs_total"] == 0
    assert res["fix"]["attempts"] == 0
    assert res["verifier"]["by_stage_outcome"] == {}


def test_aggregate_scopes_to_repo(db, repo) -> None:
    """限定仓库时不串入别家的数据。"""
    _seed(repo, 1)
    rid = repo.ensure_repo(RepoRef(platform=Platform.GITHUB, owner="psf", name="urllib3"))
    other = RawItem(platform=Platform.GITHUB, repo="psf/urllib3", number=9,
                    item_type=ItemType.ISSUE, title="别家", body="x", labels=[], author="u")
    repo.upsert_item(rid, other)

    assert bm.aggregate(db)["items"]["total"] == 2
    assert bm.aggregate(db, repo_slug="psf/requests")["items"]["total"] == 1


# ---------------------------------------------------------------------------
# 冻结：capture
# ---------------------------------------------------------------------------


def test_capture_marks_reproducible_when_worktree_clean(settings, db, repo, monkeypatch) -> None:
    """工作区干净 → 标记可复现。这是基线的核心承诺。"""
    monkeypatch.setattr(bm, "git_state", lambda root=None: _clean_git())
    _seed(repo, 1)

    rec = bm.capture(settings, db, repo_slug="psf/requests", label="clean")

    assert rec["reproducible"] is True
    assert rec["schema_version"] == bm.SCHEMA_VERSION
    assert rec["label"] == "clean"
    assert rec["provenance"]["git"]["commit"] == "a" * 40
    assert rec["result"]["items"]["total"] == 1


def test_capture_marks_not_reproducible_when_dirty(settings, db, repo, monkeypatch) -> None:
    """有未提交改动 → 明确标记**不可复现**（否则基线会被误当回归依据）。"""
    monkeypatch.setattr(bm, "git_state", lambda root=None: _dirty_git())
    _seed(repo, 1)

    rec = bm.capture(settings, db, label="dirty")

    assert rec["reproducible"] is False
    assert rec["provenance"]["git"]["dirty_files"] == 7


# ---------------------------------------------------------------------------
# 落盘：save / load
# ---------------------------------------------------------------------------


def test_save_load_roundtrip_writes_lf_and_utf8(tmp_path: Path) -> None:
    """必须以 UTF-8 + LF 落盘：基线要能进版本库并跨平台干净 diff。"""
    rec = {"schema_version": 1, "label": "中文标签", "result": {"items": {"total": 3}}}
    path = bm.save(rec, tmp_path / "sub" / "b.json")

    assert path.exists()
    raw = path.read_bytes()
    assert b"\r\n" not in raw          # 不能出现 CRLF
    assert raw.endswith(b"\n")                 # 末尾换行（git diff 友好）
    assert json.loads(raw.decode("utf-8")) == rec
    assert bm.load(path) == rec


# ---------------------------------------------------------------------------
# 对比：diff —— 判错比不判更糟，重点测「可比性」
# ---------------------------------------------------------------------------


def _rec(label="x", *, commit="a" * 40, dirty=False, dirty_files=0,
         model="m1", total=1, base_fail=0, base_error=0, fix_pass=0,
         repos: list[str] | None = None) -> dict:
    items: dict = {"total": total}
    if repos is not None:
        # 只在显式传入时才写入 ``by_repo``——不传即模拟「旧 schema 没有这一项」。
        items["by_repo"] = {r: 1 for r in repos}
    return {
        "schema_version": bm.SCHEMA_VERSION,
        "label": label,
        "provenance": {
            "git": {"commit": commit, "branch": "main", "dirty": dirty,
                    "dirty_files": dirty_files},
            "config": {"llm": {"model": model}, "verifier": {"regression_gate": "strict"}},
        },
        "result": {
            "items": items,
            "verifier": {
                "base_error_runs": base_error,
                "by_stage_outcome": {"base/fail": base_fail, "fix/pass": fix_pass},
            },
        },
    }


def test_diff_identical_clean_baselines_is_comparable_and_empty() -> None:
    """同一 commit、干净工作区、同模型、同结果 → 可比且无变化。"""
    d = bm.diff(_rec(), _rec())

    assert d["comparable"] is True
    assert d["not_comparable_because"] == []
    assert d["changed"] == []


def test_diff_flags_dirty_worktree_as_not_comparable() -> None:
    """任一侧脏 → 不可比（这正是原文档「非冻结」局限的机械表达）。"""
    d = bm.diff(_rec(), _rec(dirty=True, dirty_files=3))

    assert d["comparable"] is False
    assert any("未提交改动" in r for r in d["not_comparable_because"])
    assert any("3 个文件" in r for r in d["not_comparable_because"])


def test_diff_flags_commit_change() -> None:
    d = bm.diff(_rec(commit="a" * 40), _rec(commit="b" * 40))

    assert d["comparable"] is False
    assert any("commit 不同" in r for r in d["not_comparable_because"])


def test_diff_flags_model_change() -> None:
    """换模型则全部数字都会变——必须显式拦住。"""
    d = bm.diff(_rec(model="m1"), _rec(model="m2"))

    assert d["comparable"] is False
    assert any("模型不同" in r for r in d["not_comparable_because"])


def test_diff_reports_result_deltas_and_summary() -> None:
    """可比时给出逐项增量，并在摘要里点名关键指标。"""
    old = _rec(total=10, base_fail=8, base_error=0, fix_pass=6)
    new = _rec(total=10, base_fail=10, base_error=2, fix_pass=6)

    d = bm.diff(old, new)

    assert d["comparable"] is True
    assert d["summary"]["base_fail"] == 2
    assert d["summary"]["base_error"] == 2
    assert d["summary"]["fix_pass"] is None          # 无变化 → 不编造 0
    keys = {c["key"] for c in d["changed"]}
    assert "result.verifier.by_stage_outcome.base/fail" in keys
    delta = next(c for c in d["changed"] if c["key"] == "result.verifier.base_error_runs")
    assert (delta["before"], delta["after"], delta["delta"]) == (0, 2, 2)


def test_diff_flags_different_repo_sets() -> None:
    """覆盖的仓库不同 → 不可比。

    这是最容易被忽略的一条：范围一改，增量里混的是**范围变化**而非能力变化。实测
    踩过——旧基线含 textkit/ratekit/pipekit，新基线只剩 envkit/lockkit，diff 的增量
    几乎全是范围差异，而当时只 flag 了 commit 与模型。
    """
    old = _rec(repos=["psf/requests", "o/pipekit"])
    new = _rec(repos=["psf/requests", "o/lockkit"])

    d = bm.diff(old, new)

    assert d["comparable"] is False
    reasons = " ".join(d["not_comparable_because"])
    assert "仓库集不同" in reasons
    assert "o/pipekit" in reasons and "o/lockkit" in reasons     # 说清哪边多出了谁


def test_diff_same_repo_set_is_comparable_even_when_counts_grow() -> None:
    """仓库集一致时不该因「条目变多」判不可比——语料增长是正常的。"""
    d = bm.diff(
        _rec(repos=["psf/requests"], total=10),
        _rec(repos=["psf/requests"], total=25),
    )

    assert d["comparable"] is True, d["not_comparable_because"]


def test_diff_flags_unverifiable_scope_when_one_side_lacks_repo_set() -> None:
    """只有一侧记录仓库集 → 无法确认范围一致，按不可比处理（宁可保守）。

    「没记录」不等于「没覆盖」：旧 schema 的基线没有 ``by_repo``，此时不能默认可比，
    否则范围差异会悄悄混进回归结论里。
    """
    d = bm.diff(_rec(), _rec(repos=["psf/requests"]))

    assert d["comparable"] is False
    assert any("未记录仓库集" in r for r in d["not_comparable_because"])


def test_diff_detects_config_change_beyond_model() -> None:
    """回归门档位从 warn 改 strict 会直接改变结论，必须能被看出来。"""
    a = _rec()
    b = _rec()
    b["provenance"]["config"]["verifier"]["regression_gate"] = "warn"

    d = bm.diff(a, b)

    assert any("regression_gate" in c for c in d["config_deltas"])


def test_diff_rejects_schema_mismatch() -> None:
    old = _rec()
    new = _rec()
    new["schema_version"] = bm.SCHEMA_VERSION + 1

    d = bm.diff(old, new)

    assert d["comparable"] is False
    assert any("schema" in r for r in d["not_comparable_because"])


def test_render_diff_states_incomparability_loudly() -> None:
    """不可比必须在输出最显眼处说清，不能只给一堆数字。"""
    text = bm.render_diff(bm.diff(_rec(), _rec(dirty=True, dirty_files=2)))

    assert "不可比" in text
    assert "未提交改动" in text


def test_render_diff_shows_arrows_for_deltas() -> None:
    text = bm.render_diff(bm.diff(_rec(base_fail=1), _rec(base_fail=4)))

    assert "base 失败" in text
    assert "↑" in text


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------


def test_bench_commands_are_registered() -> None:
    """三个子命令都要挂在 `fissue bench` 下（防止实现写了没接线）。"""
    from typer.testing import CliRunner

    from fissue.cli.main import app

    result = CliRunner().invoke(app, ["bench", "--help"])

    assert result.exit_code == 0, result.output
    for cmd in ("capture", "diff", "list"):
        assert cmd in result.output


def test_bench_list_on_missing_dir_is_friendly(tmp_path: Path) -> None:
    """还没冻结过任何基线时，list 要给提示而不是报错。"""
    import yaml
    from typer.testing import CliRunner

    from fissue.cli.main import app

    # 自带一份隔离配置（``cli_env`` 定义在 test_cli.py 里，本模块用不到）
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "database": {"url": f"sqlite:///{(tmp_path / 'b.db').as_posix()}"},
                "app": {"data_dir": str(tmp_path / "data"), "log_level": "warning"},
                "llm": {"api_key": "k", "model": "m", "fallback_model": None},
            }
        ),
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text("FISSUE_LLM_API_KEY=k\n", encoding="utf-8")

    result = CliRunner().invoke(
        app, ["bench", "list", "--config", str(cfg), "--env", str(env)]
    )

    assert result.exit_code == 0, result.output
    assert "bench capture" in result.output
