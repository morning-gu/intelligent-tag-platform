"""eval/open_report 的单测：三维口径报告组装 / 渲染 / 落盘 / 历史摘要。

夹具镜像 mtg 管线 `_write_report` 的键形（merged / accepted / verdicts
（三维分）/ dropped / library.align）与 OpenMovieResult 的行契约
（hits/new_words 带 review = 三维分 + 推导 pass + quote，judge_reference
可选）。钉死：

1. 汇总微口径：通过率分母只计有判定行（漏判 pass=None 排除），评审
   失败片整体排除（None≠0，产出不作废仍计 total_tags/new_ratio）；
2. 三维指标复用 quality_metrics（与生产批次汇总同机制）；
3. judge 审计聚合（self_filter / 三维分聚合 / dropped / bands）与缺失
   警示；banner 三档（部分运行 / 同模 / 小样本）；
4. report.json / report.md / metrics.csv 三件套与历史摘要五键容错。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tagplatform.services.eval.open_report import (
    _read_open_summary,
    assemble_open_report,
    render_open_report_md,
    write_open_report,
)
from tagplatform.services.eval.open_service import OpenMovieResult


def _review(passed=True, r=4, s=3, v=3, quote="原文摘录"):
    return {"pass": passed,
            "relevance": {"score": r, "reason": "相关理由"},
            "semantics": {"score": s, "reason": "语义理由"},
            "value": {"score": v, "reason": "价值理由"},
            "reason": "相关理由；语义理由；价值理由", "quote": quote}


def _missed():
    return {"pass": None,
            "relevance": {"score": None, "reason": "评审未返回该条判定"},
            "semantics": {"score": None, "reason": "评审未返回该条判定"},
            "value": {"score": None, "reason": "评审未返回该条判定"},
            "reason": "评审未返回该条判定", "quote": None}


_DEFAULT = object()  # 区分"缺省给过线评审"与"显式 None（评审失败占位）"


def _hit(tag_id, name="命中标签", review=_DEFAULT, reference=None):
    return {"tag_id": tag_id, "tag_name": name, "level": "film",
            "judge_reference": reference,
            "review": _review() if review is _DEFAULT else review}


def _new(name="新词", review=_DEFAULT, reference=None):
    return {"name": name, "desc": "通用释义", "level": "film",
            "judge_reference": reference,
            "review": _review() if review is _DEFAULT else review}


def _result(tt, hits=(), new_words=(), ok=True, error=None,
            review_error=None):
    return OpenMovieResult(tt=tt, title=f"影片{tt}", ok=ok, error=error,
                           hits=list(hits), new_words=list(new_words),
                           review_error=review_error, elapsed_ms=100)


def _meta(**over):
    meta = {"status": "succeeded", "done": 2, "total": 2,
            "llm_model": "gen-model", "embedding_model": "emb",
            "prompt": {"version": "mtg-v6-graded-judge"},
            "review": {"model": "rev-model", "same_model": False,
                       "prompt_version": "open-review-v2"},
            "library": {"version": "L1"}, "dataset": {"id": "emotion"},
            "modalities": ["script", "dialogue"]}
    meta.update(over)
    return meta


def _dump(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---- 汇总与三维指标 ----


def test_assemble_summary_micro_rates_and_metrics():
    m1 = _result("ttA",
                 hits=[_hit("t1", review=_review()),
                       _hit("t2", review=_review(False, r=2))],
                 new_words=[_new("新词甲", review=_review(True, 4, 4, 4))])
    m2 = _result("ttB", hits=[_hit("t3", review=_review(True, 3, 3, 2))],
                 new_words=[_new("新词乙", review=_missed())])
    report = assemble_open_report([m1, m2], _meta())
    assert report["sample_accounting"] == {
        "total": 2, "ok": 2, "failed": 0, "review_failed": 0}
    summary = report["summary"]
    assert summary["total_tags"] == 5
    assert summary["reviewed_tags"] == 5
    assert summary["new_words"] == 2 and summary["hits"] == 3
    assert summary["new_ratio"] == pytest.approx(2 / 5)
    # 漏判（pass=None）排除出通过率分母：3 过 / 4 有判定
    assert summary["pass_rate"] == pytest.approx(3 / 4)
    assert summary["new_pass"] == pytest.approx(1.0)
    assert summary["hit_pass"] == pytest.approx(2 / 3)
    assert summary["review_missing"] == 1
    metrics = summary["metrics"]
    assert metrics["evaluated_tags"] == 5
    assert metrics["undetermined_tags"] == 1
    assert metrics["relevance"]["average"] == pytest.approx((4 + 2 + 4 + 3) / 4)
    assert metrics["relevance"]["distribution"] == [0, 0, 1, 1, 2]
    assert metrics["relevance"]["undetermined"] == 1
    # 三维满分率：仅新词甲 4/4/4，分母 = 三维均有分的 4 行
    assert metrics["three_dimension_high_rate"] == pytest.approx(1 / 4)


def test_review_failed_movie_excluded_but_output_kept():
    m1 = _result("ttA", hits=[_hit("t1")], new_words=[_new()])
    m2 = _result("ttB", hits=[_hit("t2", review=None)],
                 new_words=[_new("保留新词", review=None)],
                 review_error="ReviewRequestError: 评审端点宕机")
    report = assemble_open_report([m1, m2], _meta())
    assert report["sample_accounting"]["review_failed"] == 1
    summary = report["summary"]
    assert summary["total_tags"] == 4          # 产出不作废
    assert summary["new_ratio"] == pytest.approx(2 / 4)
    assert summary["reviewed_tags"] == 2       # 评审失败片排除出分母
    assert summary["pass_rate"] == pytest.approx(1.0)
    assert report["review_failed_movies"] == [
        {"tt": "ttB", "review_error": "ReviewRequestError: 评审端点宕机"}]
    movie_b = next(m for m in report["movies"] if m["tt"] == "ttB")
    assert movie_b["pass_rate"] is None and movie_b["metrics"] is None
    assert movie_b["new_words"][0]["name"] == "保留新词"


def test_produce_failed_movie_accounting():
    m1 = _result("ttA", hits=[_hit("t1")])
    m2 = _result("ttB", ok=False, error="RuntimeError: boom")
    report = assemble_open_report([m1, m2], _meta(done=2, total=2))
    assert report["sample_accounting"] == {
        "total": 2, "ok": 1, "failed": 1, "review_failed": 0}
    assert report["failed_movies"] == [{"tt": "ttB", "error": "RuntimeError: boom"}]
    assert report["summary"]["total_tags"] == 1
    assert report["produced_stats"]["counts"] == [{"tt": "ttA", "hits": 1,
                                                   "new_words": 0}]


def test_movie_rows_carry_review_and_reference():
    reference = {"relevance": 4, "semantics": 3, "value": 3, "reason": "judge 依据"}
    m1 = _result("ttA", hits=[_hit("t1", reference=reference)],
                 new_words=[_new("新词甲")])
    report = assemble_open_report([m1], _meta(done=1, total=1))
    movie = report["movies"][0]
    assert movie["hits"][0]["judge_reference"] == reference
    assert movie["hits"][0]["review"]["relevance"]["score"] == 4
    assert movie["n_hits"] == 1 and movie["n_new_words"] == 1
    assert movie["pass_rate"] == pytest.approx(1.0)
    assert movie["metrics"]["evaluated_tags"] == 2


# ---- banner ----


def test_banner_partial_run():
    report = assemble_open_report([_result("ttA", hits=[_hit("t1")])],
                                  _meta(status="cancelled", done=1, total=3))
    assert any("部分" in b or "不可" in b for b in report["banner"])


def test_banner_same_model():
    report = assemble_open_report(
        [_result("ttA", hits=[_hit("t1")])],
        _meta(done=1, total=1, review={"model": "gen-model",
                                       "same_model": True,
                                       "prompt_version": "open-review-v2"}))
    assert any("同模型" in b for b in report["banner"])


def test_banner_small_sample():
    results = [_result("ttA", hits=[_hit("t1")])] + [
        _result(f"ttF{i}", ok=False, error="boom") for i in range(2)]
    report = assemble_open_report(results, _meta(done=3, total=3))
    assert any("小样本" in b for b in report["banner"])


# ---- judge 审计聚合 ----


def _audit_payload():
    return {
        "merged": [{"tag": "m1"}, {"tag": "m2"}, {"tag": "m3"}, {"tag": "m4"}],
        "accepted": [{"tag": "m1", "candidate": "m1"},
                     {"tag": "库内名", "candidate": "m2"}],
        "verdicts": [
            {"tag": "m1", "relevance": 4, "semantics": 3, "value": 3},
            {"tag": "m2", "relevance": 3, "semantics": 3, "value": 2},
            {"tag": "m3", "relevance": 1, "semantics": 2, "value": 1},
            {"tag": "m4", "relevance": None, "semantics": 3, "value": 2},
        ],
        "dropped": [{"tag": "m3", "why": "judge 评分未过线（relevance=1 < 3）"},
                    {"tag": "m4", "why": "judge 评分未过线（relevance=null < 3）"}],
        "library": {"align": [{"tag": "m1", "band": "duplicate"},
                              {"tag": "m2", "band": "suspect"},
                              {"tag": "m3", "band": "new"}]},
    }


def test_judge_section_aggregates_scores(tmp_path):
    audit_dir = tmp_path / "audit"
    _dump(audit_dir / "ttA.json", _audit_payload())
    m1 = _result("ttA", hits=[_hit("t1")])
    report = assemble_open_report([m1], _meta(done=1, total=1),
                                  audit_dir=audit_dir)
    judge = report["judge"]
    assert judge["self_filter"] == pytest.approx(2 / 4)
    assert judge["scores"]["relevance"] == {
        "n": 3, "average": pytest.approx(8 / 3),
        "distribution": [0, 1, 0, 1, 1], "null": 1}
    assert judge["scores"]["semantics"]["n"] == 4
    assert judge["dropped"] == {
        "judge 评分未过线（relevance=1 < 3）": 1,
        "judge 评分未过线（relevance=null < 3）": 1}
    assert judge["bands"] == {"duplicate": 1, "suspect": 1, "new": 1}
    assert report["warnings"] == []


def test_judge_section_missing_audit_warns(tmp_path):
    m1 = _result("ttA", hits=[_hit("t1")])
    report = assemble_open_report([m1], _meta(done=1, total=1),
                                  audit_dir=tmp_path / "audit")
    assert report["judge"]["self_filter"] is None
    assert report["judge"]["scores"]["relevance"]["n"] == 0
    assert any("审计" in w for w in report["warnings"])


def test_judge_section_tolerates_broken_audit(tmp_path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "ttA.json").write_text("{损坏", encoding="utf-8")
    _dump(audit_dir / "ttB.json", _audit_payload())
    results = [_result("ttA", hits=[_hit("t1")]),
               _result("ttB", hits=[_hit("t2")])]
    report = assemble_open_report(results, _meta(), audit_dir=audit_dir)
    assert report["judge"]["self_filter"] == pytest.approx(2 / 4)  # 仅 ttB 贡献


# ---- 渲染与落盘 ----


def test_render_md_has_sections():
    m1 = _result("ttA", hits=[_hit("t1", "悬疑")], new_words=[_new("新词甲")])
    report = assemble_open_report([m1], _meta(done=1, total=1))
    md = render_open_report_md(report)
    assert "# 开放生成评测报告" in md
    assert "## 汇总" in md and "## 评分分布" in md
    assert "## judge 审计" in md and "## 逐片明细" in md and "## 口径" in md
    assert "悬疑" in md and "新词甲" in md


def test_write_open_report_three_files(tmp_path):
    m1 = _result("ttA", hits=[_hit("t1")], new_words=[_new()])
    report = assemble_open_report([m1], _meta(done=1, total=1))
    write_open_report(tmp_path, report)
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "report.md").exists()
    csv_text = (tmp_path / "metrics.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == "metric,value,n"
    assert "pass_rate,1.0,2" in csv_text
    assert "relevance_average,4.0,2" in csv_text
    assert "new_ratio,0.5,2" in csv_text


# ---- 历史摘要 ----


def test_read_open_summary_five_keys(tmp_path):
    m1 = _result("ttA", hits=[_hit("t1")],
                 new_words=[_new(review=_review(True, 4, 4, 4))])
    report = assemble_open_report([m1], _meta(done=1, total=1))
    write_open_report(tmp_path, report)
    summary = _read_open_summary(tmp_path)
    assert summary == {"pass_rate": pytest.approx(1.0),
                       "new_pass": pytest.approx(1.0),
                       "hit_pass": pytest.approx(1.0),
                       "new_ratio": pytest.approx(0.5),
                       "three_high": pytest.approx(0.5)}


def test_read_open_summary_missing_or_broken(tmp_path):
    keys = {"pass_rate", "new_pass", "hit_pass", "new_ratio", "three_high"}
    assert set(_read_open_summary(tmp_path)) == keys
    assert all(v is None for v in _read_open_summary(tmp_path).values())
    (tmp_path / "report.json").write_text("{截断", encoding="utf-8")
    assert all(v is None for v in _read_open_summary(tmp_path).values())
    _dump(tmp_path / "report.json", {"summary": {"pass_rate": "坏值"}})
    summary = _read_open_summary(tmp_path)
    assert summary["pass_rate"] is None and summary["new_pass"] is None
