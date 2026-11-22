"""eval/movie_breakdown 的单测：单片 badcase 指标的口径钉死。

夹具为手造 raw_results.json 行 dict；标准网格 thetas=[0.2,0.4,0.6]、
ks=[5,10,20] → 缺省 θ_p=0.2（DEFAULT_THETA_P）、θ_r=0.6（0.8 的最近
档）。这里钉死几件容易写错的
事：

1. **None≠0**——θ-空 / 无交集 / 失败片的 p/r/spearman 是 None（算不出），
   不是 0（得 0 分）；
2. **对账**——单片 P 与闭环网格 (θ=0.2, k=10) 格 precision 用同一原语、
   同一样本构造，均值精确相等（θ-空/无交集/失败片两侧同被排除）；
3. **防御性**——produced 输入序不影响指标与明细；空间外条目不占 P@K
   的坑、不进计分清单。
"""

from __future__ import annotations

import pytest

from tagplatform.services.eval.closed_ranking import ClosedSample, evaluate_closed
from tagplatform.services.eval.movie_breakdown import (
    breakdown_movie,
    pick_k,
    pick_theta,
    summarize_movies,
)

THETAS = [0.2, 0.4, 0.6]  # → 缺省 θ_p=0.2、θ_r=0.6（0.8 最近档）
KS = [5, 10, 20]  # → k_p = 10、k_r = 20


# ---------------------------------------------------------------- 夹具

def _row(tt, gt, produced, *, ok=True, error=None, title=None, elapsed_ms=100):
    """手造一条 raw_results.json 行（形状见 brief Interfaces）。"""
    return {"tt": tt, "title": title if title is not None else f"片名 {tt}",
            "ok": ok, "error": error, "produced": produced,
            "gt": dict(gt), "elapsed_ms": elapsed_ms}


def _prod(tag_id, sf, *, tag_name=None, confidence=0.9, evidence_count=1):
    """手造一条 produced 条目。"""
    return {"tag_id": tag_id, "tag_name": tag_name or tag_id, "sf": sf,
            "confidence": confidence, "evidence_count": evidence_count}


# ---------------------------------------------------------------- 选 θ / 选 k

def test_pick_theta_and_k_tie_rules():
    # target 参数化（统一约定：P/nDCG→0.2、R→0.8；并列取小）
    assert pick_theta([0.2, 0.6], 0.4) == 0.2  # 距离并列（0.2）取小
    assert pick_theta([0.2, 0.4], 0.4) == 0.4
    assert pick_theta([0.2, 0.4, 0.6], 0.2) == 0.2  # θ_p 目标
    assert pick_theta([0.2, 0.4, 0.6], 0.8) == 0.6  # θ_r 目标（最近档）
    assert pick_k([5, 20], 10) == 5  # 距离并列（5）取小
    assert pick_k([15, 5], 10) == 5
    assert pick_k([15], 10) == 15


# ---------------------------------------------------------------- 命中/噪声/空间外分类

def test_hit_miss_noise_classification():
    gt = {"gA": 0.81, "gB": 0.77, "gC": 0.61, "gD": 0.1}
    row = _row("m1", gt, [_prod("gC", 0.95), _prod("gD", 0.90),
                          _prod("out1", 0.50)])
    library_ids = set(gt) | {"out1"}

    summary = summarize_movies([row], THETAS, KS, library_ids)
    assert (summary["theta_p"], summary["theta_r"], summary["k_p"],
            summary["k_r"]) == (0.2, 0.6, 10, 20)
    srow = summary["rows"][0]
    # 手算（θ_p=0.2/θ_r=0.6，相关集同 0.4 门）：计分条 2 条全进 top-10，
    # 命中 gC 1 条 → p=1/10；相关 3 个 → r=1/3
    assert srow["p_at_k"] == pytest.approx(0.1)
    assert srow["r_at_k"] == pytest.approx(1 / 3)
    # 手算：keys=[gA,gB,gC,gD]，xs=[0,0,0.95,0.9] 秩 [1.5,1.5,4,3]，
    # ys 秩 [4,3,2,1] → -3.5/√22.5 ≈ -0.7379
    assert srow["spearman"] == pytest.approx(-0.7379, abs=1e-3)
    assert (srow["n_produced"], srow["n_relevant"]) == (3, 3)

    detail = breakdown_movie([row], "m1", THETAS, KS, library_ids, {})
    assert (detail["theta_p"], detail["theta_r"]) == (0.2, 0.6)
    produced = detail["produced"]
    assert [p["tag_id"] for p in produced] == ["gC", "gD"]
    assert produced[0]["hit"] is True
    assert produced[0]["gt_score"] == 0.61
    assert produced[1]["hit"] is False
    assert produced[1]["gt_score"] == 0.1  # 原样透传
    assert [o["tag_id"] for o in detail["out_of_space"]] == ["out1"]
    assert detail["out_of_space"][0]["sf"] == 0.50
    # misses 按 gt 分降序：0.81 在前
    assert [(m["tag_id"], m["gt_score"]) for m in detail["misses"]] == [
        ("gA", 0.81), ("gB", 0.77)]


