"""eval/open_report 的单测：三维口径报告组装 / 渲染 / 落盘 / 历史摘要。

夹具镜像 mtg 管线 `_build_report` 的键形（runs / merged / accepted /
verdicts（三维分）/ dropped / library.align / library.truncated）与
OpenMovieResult 的行契约
（hits/new_words 带 review = 三维分 + 推导 pass + quote，judge_reference
可选）。钉死：

1. 汇总微口径：通过率分母只计有判定行（漏判 pass=None 排除），评审
   失败片整体排除（None≠0，产出不作废仍计 total_tags/new_ratio）；
2. 三维指标复用 quality_metrics（与生产批次汇总同机制）；
3. judge 审计聚合（数量漏斗 / 三维分聚合 / dropped / bands）与缺失
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
    fill_open_report_mounts,
    render_open_report_md,
    upgrade_open_report,
    write_open_report,
)
from tagplatform.services.eval.open_service import OpenMovieResult


def _review(passed=True, r=4, s=3, v=3, quote="原文摘录"):
    return {"pass": passed,
            "relevance": {"score": r, "reason": "相关理由"},
            "semantics": {"score": s, "reason": "语义理由"},
            "value": {"score": v, "reason": "价值理由"},
            "reason": "相关理由；语义理由；价值理由", "evaluated": True, "evidence_scene_ids": ["scene_1", "scene_2"]}


def _missed():
    return {"pass": None,
            "relevance": {"score": None, "reason": "评审未返回该条判定"},
            "semantics": {"score": None, "reason": "评审未返回该条判定"},
            "value": {"score": None, "reason": "评审未返回该条判定"},
            "reason": "评审未返回该条判定", "evaluated": False, "evidence_scene_ids": []}


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


def test_historical_mounts_follow_final_name_origin_and_output_order(tmp_path):
    movie = {"tt": "tt123", "hits": [_hit("t1", name="悬疑")],
             "new_words": [_new("悬疑"), _new("悬疑"), _new("缺少记录")]}
    _dump(tmp_path / "tt123.json", {"accepted": [
        {"tag": "悬疑", "origin": "generated", "dir_id": "dir_scene", "target_type": "scene"},
        {"tag": "悬疑", "origin": "catalog", "dir_id": "dir_catalog",
         "dir_name": "氛围", "target_type": "content"},
        {"tag": "悬疑", "origin": "generated", "dir_id": "dir_character", "target_type": "character"},
        {"tag": "缺少记录", "origin": "generated", "target_type": "scene"},
        {"tag": "缺少记录", "origin": "generated", "target_type": "content"},
    ]})

    fill_open_report_mounts([movie], tmp_path)

    assert movie["hits"][0]["dir_name"] == "氛围"
    assert movie["hits"][0]["target_type"] == "content"
    assert [(row["dir_id"], row["target_type"]) for row in movie["new_words"][:2]] == [
        ("dir_scene", "scene"), ("dir_character", "character"),
    ]
    assert "target_type" not in movie["new_words"][2]


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
    assert summary["reviewed_tags"] == 4
    assert summary["new_words"] == 2 and summary["hits"] == 3
    assert summary["new_ratio"] == pytest.approx(2 / 5)
    # 漏判（pass=None）排除出通过率分母：3 过 / 4 有判定
    assert summary["pass_rate"] == pytest.approx(3 / 4)
    assert summary["new_pass"] == pytest.approx(1.0)
    assert summary["hit_pass"] == pytest.approx(2 / 3)
    assert summary["review_missing"] == 1
    metrics = summary["metrics"]
    assert metrics["evaluated_tags"] == 4
    assert metrics["coverage"] == 4 / 5
    assert metrics["undetermined_tags"] == 0
    assert metrics["relevance"]["average"] == pytest.approx((4 + 2 + 4 + 3) / 4)
    assert metrics["relevance"]["distribution"] == [0, 0, 1, 1, 2]
    assert metrics["relevance"]["undetermined"] == 0
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
    assert movie_b["pass_rate"] is None and movie_b["metrics"]["coverage"] == 0
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


def test_banner_conditions_and_clean_run():
    partial = assemble_open_report([_result("ttA", hits=[_hit("t1")])],
                                   _meta(status="cancelled", done=1, total=3))
    assert any("部分" in b or "不可" in b for b in partial["banner"])

    same_model = assemble_open_report(
        [_result("ttA", hits=[_hit("t1")])],
        _meta(done=1, total=1, review={"model": "gen-model",
                                       "same_model": True,
                                       "prompt_version": "open-review-v2"}))
    assert any("共享判断偏差" in b for b in same_model["banner"])

    small = assemble_open_report(
        [_result("ttA", hits=[_hit("t1")])]
        + [_result(f"ttF{i}", ok=False, error="boom") for i in range(2)],
        _meta(done=3, total=3))
    assert any("小样本" in b for b in small["banner"])

    clean = assemble_open_report(
        [_result(f"tt{i}", hits=[_hit("t1")]) for i in range(3)],
        _meta(done=3, total=3))
    assert clean["banner"] == []


# ---- judge 审计聚合 ----


def _audit_payload():
    return {
        "runs": [{"run": 0, "scope": "scene", "items": 6},
                 {"run": 1, "scope": "film", "items": 2}],
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
                              {"tag": "m3", "band": "new"}],
                    "truncated": []},
    }


def test_judge_section_aggregates_scores(tmp_path):
    audit_dir = tmp_path / "audit"
    _dump(audit_dir / "ttA.json", _audit_payload())
    m1 = _result("ttA", hits=[_hit("t1")])
    report = assemble_open_report([m1], _meta(done=1, total=1),
                                  audit_dir=audit_dir)
    judge = report["judge"]
    # 漏斗：raw 8 → merged 4（过滤段淘汰 2）→ passed 2 → 配额保留 2 → final 2
    assert judge["funnel"] == {
        "raw": 8, "merged": 4, "verdicts": 4, "judge_passed": 2,
        "rank_kept": 2, "final": 2,
        "merge_keep_rate": pytest.approx(0.5),
        "judge_coverage": pytest.approx(1.0),
        "judge_pass_rate": pytest.approx(0.5),
        "judge_pass_rate_scored": pytest.approx(0.5),
        "rank_keep_rate": pytest.approx(1.0),
        "dir_mount_rate": pytest.approx(1.0),
        "final_keep_rate": pytest.approx(0.5),
    }
    assert judge["scores"]["relevance"] == {
        "n": 3, "average": pytest.approx(8 / 3),
        "distribution": [0, 1, 0, 1, 1], "null": 1}
    assert judge["scores"]["semantics"]["n"] == 4
    assert judge["dropped"] == {
        "judge 评分未过线（relevance=1 < 3）": 1,
        "judge 评分未过线（relevance=null < 3）": 1}
    assert judge["bands"] == {"duplicate": 1, "suspect": 1, "new": 1}
    assert report["warnings"] == []


def test_judge_section_handles_weighted_float_value_scores(tmp_path):
    payload = _audit_payload()
    payload["verdicts"] = [
        {"tag": "m1", "relevance": 4, "semantics": 3, "value": 3.65},
        {"tag": "m2", "relevance": 3, "semantics": 3, "value": 2.40},
        {"tag": "m3", "relevance": 1, "semantics": 2, "value": 1.50},
        {"tag": "m4", "relevance": None, "semantics": 3, "value": None},
    ]
    audit_dir = tmp_path / "audit"
    _dump(audit_dir / "ttA.json", payload)
    m1 = _result("ttA", hits=[_hit("t1", review=_review(v=2.85))])
    report = assemble_open_report([m1], _meta(done=1, total=1), audit_dir=audit_dir)
    assert report["judge"]["scores"]["value"] == {
        "n": 3,
        "average": pytest.approx((3.65 + 2.40 + 1.50) / 3),
        # 1.50 -> 2, 2.40 -> 2, 3.65 -> 4
        "distribution": [0, 0, 2, 0, 1],
        "null": 1,
    }
    md = render_open_report_md(report)
    assert "| 2.85 |" in md


def test_judge_section_missing_audit_warns(tmp_path):
    m1 = _result("ttA", hits=[_hit("t1")])
    report = assemble_open_report([m1], _meta(done=1, total=1),
                                  audit_dir=tmp_path / "audit")
    assert report["judge"]["funnel"] is None
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
    assert report["judge"]["funnel"]["final_keep_rate"] == pytest.approx(2 / 4)  # 仅 ttB 贡献


def test_judge_funnel_separates_filter_and_dir_drops(tmp_path):
    """漏斗推导钉死：过滤段五类淘汰原因计入 judge 未通过，目录归属段
    淘汰只影响挂载率；truncated 从 judge 通过数里扣排序配额。"""
    payload = {
        "runs": [{"run": 0, "items": 10}, {"run": 1, "items": 2}],
        "merged": [{"tag": f"m{i}"} for i in range(8)],
        "verdicts": [{"tag": f"m{i}", "relevance": 3, "semantics": 3, "value": 2}
                     for i in range(7)],
        "dropped": [
            {"tag": "m0", "why": "judge 未返回判决"},
            {"tag": "m1", "why": "judge 评分未过线（relevance=1 < 3）"},
            {"tag": "m2", "why": "judge 判定把握 0.40 < 0.5"},
            {"tag": "m3", "why": "无有效场景证据"},
            {"tag": "m4", "why": "全片级但只有单场支撑"},
            {"tag": "m5", "why": "无可归属目录"},
            {"tag": "m6", "why": "无可归属目录"},
        ],
        "library": {"truncated": [{"tag": "m7"}]},
        "accepted": [],
    }
    audit_dir = tmp_path / "audit"
    _dump(audit_dir / "ttA.json", payload)
    report = assemble_open_report([_result("ttA", hits=[_hit("t1")])],
                                  _meta(done=1, total=1), audit_dir=audit_dir)
    funnel = report["judge"]["funnel"]
    # raw 12 → merged 8 → verdicts 7；过滤段淘汰 5 → passed 3；
    # 配额截断 1 → rank_kept 2；目录归属淘汰 2 → final 0
    assert funnel["raw"] == 12 and funnel["merged"] == 8
    assert funnel["verdicts"] == 7 and funnel["judge_passed"] == 3
    assert funnel["rank_kept"] == 2 and funnel["final"] == 0
    assert funnel["judge_coverage"] == pytest.approx(7 / 8)
    assert funnel["judge_pass_rate"] == pytest.approx(3 / 8)
    assert funnel["judge_pass_rate_scored"] == pytest.approx(3 / 7)
    assert funnel["rank_keep_rate"] == pytest.approx(2 / 3)
    assert funnel["dir_mount_rate"] == 0.0
    assert funnel["final_keep_rate"] == 0.0


def test_judge_funnel_counts_clustering_before_quota(tmp_path):
    audit_dir = tmp_path / "audit"
    _dump(audit_dir / "ttA.json", {
        "runs": [{"items": 8}],
        "merged": [{"tag": str(index)} for index in range(8)],
        "verdicts": [{"tag": str(index)} for index in range(8)],
        "clustering": {"input": 6, "clusters": [{"representative": "0"}, {"representative": "1"}]},
        "library": {"truncated": [{"tag": "3"}]},
        "accepted": [{"tag": "0"}, {"tag": "1"}, {"tag": "2"}],
    })
    report = assemble_open_report([_result("ttA")], _meta(done=1, total=1), audit_dir=audit_dir)
    funnel = report["judge"]["funnel"]
    assert funnel["judge_passed"] == 8
    assert funnel["cluster_kept"] == 4 and funnel["cluster_keep_rate"] == 0.5
    assert funnel["rank_kept"] == 3 and funnel["rank_keep_rate"] == 0.75


def test_judge_funnel_stub_audit_without_keys_is_none(tmp_path):
    """替身审计（只有 task_id/stub）无漏斗键 → funnel None，不伪造 0。"""
    audit_dir = tmp_path / "audit"
    _dump(audit_dir / "ttA.json", {"task_id": "ttA", "stub": True})
    report = assemble_open_report([_result("ttA", hits=[_hit("t1")])],
                                  _meta(done=1, total=1), audit_dir=audit_dir)
    assert report["judge"]["funnel"] is None


# ---- 旧 schema 惰性升级 ----


def _old_run_dir(tmp_path):
    from dataclasses import asdict
    m1 = _result("ttA", hits=[_hit("t1")])
    meta = _meta(done=1, total=1)
    meta["run_id"] = "run-old"
    _dump(tmp_path / "meta.json", meta)
    _dump(tmp_path / "raw_results.json", [asdict(m1)])
    _dump(tmp_path / "audit" / "ttA.json", _audit_payload())
    _dump(tmp_path / "report.json",
          {"schema_version": 2, "judge": {"self_filter": 0.5}})
    return m1, meta


def test_upgrade_open_report_rebuilds_old_schema(tmp_path):
    _old_run_dir(tmp_path)
    report = upgrade_open_report(tmp_path)
    assert report is not None and report["schema_version"] == 3
    assert report["judge"]["funnel"]["merged"] == 4
    assert report["summary"]["total_tags"] == 1
    on_disk = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert on_disk == report  # 三件套重写，读侧不再 410
    assert (tmp_path / "report.md").exists()
    assert (tmp_path / "metrics.csv").exists()
    # 材料缺失 → None；run 非终态 → None（运行中不升级）
    assert upgrade_open_report(tmp_path / "missing") is None
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    meta["status"] = "running"
    _dump(tmp_path / "meta.json", meta)
    assert upgrade_open_report(tmp_path) is None


def test_read_open_summary_upgrades_old_schema(tmp_path):
    _old_run_dir(tmp_path)
    summary = _read_open_summary(tmp_path)
    assert summary["pass_rate"] == pytest.approx(1.0)
    assert summary["hit_pass"] == pytest.approx(1.0)
    on_disk = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 3


# ---- 渲染与落盘 ----


def test_render_md_has_sections():
    hit = {**_hit("t1", "悬疑"), "dir_name": "情绪氛围", "target_type": "content"}
    word = {**_new("新词甲"), "level": "scene", "dir_id": "dir_story", "target_type": "scene"}
    m1 = _result("ttA", hits=[hit], new_words=[word])
    report = assemble_open_report([m1], _meta(done=1, total=1))
    assert "note" not in report
    md = render_open_report_md(report)
    assert "## 汇总" in md and "## 评分分布" in md and "## 逐片明细" in md
    assert "悬疑" in md and "情绪氛围" in md
    assert "新词甲" in md and "dir_story" in md
    assert "银标准" not in md


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
    assert "merge_keep_rate,," in csv_text  # 无审计 → 漏斗率空值


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
