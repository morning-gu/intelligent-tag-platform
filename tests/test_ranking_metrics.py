"""排序指标单测：`tagplatform.eval.ranking` 的纯函数口径。

这些函数是"归属排序准不准"的唯一算分口径，错一个折扣因子就会让所有
基线数字失去意义，因此逐条钉死手算的黄金值：

- 折扣因子 1/log2(rank+1)：首位 1.0、次位 0.6309、三位 0.5；
- 单相关项时 Recall ≡ HitRate、Precision = HitRate / k、NDCG = 1/log2(rank+1)；
- 分级相关度：rel=2 精确目录 > rel=1 同父兄弟目录 > 0；
- 随机基线是解析期望：候选数 = 相关数时 HitRate 必为 1；
- 自举置信区间只依赖种子，同输入同输出（可回归）。
"""

from __future__ import annotations

import math

import pytest

from tagplatform.eval import (
    GRADE_EXACT,
    GRADE_NONE,
    GRADE_SIBLING,
    RankingSample,
    bootstrap_ci,
    evaluate_samples,
    hit_rate_at_k,
    lift,
    macro_average,
    mrr,
    ndcg_at_k,
    precision_at_k,
    random_baseline,
    recall_at_k,
)

EXACT = GRADE_EXACT
SIB = GRADE_SIBLING


# ---------------------------------------------------------------- 命中类

def test_hit_rate_counts_first_k_only():
    relevance = {"d1": EXACT, "d2": 0, "d3": 0}
    ranked = ("d2", "d1", "d3")
    assert hit_rate_at_k(ranked, relevance, 1) == 0.0
    assert hit_rate_at_k(ranked, relevance, 2) == 1.0


def test_hit_rate_sibling_counts_as_relevant_by_default():
    relevance = {"d_exact": EXACT, "d_sib": SIB, "d_other": 0}
    ranked = ("d_sib",)
    assert hit_rate_at_k(ranked, relevance, 1) == 1.0
    # 收紧到必须精确命中时，兄弟目录不算
    assert hit_rate_at_k(ranked, relevance, 1, relevant_grade=EXACT) == 0.0


def test_hit_rate_without_relevant_ids_is_zero():
    assert hit_rate_at_k(("d1",), {}, 5) == 0.0
    assert hit_rate_at_k(("d1",), {"d1": 0}, 5) == 0.0


def test_recall_equals_hit_rate_for_single_relevant():
    """单相关项：命中即全召回，两者必须相等（文档承诺的退化关系）。"""
    relevance = {"d1": EXACT, "d2": 0}
    for ranked in (("d1", "d2"), ("d2", "d1"), ("d2",)):
        assert recall_at_k(ranked, relevance, 2) == hit_rate_at_k(ranked, relevance, 2)


def test_precision_is_hit_rate_over_k():
    relevance = {"d1": EXACT, "d2": 0}
    assert precision_at_k(("d1", "d2"), relevance, 2) == pytest.approx(0.5)
    assert precision_at_k(("d1", "d2"), relevance, 5) == pytest.approx(0.2)
    # 候选列表短于 k 时分母仍是 k（缺位算没命中）
    assert precision_at_k(("d1",), relevance, 4) == pytest.approx(0.25)


def test_precision_two_relevant():
    relevance = {"d1": EXACT, "d2": SIB, "d3": 0, "d4": 0}
    assert precision_at_k(("d1", "d2", "d3", "d4"), relevance, 4) == pytest.approx(0.5)
    assert recall_at_k(("d1", "d2", "d3", "d4"), relevance, 4) == pytest.approx(1.0)


# ---------------------------------------------------------------- NDCG

def test_ndcg_perfect_ranking_is_one():
    relevance = {"d1": EXACT, "d2": 0, "d3": 0}
    assert ndcg_at_k(("d1", "d2", "d3"), relevance, 3) == pytest.approx(1.0)


def test_ndcg_second_position_uses_log2_discount():
    relevance = {"d1": EXACT, "d2": 0}
    # 唯一相关项排在第 2：DCG = 2/log2(3)
    assert ndcg_at_k(("d2", "d1"), relevance, 2) == pytest.approx(1 / math.log2(3))


def test_ndcg_third_position_uses_log2_discount():
    relevance = {"d1": EXACT}
    assert ndcg_at_k(("x", "y", "d1"), relevance, 3) == pytest.approx(0.5)


def test_ndcg_graded_relevance_gives_partial_credit():
    """精确目录排第一、兄弟目录排第二：满分排序也是这个顺序，得 1.0。"""
    relevance = {"d_exact": EXACT, "d_sib": SIB, "d_other": 0}
    assert ndcg_at_k(("d_exact", "d_sib"), relevance, 2) == pytest.approx(1.0)
    # 兄弟在前、精确在后：拿不到满分但明显高于 0
    partial = ndcg_at_k(("d_sib", "d_exact"), relevance, 2)
    assert 0.0 < partial < 1.0