# ---------------------------------------------------------------- 空间外不占坑

def test_out_of_space_excluded_from_metrics_but_listed():
    gt = {f"s{i}": 0.9 for i in range(1, 11)}  # s1..s10 全部 ≥θ
    produced = ([_prod("zzz_out", 100)]
                + [_prod(f"s{i}", 20 - i) for i in range(1, 11)])
    row = _row("m1", gt, produced)
    library_ids = set(gt) | {"zzz_out"}

    detail = breakdown_movie([row], "m1", THETAS, KS, library_ids, {})
    # 空间外条目 sf 最高排第一，但不进计分清单
    assert [p["tag_id"] for p in detail["produced"]] == [
        f"s{i}" for i in range(1, 11)]
    assert detail["out_of_space"] == [
        {"tag_id": "zzz_out", "tag_name": "zzz_out", "sf": 100}]
    assert detail["misses"] == []

    summary = summarize_movies([row], THETAS, KS, library_ids)
    # 空间外不占坑：top-10 恰好 10 条相关计分条 → P=1.0（若占坑则 0.9）
    assert summary["rows"][0]["p_at_k"] == pytest.approx(1.0)
    assert summary["rows"][0]["r_at_k"] == pytest.approx(1.0)
    assert summary["rows"][0]["n_produced"] == 11
    assert summary["rows"][0]["n_relevant"] == 10


# ---------------------------------------------------------------- θ-空：P/R 为 None、Spearman 保留

def test_theta_empty_relevant_nulls_pr_keeps_spearman():
    cold = _row("m-cold", {"low1": 0.15, "low2": 0.05}, [_prod("low1", 0.9)])
    hot = _row("m-hot", {"h1": 0.9, "h2": 0.8},
               [_prod("h1", 0.95), _prod("h2", 0.90)])
    library_ids = {"low1", "low2", "h1", "h2"}

    summary = summarize_movies([cold, hot], THETAS, KS, library_ids)
    by_tt = {r["tt"]: r for r in summary["rows"]}
    assert by_tt["m-cold"]["p_at_k"] is None  # gt∩库非空但全 <θ_p → None≠0
    assert by_tt["m-cold"]["r_at_k"] is None
    # 手算：两点 xs=[0.9,0]、ys=[0.15,0.05] 完全同向 → 1.0
    assert by_tt["m-cold"]["spearman"] == 1.0
    assert by_tt["m-cold"]["n_relevant"] == 0
    # 排序：p=None 垫底，位于非空 P 行之后
    assert [r["tt"] for r in summary["rows"]] == ["m-hot", "m-cold"]


# ---------------------------------------------------------------- 无交集 / 失败片

def test_no_intersection_and_failed_rows_null():
    empty = _row("m-empty", {}, [_prod("x1", 0.9)])
    failed = _row("m-fail", {"f1": 0.9}, [], ok=False, error="boom")
    library_ids = {"f1"}

    summary = summarize_movies([empty, failed], THETAS, KS, library_ids)
    by_tt = {r["tt"]: r for r in summary["rows"]}
    for key in ("spearman", "p_at_k", "r_at_k"):
        assert by_tt["m-empty"][key] is None  # gt 空 → 全 None
        assert by_tt["m-fail"][key] is None  # 失败 → 全 None
    assert by_tt["m-empty"]["n_relevant"] == 0
    assert by_tt["m-fail"]["n_relevant"] == 1  # gt∩库 ≥θ 仍计数（失败也不例外）
    assert by_tt["m-fail"]["error"] == "boom"

    detail = breakdown_movie([empty, failed], "m-fail", THETAS, KS,
                             library_ids, {})
    assert detail["ok"] is False
    assert detail["error"] == "boom"  # error 原样透传
    assert detail["produced"] == []  # 失败片三表全空
    assert detail["out_of_space"] == []
    assert detail["misses"] == []


# ---------------------------------------------------------------- 摘要排序

