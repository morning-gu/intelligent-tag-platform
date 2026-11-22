"""open_report 单测：报告组装/渲染/落盘 + 历史摘要取格规则（Task 5）。

夹具手造 `OpenMovieResult`（评审失败片 / 交集外命中 / 漏判条目 / produce
失败片 / 零产出片五类齐备），report.json 各键精确值**手算**钉死；audit
夹具镜像 mtg 管线 `_write_report` 的键形（merged / accepted / verdicts /
dropped / library.align）。服务侧接线（_write_artifacts 落三件套、
history 摘要接线）的回归在 test_open_eval.py。

手算基准（thetas=(0.4, 0.8)；gt 三片；库 = {g1..g5, g7}，g6/g8/g9 越库）：

- ttA ok：命中 g1(0.9)/g9(交集外)/g3(0.1,漏判)/g4(0.8,不通过) + 新词
  甲(True)/乙(False) → hits_in 3、hits_out 1、no_gt 1、缺评审 1 条；
- ttB ok 评审失败：命中 g1(0.5)/g5(0.7) + 新词丙 → hit/new_pass 整体
  None，缺评审 +2（新词数整体不可知的结构性局限）；
- ttC ok：命中 g8(无 gt)/g6(gt 0.2 但不在库 → 交集外) + 新词丁(True)
  → 交集内命中为空（invalid），命中通过率仍计 1/2；
- ttD produce 失败：不进样本（n_failed 组装层自算），进 failed_movies；
- ttE ok 零产出：hit_pass=()（评审成功）→ 不是 produce 失败形，计
  invalid（θ 族不可评）。

先验序（gt 全影片均值，并列 tag_id 字典序）：g4(0.8) > g1(0.7) = g5(0.7)
> g2(0.65) > g6(0.2) > g3(0.1)，0.7 并列取 g1。

θ 网格手算（有效片 = ttA/ttB，ttB 评审失败不进标定）：

- θ=0.4：准确率 (2/3 + 1)/2 = 5/6；随机 (3/4 + 2/2)/2 = 0.875；先验
  (3/3 + 2/2)/2 = 1.0；覆盖率 (2/3 + 2/2)/2 = 5/6；标定 1 片（ttA：
  tp1/fp0/fn1/tn0）→ 一致率 0.5、FP 0.0、FN 0.5（单值 CI = [0.5, 0.5]）。
- θ=0.8：准确率 (2/3 + 0)/2 = 1/3；随机 (2/4 + 0)/2 = 0.25；先验
  (2/3 + 0)/2 = 1/3；覆盖率 ttB 相关集空不进格 → 1.0；标定同上 0.5。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tagplatform.services.eval.open_report import (
    _read_open_summary,
    assemble_open_report,
    render_open_report_md,
    write_open_report,
)
from tagplatform.services.eval.open_ranking import DEFAULT_THETA_HEADLINE
from tagplatform.services.eval.open_service import OpenMovieResult
from tagplatform.services.eval.report import BANNER_PARTIAL, NOTE_SILVER

# ---------------------------------------------------------------- 夹具

GT = {
    "ttA": {"g1": 0.9, "g2": 0.4, "g3": 0.1, "g4": 0.8},
    "ttB": {"g1": 0.5, "g5": 0.7},
    "ttC": {"g2": 0.9, "g6": 0.2},
}
LIBRARY = ("g1", "g2", "g3", "g4", "g5", "g7")
THETAS = (0.4, 0.8)


def _verdict(passed):
    """行级 review dict；passed=None = 模型漏判（pass=None 占位，非评审失败）。"""
    return {"pass": passed, "reason": "评审理由", "quote": "两名人物发生争执"}


def _hit(tag_id, gt, review):
    return {"tag_id": tag_id, "tag_name": f"名-{tag_id}", "gt": gt,
            "review": review}


def _new(name, passed):
    return {"name": name, "desc": "通用释义", "level": "film",
            "review": _verdict(passed)}


def _result(tt, title, *, ok=True, error=None, hits=(), new_words=(),
            review_error=None, elapsed_ms=100):
    return OpenMovieResult(tt=tt, title=title, ok=ok, error=error,
                           hits=list(hits), new_words=list(new_words),
                           review_error=review_error, elapsed_ms=elapsed_ms)


def _main_results():
    """五类片齐备的主夹具（手算基准见模块 docstring）。"""
    return [
        _result("ttA", "A Movie", hits=[
            _hit("g1", 0.9, _verdict(True)),
            _hit("g9", None, _verdict(True)),   # 交集外（不在 gt 也不在库）
            _hit("g3", 0.1, _verdict(None)),    # 漏判条目
            _hit("g4", 0.8, _verdict(False)),   # 评审不通过
        ], new_words=[_new("新词甲", True), _new("新词乙", False)]),
        _result("ttB", "B Movie", hits=[
            _hit("g1", 0.5, None), _hit("g5", 0.7, None)],
            new_words=[_new("新词丙", None)],
            review_error="ReviewRequestError: 宕机"),
        _result("ttC", "C Movie", hits=[
            _hit("g8", None, _verdict(True)),   # 完全不在 gt
            _hit("g6", 0.2, _verdict(False)),   # 在 gt 但不在库 → 交集外
        ], new_words=[_new("新词丁", True)]),
        _result("ttD", "D Movie", ok=False, error="RuntimeError: 注入失败"),
        _result("ttE", "E Movie"),              # ok 零产出（评审成功无需评审）
    ]


def _meta(*, status="succeeded", done=5, total=5, failed=1, same_model=True):
    return {
        "run_id": "run-test", "status": status, "done": done,
        "total": total, "failed": failed,
        "llm_model": "qwen-x", "embedding_model": "bge-m3",
        "prompt": {"mode": "open", "version": "mtg-v9"},
        "review": {"model": "review-x", "same_model": same_model,
                   "prompt_version": "rp1"},
        "library": {"version": 12, "n_dirs": 3, "n_tags": 6},
        "dataset": {"id": "emotion", "gt_path": "/x/gt", "gt_sha8": "abcd1234",
                    "n_rows": 10},
        "modalities": ["script", "dialogue"],
        "thetas": list(THETAS),
    }


@pytest.fixture
def audit_dir(tmp_path):
    """评审官审计夹具（镜像 pipeline._write_report 键形）：ttC 故意缺失。"""
    d = tmp_path / "audit"
    d.mkdir()
    _dump = lambda name, payload: (d / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _dump("ttA.json", {
        "merged": [{"tag": "m1"}, {"tag": "m2"}, {"tag": "m3"}, {"tag": "m4"}],
        "accepted": [{"tag": "a1"}, {"tag": "a2"}, {"tag": "a3"}],
        "verdicts": [{"tag": "m1", "verdict": "supported"},
                     {"tag": "m2", "verdict": "supported"},
                     {"tag": "m3", "verdict": "insufficient"},
                     {"tag": "m4", "verdict": "contradicted"}],
        "dropped": [{"tag": "x", "why": "judge=insufficient"},
                    {"tag": "y", "why": "无有效场景证据"}],
        "library": {"align": [{"tag": "m1", "band": "duplicate"},
                              {"tag": "m2", "band": "new"},
                              {"tag": "m3", "band": "suspect"},
                              {"tag": "m4", "band": "new"}]},
    })
    _dump("ttB.json", {"merged": [{"tag": "m5"}], "accepted": [],
                       "verdicts": [], "dropped": [],
                       "library": {"align": []}})
    _dump("ttE.json", {"merged": [], "accepted": [], "verdicts": [],
                       "dropped": [], "library": {"align": []}})
    return d


def _assemble(results=None, meta=None, audit_dir=None, thetas=THETAS):
    """组装入口：audit_dir=None = 不提供审计目录（judge 空段 + 警示）。"""
    kwargs = {"audit_dir": audit_dir} if audit_dir is not None else {}
    return assemble_open_report(
        _main_results() if results is None else results, GT, LIBRARY,
        thetas, _meta() if meta is None else meta, **kwargs)


# ---------------------------------------------------------------- report.json 全键

def test_assemble_report_full_contract(audit_dir):
    """report.json 键契约（前端 T7）+ θ 网格/通过率/核算/产出统计精确值。"""
    report = _assemble(audit_dir=audit_dir)
    assert set(report) == {
        "banner", "sample_accounting", "no_gt", "theta_rows", "pass_rates",
        "ndcg", "new_ratio", "produced_stats", "judge", "calibre", "movies",
        "failed_movies", "review_failed_movies", "warnings", "note"}
    # 样本核算三类分开：total/ok/failed 组装层自算，invalid 来自 evaluate_open
    assert report["sample_accounting"] == {
        "total": 5, "ok": 4, "failed": 1, "review_failed": 1, "invalid": 2}
    assert report["no_gt"] == 3  # ttA g9 一条 + ttC g8/g6 两条
    assert report["note"] == NOTE_SILVER

    # θ 行：键契约 + 手算格值
    rows = report["theta_rows"]
    assert [r["theta"] for r in rows] == [0.4, 0.8]
    for row in rows:
        assert set(row) == {
            "theta", "n_valid", "accuracy", "accuracy_ci", "accuracy_random",
            "accuracy_prior", "coverage", "n_calibrated", "agreement",
            "agreement_ci", "fp_rate", "fn_rate"}
    r04, r08 = rows
    assert r04["n_valid"] == 2 and r08["n_valid"] == 2  # 有效片 ttA/ttB
    assert r04["accuracy"] == pytest.approx(5 / 6)
    assert r04["accuracy_random"] == pytest.approx(0.875)
    assert r04["accuracy_prior"] == pytest.approx(1.0)
    assert r04["coverage"] == pytest.approx(5 / 6)
    assert r04["accuracy_ci"][0] <= r04["accuracy"] <= r04["accuracy_ci"][1]
    assert r08["accuracy"] == pytest.approx(1 / 3)
    assert r08["accuracy_random"] == pytest.approx(0.25)
    assert r08["accuracy_prior"] == pytest.approx(1 / 3)
    assert r08["coverage"] == pytest.approx(1.0)  # ttB 相关集空 → 不进该格
    # 标定：仅 ttA（评审成功且有可标定条目），漏判条目 g3 已排除
    assert r04["n_calibrated"] == 1 and r08["n_calibrated"] == 1
    assert r04["agreement"] == pytest.approx(0.5)
    assert r04["agreement_ci"] == [0.5, 0.5]  # 单值片 → 自举区间退化为点
    assert r04["fp_rate"] == pytest.approx(0.0)
    assert r04["fn_rate"] == pytest.approx(0.5)
    assert r08["agreement"] == pytest.approx(0.5)
    assert r08["fp_rate"] == pytest.approx(0.0)
    assert r08["fn_rate"] == pytest.approx(0.5)

    # 通过率：漏判条目排除出分子分母；评审失败片整体排除
    pr = report["pass_rates"]
    assert pr["new_macro"] == pytest.approx(0.75)   # ttA 1/2、ttC 1/1
    assert pr["new_micro"] == pytest.approx(2 / 3)  # 2 True / 3 判定
    assert pr["new_n"] == 2
    assert pr["hit_macro"] == pytest.approx(7 / 12)  # ttA 2/3、ttC 1/2
    assert pr["hit_micro"] == pytest.approx(0.6)     # 3 True / 5 判定
    assert pr["hit_n"] == 2
    assert pr["review_missing"] == 3  # ttA 漏判 1 + ttB 整体缺位 2

    # nDCG（产出序、连续增益）与新词占比
    assert report["ndcg"]["macro"] == pytest.approx(0.9322, abs=1e-3)
    assert report["ndcg"]["ci"][0] <= report["ndcg"]["macro"] \
        <= report["ndcg"]["ci"][1]
    assert report["new_ratio"]["macro"] == pytest.approx(1 / 3)

    # 产出统计（逐成功片）：ttE 零产出也是合法观测（total 0）
    assert report["produced_stats"]["counts"] == [
        {"tt": "ttA", "hits_in": 3, "hits_out": 1, "new_words": 2},
        {"tt": "ttB", "hits_in": 2, "hits_out": 0, "new_words": 1},
        {"tt": "ttC", "hits_in": 0, "hits_out": 2, "new_words": 1},
        {"tt": "ttE", "hits_in": 0, "hits_out": 0, "new_words": 0},
    ]
    assert report["produced_stats"]["min"] == 0
    assert report["produced_stats"]["median"] == pytest.approx(3.0)
    assert report["produced_stats"]["max"] == 6

    # judge 段：跨片聚合（ttC 审计缺失跳过；ttB/ttE 空段贡献 0）
    assert report["judge"]["self_filter"] == pytest.approx(3 / 5)
    assert report["judge"]["verdicts"] == {
        "supported": 2, "insufficient": 1, "contradicted": 1}
    assert report["judge"]["dropped"] == {
        "judge=insufficient": 1, "无有效场景证据": 1}
    assert report["judge"]["bands"] == {
        "duplicate": 1, "suspect": 1, "new": 2}

    # 口径三件套 + 模态
    assert report["calibre"] == {
        "llm_model": "qwen-x", "embedding_model": "bge-m3",
        "prompt_version": "mtg-v9", "review_model": "review-x",
        "review_same_model": True, "review_prompt_version": "rp1",
        "library_version": 12, "dataset_id": "emotion",
        "gt_sha8": "abcd1234", "modalities": ["script", "dialogue"]}

    # 警示：evaluate_open 口径警示原样透传（audit 齐 → 无 judge 缺失警示）
    assert len(report["warnings"]) == 2
    assert "评审失败" in report["warnings"][0] and "ttB" in report["warnings"][0]
    assert "交集内命中为空" in report["warnings"][1]


def test_produce_failed_films_never_in_samples():
    """produce 失败片不进样本（n_failed 组装层自算）——n_valid 只含 ok 片。"""
    report = _assemble(results=[_result("ttX", "X", ok=False, error="boom")],
                       meta=_meta(status="aborted", done=1, total=1,
                                  failed=1, same_model=False))
    assert report["sample_accounting"] == {
        "total": 1, "ok": 0, "failed": 1, "review_failed": 0, "invalid": 0}
    assert report["theta_rows"][0]["n_valid"] == 0
    assert report["theta_rows"][0]["accuracy"] is None  # None≠0
    assert report["produced_stats"]["min"] is None      # 无成功片 → None
    assert report["failed_movies"] == [{"tt": "ttX", "error": "boom"}]
    # 全失败 run：judge 段空但不警示（无可审计的片），banner 齐两档
    assert report["judge"]["self_filter"] is None
    assert not any("审计" in w for w in report["warnings"])
    assert report["banner"][0] == BANNER_PARTIAL
    assert "produce 失败 1/1" in report["banner"][1]


# ---------------------------------------------------------------- movies 明细

CONTRACT_MOVIE_KEYS = {"tt", "title", "ok", "review", "hits_n", "new_n",
                       "accuracy", "new_pass", "hit_pass", "error",
                       "review_error", "elapsed_ms"}


def test_movies_rows_contract():
    """movies 折叠明细：评审三态 / 片级通过率 / 缺省 θ=0.4 的片级准确率。"""
    report = _assemble()
    assert len(report["movies"]) == 5  # 全部影片（含失败片），ok 标记区分
    by_tt = {m["tt"]: m for m in report["movies"]}
    for row in by_tt.values():
        assert set(row) == CONTRACT_MOVIE_KEYS
    a = by_tt["ttA"]
    assert a["review"] == "ok" and a["ok"] is True
    assert a["hits_n"] == 4 and a["new_n"] == 2
    assert a["accuracy"] == pytest.approx(2 / 3)  # 2/3 条交集内命中 gt≥0.4
    assert a["hit_pass"] == pytest.approx(2 / 3)  # 漏判条目排除出分母
    assert a["new_pass"] == pytest.approx(0.5)
    b = by_tt["ttB"]
    assert b["review"] == "failed"  # 评审失败片：ok 但 review=failed
    assert b["accuracy"] == pytest.approx(1.0)  # 准确率照常可算（产出不作废）
    assert b["hit_pass"] is None and b["new_pass"] is None  # 整体 None
    c = by_tt["ttC"]
    assert c["review"] == "ok" and c["accuracy"] is None  # 交集内命中为空
    assert c["hit_pass"] == pytest.approx(0.5) and c["new_pass"] == 1.0
    d = by_tt["ttD"]
    assert d["review"] == "skipped" and d["ok"] is False
    assert d["hits_n"] == 0 and d["accuracy"] is None
    assert d["error"] == "RuntimeError: 注入失败"
    e = by_tt["ttE"]
    assert e["review"] == "ok" and e["hit_pass"] is None  # 无条目 → None≠0
    assert report["failed_movies"] == [{"tt": "ttD", "error": "RuntimeError: 注入失败"}]
    assert report["review_failed_movies"] == [
        {"tt": "ttB", "review_error": "ReviewRequestError: 宕机"}]


# ---------------------------------------------------------------- banner 规则

def _clean_result(tt):
    return _result(tt, tt, hits=[_hit("g1", 0.9, _verdict(True))],
                   new_words=[_new("新词", True)])


def _gt_for(tts):
    """banner 用例的 gt：各片 {g1: 0.9, g2: 0.4}（与 _clean_result 对齐——
    行 gt 与 gt 表必须同源，否则交集口径失真）。"""
    return {tt: {"g1": 0.9, "g2": 0.4} for tt in tts}


def test_banner_clean_run_empty():
    """全量成功 + 异模评审 + 无失败核算超 10% → banner 空表。"""
    tts = [f"m{i}" for i in range(10)]
    report = assemble_open_report([_clean_result(tt) for tt in tts],
                                  _gt_for(tts), LIBRARY, (0.4,),
                                  _meta(done=10, total=10, failed=0,
                                        same_model=False))
    assert report["banner"] == []


def test_banner_partial_run():
    """cancelled → 部分运行警示；succeeded 但 done<total 同样不可横比。"""
    cancelled = assemble_open_report([_clean_result("m1")], _gt_for(["m1"]),
                                     LIBRARY, (0.4,),
                                     _meta(status="cancelled", done=1,
                                           total=2, failed=0,
                                           same_model=False))
    assert cancelled["banner"][0] == BANNER_PARTIAL
    shrank = assemble_open_report([_clean_result("m1")], _gt_for(["m1"]),
                                  LIBRARY, (0.4,),
                                  _meta(done=1, total=2, failed=0,
                                        same_model=False))
    assert shrank["banner"][0] == BANNER_PARTIAL


def test_banner_same_model():
    """review_same_model → 同模评审警示（spec §6 文案原样）。"""
    report = assemble_open_report([_clean_result("m1")], _gt_for(["m1"]),
                                  LIBRARY, (0.4,),
                                  _meta(done=1, total=1, failed=0,
                                        same_model=True))
    assert report["banner"] == [
        "评审与生成同模型，通过率绝对值系统性偏高，趋势对比可用、"
        "绝对值需打折解读"]


def test_banner_small_sample_three_categories():
    """失败核算三类（failed/review_failed/invalid）任一占比 >10% → 小样本警示。"""
    tts = [f"m{i}" for i in range(8)]
    oks = [_clean_result(tt) for tt in tts]
    # produce 失败 2/10
    failed = oks + [_result("f1", "F1", ok=False, error="boom"),
                    _result("f2", "F2", ok=False, error="boom")]
    report = assemble_open_report(failed, _gt_for(tts + ["f1", "f2"]),
                                  LIBRARY, (0.4,),
                                  _meta(done=10, total=10, failed=2,
                                        same_model=False))
    assert len(report["banner"]) == 1
    assert "produce 失败 2/10" in report["banner"][0]
    # 评审失败 2/10（评审失败片带产出，仍是 ok，且命中在交集内）
    review_failed = oks + [_result("r1", "R1", hits=[_hit("g1", 0.9, None)],
                                   review_error="宕机"),
                           _result("r2", "R2", hits=[_hit("g1", 0.9, None)],
                                   review_error="宕机")]
    report = assemble_open_report(review_failed, _gt_for(tts + ["r1", "r2"]),
                                  LIBRARY, (0.4,),
                                  _meta(done=10, total=10, failed=0,
                                        same_model=False))
    assert "评审失败 2/10" in report["banner"][0]
    # 交集内命中为空 2/10（全部命中落在交集外）
    invalid = oks + [_result("i1", "I1", hits=[_hit("gX", None,
                                                _verdict(True))]),
                     _result("i2", "I2", hits=[_hit("gX", None,
                                                _verdict(True))])]
    report = assemble_open_report(invalid, _gt_for(tts + ["i1", "i2"]),
                                  LIBRARY, (0.4,),
                                  _meta(done=10, total=10, failed=0,
                                        same_model=False))
    assert "交集内命中为空 2/10" in report["banner"][0]


# ---------------------------------------------------------------- judge 段容错

def test_judge_section_absent_audit_empty_with_warning():
    """audit 目录整体缺失 → judge 空段 + 警示，绝不抛错。"""
    report = _assemble(audit_dir=None)
    assert report["judge"] == {
        "self_filter": None, "verdicts": {"supported": 0, "insufficient": 0,
                                          "contradicted": 0},
        "dropped": {}, "bands": {"duplicate": 0, "suspect": 0, "new": 0}}
    assert any("审计" in w for w in report["warnings"])


def test_judge_section_tolerates_broken_audit_files(tmp_path):
    """audit 文件损坏/非 dict → 跳过该片；好文件照常聚合（无警示）。"""
    d = tmp_path / "audit"
    d.mkdir()
    (d / "ttA.json").write_text("not-json{", encoding="utf-8")
    (d / "ttB.json").write_text(json.dumps(["non-dict"]), encoding="utf-8")
    (d / "ttE.json").write_text(json.dumps({
        "merged": [{"tag": "m"}], "accepted": [{"tag": "a"}],
        "verdicts": [{"tag": "m", "verdict": "supported"}],
        "dropped": [{"tag": "x", "why": "策略禁止产出新词"}],
        "library": {"align": [{"tag": "m", "band": "new"}]}}),
        encoding="utf-8")
    report = _assemble(audit_dir=d)
    assert report["judge"] == {
        "self_filter": 1.0,
        "verdicts": {"supported": 1, "insufficient": 0, "contradicted": 0},
        "dropped": {"策略禁止产出新词": 1},
        "bands": {"duplicate": 0, "suspect": 0, "new": 1}}
    assert not any("审计" in w for w in report["warnings"])


# ---------------------------------------------------------------- 落盘三件套

def test_write_open_report_artifacts(tmp_path, audit_dir):
    """report.json / report.md / metrics.csv 三件套落盘；md 含 note、警示
    与「与闭环评测的口径关系」收尾节。"""
    report = _assemble(audit_dir=audit_dir)
    write_open_report(tmp_path, report)
    written = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert written == report  # None 安全序列化（None 原样保留）
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "银标准警示" in md and "相关性模型输出" in md  # 银标准警示原文
    assert "评审与生成同模型" in md and "小样本警示" in md  # banner 渲染
    assert "缺评审条目" in md  # review_missing 的展示标签（勿用漏判字样）
    assert "## θ 网格" in md and "## 评审通过率" in md and "## 评审官审计" in md
    # note 在前、与闭环的口径关系收尾在最后（末行是收尾节的收束句）
    assert md.index("银标准警示") < md.index("与闭环评测的口径关系")
    tail = md.rstrip().splitlines()[-1]
    assert "错误证据" in tail


def test_metrics_csv_long_table(tmp_path, audit_dir):
    """metrics.csv 长表：表头精确 + 行数 = 5×θ数 + 6 条 θ 无关行。"""
    report = _assemble(audit_dir=audit_dir)
    write_open_report(tmp_path, report)
    with open(tmp_path / "metrics.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["metric", "theta", "macro", "ci_low", "ci_high",
                       "random_baseline", "prior_baseline", "n_valid"]
    assert len(rows) == 1 + 5 * 2 + 6  # θ 行 5 指标 × 2 档 + 6 条 θ 无关
    by_name = {(r[0], r[1]): r for r in rows[1:]}
    acc = by_name[("accuracy", "0.4")]
    assert float(acc[2]) == pytest.approx(5 / 6)
    assert float(acc[5]) == pytest.approx(0.875)     # random_baseline
    assert float(acc[6]) == pytest.approx(1.0)       # prior_baseline
    assert acc[7] == "2"                             # n_valid（片数口径）
    cov = by_name[("coverage", "0.4")]
    assert float(cov[2]) == pytest.approx(5 / 6)
    assert cov[5] == "" and cov[6] == ""             # 覆盖率无基线列值
    agr = by_name[("agreement", "0.4")]
    assert float(agr[2]) == pytest.approx(0.5)
    assert agr[7] == "1"                             # n_calibrated（片数口径）
    assert by_name[("fp_rate", "0.4")][7] == "1"
    assert by_name[("fn_rate", "0.4")][7] == "1"
    ndcg = by_name[("ndcg", "")]
    assert ndcg[1] == ""                              # θ 无关行 theta 空
    assert float(ndcg[2]) == pytest.approx(0.9322, abs=1e-3)
    assert float(by_name[("new_pass", "")][2]) == pytest.approx(0.75)
    assert by_name[("new_pass", "")][7] == "2"        # new_n（片数口径）
    assert float(by_name[("new_pass_micro", "")][2]) == pytest.approx(2 / 3)
    assert float(by_name[("hit_pass", "")][2]) == pytest.approx(7 / 12)
    assert by_name[("hit_pass", "")][7] == "2"
    assert float(by_name[("new_ratio", "")][2]) == pytest.approx(1 / 3)


def test_render_md_none_cells_say_na(tmp_path):
    """None 值格渲染「暂无」（None≠0 的报表侧）：全失败 run 的空报告。"""
    report = assemble_open_report(
        [_result("ttX", "X", ok=False, error="boom")], GT, LIBRARY, (0.4,),
        _meta(done=1, total=1, failed=1, same_model=False))
    md = render_open_report_md(report)
    assert "暂无" in md  # 准确率/通过率/自滤率等格


# ---------------------------------------------------------------- 历史摘要取格

def _write_report_json(run_dir: Path, payload) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


FIVE = ("accuracy40", "new_pass", "hit_pass", "agreement40", "new_ratio")


def test_read_open_summary_nearest_valued_cell(tmp_path):
    """取格 = 最接近 0.4 的**有值** θ 行、并列取小、无有值格 → None。"""
    _write_report_json(tmp_path, {
        "theta_rows": [
            {"theta": 0.4, "accuracy": None, "agreement": None},
            {"theta": 0.2, "accuracy": 0.7, "agreement": 0.6},
            {"theta": 0.6, "accuracy": 0.9, "agreement": 0.8},
        ],
        "pass_rates": {"new_macro": 0.42, "hit_macro": 0.51},
        "new_ratio": {"macro": 0.3},
    })
    summary = _read_open_summary(tmp_path)
    # 0.2 与 0.6 距 0.4 并列 → 取小 θ（0.2 行）；0.4 行无值不算
    assert summary["accuracy40"] == pytest.approx(0.7)
    assert summary["agreement40"] == pytest.approx(0.6)
    assert summary["new_pass"] == pytest.approx(0.42)
    assert summary["hit_pass"] == pytest.approx(0.51)
    assert summary["new_ratio"] == pytest.approx(0.3)


def test_read_open_summary_round9_tie_break(tmp_path):
    """round 9 防浮点噪声：0.2+1e-12 与 0.6 距离并列 → 取小 θ。"""
    _write_report_json(tmp_path, {"theta_rows": [
        {"theta": 0.2 + 1e-12, "accuracy": 0.7, "agreement": 0.6},
        {"theta": 0.6, "accuracy": 0.9, "agreement": 0.8},
    ]})
    summary = _read_open_summary(tmp_path)
    assert summary["accuracy40"] == pytest.approx(0.7)
    assert summary["agreement40"] == pytest.approx(0.6)


def test_read_open_summary_exact_headline_wins(tmp_path):
    """恰有 0.4 有值行 → 直接取它（距离 0 最小）。"""
    _write_report_json(tmp_path, {"theta_rows": [
        {"theta": 0.2, "accuracy": 0.99, "agreement": 0.99},
        {"theta": 0.4, "accuracy": 0.55, "agreement": 0.66},
    ]})
    summary = _read_open_summary(tmp_path)
    assert summary["accuracy40"] == pytest.approx(0.55)
    assert summary["agreement40"] == pytest.approx(0.66)


def test_read_open_summary_no_valued_row(tmp_path):
    """全部无值 → None（宁缺勿假）；headline 缺档回退最近有值档。"""
    _write_report_json(tmp_path, {"theta_rows": [
        {"theta": 0.4, "accuracy": None, "agreement": None},
        {"theta": 0.6, "accuracy": None, "agreement": None},
    ]})
    summary = _read_open_summary(tmp_path)
    assert summary["accuracy40"] is None
    assert summary["agreement40"] is None
    # headline 档缺 accuracy、但有更远的有值档 → 回退最近有值档
    _write_report_json(tmp_path / "r2", {"theta_rows": [
        {"theta": 0.2, "accuracy": 0.3, "agreement": None},
        {"theta": 0.4, "accuracy": None, "agreement": None},
        {"theta": 0.8, "accuracy": 0.9, "agreement": None},
    ]})
    summary = _read_open_summary(tmp_path / "r2")
    assert summary["accuracy40"] == pytest.approx(0.3)  # 0.2 比 0.8 近
    assert summary["agreement40"] is None               # 全程无值 → None


def test_read_open_summary_missing_or_broken_report(tmp_path):
    """report.json 缺失/损坏/缺段 → 五键宁缺勿假（不因缺报告报错）。"""
    summary = _read_open_summary(tmp_path / "missing")  # 目录不存在
    assert all(summary[k] is None for k in FIVE)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "report.json").write_text("{corrupt", encoding="utf-8")
    summary = _read_open_summary(broken)
    assert all(summary[k] is None for k in FIVE)
    # 缺 theta_rows 段：accuracy/agreement None，pass 键照读
    _write_report_json(tmp_path / "r3", {
        "pass_rates": {"new_macro": 0.1, "hit_macro": 0.2},
        "new_ratio": {"macro": 0.4}})
    summary = _read_open_summary(tmp_path / "r3")
    assert summary["accuracy40"] is None and summary["agreement40"] is None
    assert summary["new_pass"] == pytest.approx(0.1)
    assert summary["hit_pass"] == pytest.approx(0.2)
    assert summary["new_ratio"] == pytest.approx(0.4)


def test_headline_theta_constant():
    """取格目标常量契约：0.4（与 open_ranking 的三处展示位共用）。"""
    assert DEFAULT_THETA_HEADLINE == 0.4