def test_ndcg_empty_relevance_is_zero():
    assert ndcg_at_k(("d1",), {}, 5) == 0.0


# ---------------------------------------------------------------- MRR

def test_mrr_reciprocal_rank():
    relevance = {"d1": EXACT}
    assert mrr(("d1",), relevance) == pytest.approx(1.0)
    assert mrr(("x", "d1"), relevance) == pytest.approx(0.5)
    assert mrr(("x", "y", "d1"), relevance) == pytest.approx(1 / 3)
    assert mrr(("x", "y"), relevance) == 0.0


# ---------------------------------------------------------------- 聚合

def test_macro_average_distinguishes_empty_from_zero():
    assert macro_average([]) is None
    assert macro_average([0.0, 0.0]) == 0.0
    assert macro_average([1.0, 0.0]) == pytest.approx(0.5)


def test_lift_against_baseline():
    assert lift(0.6, 0.2) == pytest.approx(3.0)
    assert lift(0.6, 0.0) is None
    assert lift(None, 0.2) is None


# ---------------------------------------------------------------- 随机基线

def test_random_baseline_hit_rate_when_all_candidates_relevant():
    """候选全是相关的极端情形：任何随机排序都必然命中。"""
    relevance = {"d1": EXACT, "d2": EXACT}
    assert random_baseline(2, relevance, 1)["hit_rate"] == pytest.approx(1.0)


def test_random_baseline_hit_rate_single_relevant():
    """n 个候选、1 个相关：P(首位命中) = 1/n，P(前 k 命中) = k/n。"""
    relevance = {"d1": EXACT, "d2": 0, "d3": 0, "d4": 0}
    assert random_baseline(4, relevance, 1)["hit_rate"] == pytest.approx(0.25)
    assert random_baseline(4, relevance, 2)["hit_rate"] == pytest.approx(0.5)
    assert random_baseline(4, relevance, 4)["hit_rate"] == pytest.approx(1.0)


def test_random_baseline_ndcg_is_below_perfect():
    relevance = {"d1": EXACT, "d2": 0, "d3": 0}
    baseline = random_baseline(3, relevance, 3)
    assert 0.0 < baseline["ndcg"] < 1.0


def test_random_baseline_empty():
    assert random_baseline(0, {}, 5) == {"hit_rate": 0.0, "ndcg": 0.0, "mrr": 0.0}


# ---------------------------------------------------------------- 报告

def test_evaluate_samples_macro_averages():
    good = RankingSample(ranked=("d1", "d2"), relevance={"d1": EXACT, "d2": 0},
                         label="good")
    bad = RankingSample(ranked=("d2", "d1"), relevance={"d1": EXACT, "d2": 0},
                        label="bad")
    report = evaluate_samples([good, bad], (1, 2))
    assert report.n_samples == 2
    assert report.metric("hit_rate", 1) == pytest.approx(0.5)
    assert report.metric("hit_rate", 2) == pytest.approx(1.0)
    assert report.metric("mrr", 1) == pytest.approx((1.0 + 0.5) / 2)
    assert report.raw("hit_rate", 1) == [1.0, 0.0]


def test_evaluate_samples_warns_when_k_exceeds_candidate_list():
    sample = RankingSample(ranked=("d1",), relevance={"d1": EXACT})
    report = evaluate_samples([sample], (3, 5), max_ranked=3)
    assert any("超过候选列表长度" in w for w in report.warnings)


def test_evaluate_samples_flags_samples_without_truth():
    """没有正确目录的样本不进平均，但要在告警里点出来（不可归属率）。"""
    broken = RankingSample(ranked=("d1",), relevance={}, label="无处归属")
    ok = RankingSample(ranked=("d1",), relevance={"d1": EXACT}, label="正常")
    report = evaluate_samples([broken, ok], (1,))
    assert report.metric("hit_rate", 1) == pytest.approx(1.0)
    assert any("不可归属" in w for w in report.warnings)


def test_evaluate_samples_empty_input():
    report = evaluate_samples([], (1,))
    assert report.n_samples == 0
    assert report.metric("ndcg", 1) is None
    assert report.warnings


# ---------------------------------------------------------------- 置信区间

def test_bootstrap_ci_is_deterministic_and_brackets_mean():
    values = [0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0]
    first = bootstrap_ci(values, n_boot=300)
    second = bootstrap_ci(values, n_boot=300)
    assert first == second  # 固定种子 → 可回归
    mean = sum(values) / len(values)
    assert first[0] <= mean <= first[1]
    assert first[0] < first[1]


def test_bootstrap_ci_of_constant_is_degenerate():
    assert bootstrap_ci([1.0, 1.0, 1.0]) == (1.0, 1.0)


def test_bootstrap_ci_empty_is_none():
    assert bootstrap_ci([]) is None