def test_summary_sort_failed_first_p_asc_nulls_last():
    fail = _row("z9", {}, [], ok=False, error="e")
    b2 = _row("a2", {"r3": 0.9, "r4": 0.8},
              [_prod("r3", 0.9), _prod("r4", 0.8)])
    m1 = _row("m1", {"r1": 0.9, "r2": 0.8},
              [_prod("r1", 0.9), _prod("r2", 0.8)])
    n1 = _row("s3", {"c1": 0.15}, [_prod("c1", 0.9)])
    library_ids = {"r1", "r2", "r3", "r4", "c1"}

    summary = summarize_movies([fail, b2, m1, n1], THETAS, KS, library_ids)
    rows = summary["rows"]
    assert [r["tt"] for r in rows] == ["z9", "a2", "m1", "s3"]
    assert rows[0]["ok"] is False  # 失败置顶
    assert rows[1]["p_at_k"] == pytest.approx(0.2)
    assert rows[2]["p_at_k"] == pytest.approx(0.2)  # 同 p → tt 字典序 a2 < m1
    assert rows[3]["p_at_k"] is None  # null 垫底


# ---------------------------------------------------------------- 防御性重排

def test_produced_order_defensive_resort():
    gt = {"p1": 0.9, "p2": 0.8, "p3": 0.7, "p4": 0.1}
    ordered = [_prod("p1", 0.95), _prod("p2", 0.90), _prod("p3", 0.85),
               _prod("p4", 0.80)]
    row_a = _row("m1", gt, ordered)
    row_b = _row("m1", gt, list(reversed(ordered)))  # 逆序传入
    library_ids = set(gt)

    d1 = breakdown_movie([row_a], "m1", THETAS, KS, library_ids, {})
    d2 = breakdown_movie([row_b], "m1", THETAS, KS, library_ids, {})
    assert [p["tag_id"] for p in d1["produced"]] == ["p1", "p2", "p3", "p4"]
    assert d1 == d2  # 明细与输入序无关
    s1 = summarize_movies([row_a], THETAS, KS, library_ids)["rows"][0]
    s2 = summarize_movies([row_b], THETAS, KS, library_ids)["rows"][0]
    assert s1 == s2  # 指标与输入序无关


# ---------------------------------------------------------------- misses 的名称回填

def test_misses_name_join_missing_none():
    row = _row("m1", {"m1": 0.9, "m2": 0.8}, [])
    detail = breakdown_movie([row], "m1", THETAS, KS, {"m1", "m2"},
                             {"m1": "Tag One"})
    assert [(m["tag_id"], m["tag_name"], m["gt_score"])
            for m in detail["misses"]] == [
        ("m1", "Tag One", 0.9), ("m2", None, 0.8)]  # 缺名 → None


# ---------------------------------------------------------------- 与网格对账

def test_reconciliation_with_grid():
    rows = [
        _row("mA", {"a1": 0.9, "a2": 0.7, "a3": 0.1},
             [_prod("a1", 0.95), _prod("a3", 0.90)]),
        _row("mB", {"b1": 0.9, "b2": 0.8}, [_prod("b1", 0.90), _prod("b2", 0.85)]),
        _row("mC", {"c1": 0.15}, [_prod("c1", 0.90)]),  # θ-空：0.15 < 0.2
        _row("mD", {}, []),  # 无交集
        _row("mE", {"e1": 0.9}, [], ok=False, error="x"),  # 失败片
    ]
    # library_ids ⊇ 全部 gt / produced 标签 → relevance = gt 全表、无空间外
    all_tags = set()
    for r in rows:
        all_tags |= set(r["gt"])
        all_tags |= {p["tag_id"] for p in r["produced"]}
    library_ids = all_tags

    summary = summarize_movies(rows, THETAS, KS, library_ids)
    by_tt = {r["tt"]: r for r in summary["rows"]}
    assert by_tt["mA"]["p_at_k"] == pytest.approx(0.1)  # 命中 a1 / k=10
    assert by_tt["mB"]["p_at_k"] == pytest.approx(0.2)
    assert by_tt["mC"]["p_at_k"] is None  # 单片侧 θ_p-空 → null
    assert by_tt["mD"]["p_at_k"] is None
    assert by_tt["mE"]["p_at_k"] is None

    # 网格侧同法构样本（assemble_report 口径：失败片跳过；relevance = gt∩库）
    def _sample(r, ranked, system_scores):
        return ClosedSample(
            ranked=ranked,
            relevance={t: s for t, s in r["gt"].items() if t in library_ids},
            system_scores=system_scores, label=r["tt"])

    samples = [
        _sample(rows[0], ("a1", "a3"), {"a1": 0.95, "a3": 0.90}),
        _sample(rows[1], ("b1", "b2"), {"b1": 0.90, "b2": 0.85}),
        _sample(rows[2], ("c1",), {"c1": 0.90}),
        _sample(rows[3], (), {}),
    ]
    report = evaluate_closed(samples, THETAS, KS)
    gp = next(g for g in report.grid if g.theta == 0.2 and g.k == 10)
    assert gp.n_valid == 2  # 网格侧：θ0.2-空片被 valid_theta 预滤
    assert report.n_invalid == 1  # 无交集片计 invalid

    # 双路过算：非空 p 均值（按原始行序求和，与网格同序）精确相等
    pvals = [by_tt[r["tt"]]["p_at_k"] for r in rows
             if r["ok"] and by_tt[r["tt"]]["p_at_k"] is not None]
    assert pvals == [0.1, 0.2]
    assert sum(pvals) / len(pvals) == gp.precision


# ---------------------------------------------------------------- θ 可调（增补 2026-11-22）

THETAS_SPLIT = [0.2, 0.4, 0.6, 0.8]  # 拆 θ 夹具：P 门与 R 门可各取一档


def test_default_split_matches_unified_convention():
    """缺省（不带新参）= 统一取格约定：theta_p≈0.2、theta_r≈0.8（四档网格
    精确命中）；payload 换键不加戏；明细三组按约定门逐字段钉死。"""
    gt = {"gA": 0.81, "gB": 0.77, "gC": 0.61, "gD": 0.1}
    row = _row("m1", gt, [_prod("gC", 0.95), _prod("gD", 0.90),
                          _prod("out1", 0.50)])
    library_ids = set(gt) | {"out1"}

    summary = summarize_movies([row], THETAS_SPLIT, KS, library_ids)
    assert "theta_used" not in summary
    assert (summary["theta_p"], summary["theta_r"]) == (0.2, 0.8)
    assert summary["thetas"] == THETAS_SPLIT
    srow = summary["rows"][0]
    assert srow["p_at_k"] == pytest.approx(0.1)
    # θ_r=0.8：相关只 gA(.81)，top-20=[gC,gD] 无命中 → r=0.0
    assert srow["r_at_k"] == pytest.approx(0.0)
    assert srow["spearman"] == pytest.approx(-0.7379, abs=1e-3)
    assert srow["n_relevant"] == 1

    detail = breakdown_movie([row], "m1", THETAS_SPLIT, KS, library_ids, {})
    assert "theta_used" not in detail
    assert (detail["theta_p"], detail["theta_r"]) == (0.2, 0.8)
    assert [p["tag_id"] for p in detail["produced"]] == ["gC", "gD"]
    assert detail["produced"][0]["hit"] is False  # gC .61 < 0.8
    assert [m["tag_id"] for m in detail["misses"]] == ["gA"]  # gB .77 < 0.8


def test_split_theta_classification_follows_theta_r():
    """theta_p 只管 p_at_k；theta_r 管 r_at_k/n_relevant/hit/misses。"""
    gt = {"lo": 0.25, "hi": 0.9}  # lo ∈ [0.2,0.4)、hi ≥ 0.8
    row = _row("m1", gt, [_prod("lo", 0.9)])
    library_ids = set(gt)

    summary = summarize_movies([row], THETAS_SPLIT, KS, library_ids,
                               theta_p=0.2, theta_r=0.8)
    srow = summary["rows"][0]
    assert (summary["theta_p"], summary["theta_r"]) == (0.2, 0.8)
    assert srow["p_at_k"] == pytest.approx(0.1)  # θ_p=0.2：lo 算 hit → 1/10
    assert srow["r_at_k"] == pytest.approx(0.0)  # θ_r=0.8：分母只有 hi，未产出
    assert srow["n_relevant"] == 1  # 只数 ≥0.8

    detail = breakdown_movie([row], "m1", THETAS_SPLIT, KS, library_ids, {},
                             theta_p=0.2, theta_r=0.8)
    assert detail["theta_p"] == 0.2 and detail["theta_r"] == 0.8
    assert [(p["tag_id"], p["gt_score"], p["hit"])
            for p in detail["produced"]] == [("lo", 0.25, False)]  # 0.25 < 0.8
    assert [(m["tag_id"], m["gt_score"]) for m in detail["misses"]] == [
        ("hi", 0.9)]  # 漏检跟 θ_r

    # 对照：θ_r 放到 0.2 → hit 翻真、hi 也入漏检
    detail02 = breakdown_movie([row], "m1", THETAS_SPLIT, KS, library_ids, {},
                               theta_p=0.2, theta_r=0.2)
    assert detail02["produced"][0]["hit"] is True
    assert [m["tag_id"] for m in detail02["misses"]] == ["hi"]


def test_reconciliation_split_theta():
    """拆 θ 对账：非空 p 均值 == 网格 (θ_p, K_P) 格 precision、非空 r 均值 ==
    (θ_r, K_R) 格 recall——null 排除集各对各 θ 的 valid_theta。"""
    rows = [
        _row("mA", {"a1": 0.25, "a2": 0.9},
             [_prod("a1", 0.95), _prod("a2", 0.90)]),
        _row("mB", {"b1": 0.3, "b2": 0.85}, [_prod("b1", 0.90)]),
        _row("mC", {"c1": 0.3}, [_prod("c1", 0.90)]),  # 只在 0.2 门非空
        _row("mD", {}, []),  # 无交集
        _row("mE", {"e1": 0.9}, [], ok=False, error="x"),  # 失败片
    ]
    all_tags = set()
    for r in rows:
        all_tags |= set(r["gt"])
        all_tags |= {p["tag_id"] for p in r["produced"]}
    library_ids = all_tags

    summary = summarize_movies(rows, THETAS_SPLIT, KS, library_ids,
                               theta_p=0.2, theta_r=0.8)
    by_tt = {r["tt"]: r for r in summary["rows"]}
    # 手算：p@0.2 → mA 2/10、mB 1/10、mC 1/10、mD/mE null
    assert [by_tt[r["tt"]]["p_at_k"] for r in rows[:3]] == [
        pytest.approx(0.2), pytest.approx(0.1), pytest.approx(0.1)]
    # r@0.8 → mA 1.0（a2 命中）、mB 0.0（b2 漏）、mC null（θ-空）、mD/mE null
    assert by_tt["mA"]["r_at_k"] == pytest.approx(1.0)
    assert by_tt["mB"]["r_at_k"] == pytest.approx(0.0)
    assert by_tt["mC"]["r_at_k"] is None

    samples = []
    for r in rows:
        if not r["ok"]:
            continue
        ranked = tuple(p["tag_id"] for p in r["produced"])
        scores = {p["tag_id"]: p["sf"] for p in r["produced"]}
        samples.append(ClosedSample(
            ranked=ranked, relevance=r["gt"], system_scores=scores,
            label=r["tt"]))
    report = evaluate_closed(samples, THETAS_SPLIT, KS)
    g_p = next(g for g in report.grid if g.theta == 0.2 and g.k == 10)
    g_r = next(g for g in report.grid if g.theta == 0.8 and g.k == 20)
    pvals = [by_tt[r["tt"]]["p_at_k"] for r in rows
             if r["ok"] and by_tt[r["tt"]]["p_at_k"] is not None]
    rvals = [by_tt[r["tt"]]["r_at_k"] for r in rows
             if r["ok"] and by_tt[r["tt"]]["r_at_k"] is not None]
    assert g_p.n_valid == 3 and g_r.n_valid == 2  # 各 θ 各的 valid 集
    assert sum(pvals) / len(pvals) == g_p.precision
    assert sum(rvals) / len(rvals) == g_r.recall


def test_split_theta_null_rules_independent():
    """θ_p 门非空、θ_r 门空 → p 非 null、r null、spearman 照算；排序垫底只看 p。"""
    m1 = _row("m1", {"x1": 0.3, "x2": 0.5}, [_prod("x1", 0.9)])
    n1 = _row("null-p", {"y1": 0.1}, [_prod("y1", 0.9)])  # 0.1 连 0.2 门都空
    library_ids = {"x1", "x2", "y1"}

    summary = summarize_movies([m1, n1], THETAS_SPLIT, KS, library_ids,
                               theta_p=0.2, theta_r=0.8)
    by_tt = {r["tt"]: r for r in summary["rows"]}
    assert by_tt["m1"]["p_at_k"] == pytest.approx(0.1)  # x1 命中 1/10
    assert by_tt["m1"]["r_at_k"] is None  # 全 <0.8 → θ_r 空独立判
    # 手算：xs=[0.9,0]、ys=[0.3,0.5] → 秩反序 → -1.0
    assert by_tt["m1"]["spearman"] == pytest.approx(-1.0)
    assert by_tt["m1"]["n_relevant"] == 0
    assert by_tt["null-p"]["p_at_k"] is None
    # 垫底键只看 p：p 非空的 m1 排在 p null 的 null-p 之前
    assert [r["tt"] for r in summary["rows"]] == ["m1", "null-p"]
