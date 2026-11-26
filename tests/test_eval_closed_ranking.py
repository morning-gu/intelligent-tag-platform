"""eval/closed_ranking 的单测：闭环评测指标装配的口径钉死。

被测模块纯标准库、可被脚本与未来离线工具独立复用，测试同样只依赖
pytest 与被测模块本身。这里钉死的是几件容易写错的事：

1. **None≠0**——零方差 Spearman、空输入、该 θ 下无有效影片，格值都是
   None（"算不出"），不是 0（"得 0 分"）；
2. **无效影片不混进平均**——relevance 为空（无交集标签）的样本只计
   `n_invalid`；某 θ 下相关集为空的影片不进该 θ 的格平均；
3. **手算例**——网格值与解析随机基线逐个对得上手工数字，防止公式漂移。
"""

from __future__ import annotations

import pytest

from tagplatform.services.eval.closed_ranking import (
    ClosedSample,
    evaluate_closed,
    random_precision_recall,
    spearman,
)


# ---------------------------------------------------------------- spearman

def test_spearman_perfect_reversed_and_none():
    assert spearman([1, 2, 3], [10, 20, 30]) == 1.0
    assert spearman([1, 2, 3], [30, 20, 10]) == -1.0
    assert spearman([1, 1, 1], [1, 2, 3]) is None  # 零方差 → 算不出，None≠0


def test_spearman_ties_average_ranks():
    # 秩 xs=[1.5,1.5,3]、ys=[1,2,3] → Pearson-on-ranks = 1.5/√3
    assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.866, abs=1e-3)


def test_spearman_mismatched_or_empty_is_none():
    # 长度不等 / 空 → None（None≠0 纪律的输入侧）
    assert spearman([1, 2], [1, 2, 3]) is None
    assert spearman([], []) is None


# ---------------------------------------------------------------- 随机基线

def test_random_precision_recall_edges():
    # 解析期望：E[P@K] = r/n、E[R@K] = min(k, n)/n；n=0 或 r=0 → (0.0, 0.0)
    assert random_precision_recall(10, 3, 5) == pytest.approx((0.3, 0.5))
    assert random_precision_recall(10, 3, 20) == pytest.approx((0.3, 1.0))
    assert random_precision_recall(0, 0, 5) == (0.0, 0.0)
    assert random_precision_recall(10, 0, 5) == (0.0, 0.0)


# ---------------------------------------------------------------- 网格手算例

def test_grid_hand_case():
    # 10 标签：前 3 个 gt=0.9，其余 0.1；ranked 前 5 恰含前 3
    rel = {f"t{i}": (0.9 if i < 3 else 0.1) for i in range(10)}
    s = ClosedSample(ranked=("t0", "t1", "t2", "t3", "t4"), relevance=rel,
                     system_scores={"t0": 1.0, "t1": 0.9, "t2": 0.8,
                                    "t3": 0.7, "t4": 0.6})
    r = evaluate_closed([s], thetas=(0.5,), ks=(5,))
    gp = r.grid[0]
    assert gp.precision == pytest.approx(0.6)   # 3/5
    assert gp.recall == pytest.approx(1.0)      # 3/3
    assert gp.precision_random == pytest.approx(0.3)  # r/n = 3/10
    assert gp.recall_random == pytest.approx(0.5)     # min(k,n)/n


def test_ndcg_identical_ranking_is_one():
    rel = {"a": 0.9, "b": 0.7, "c": 0.5, "d": 0.3}
    s = ClosedSample(ranked=("a", "b", "c", "d"), relevance=rel,
                     system_scores={})
    r = evaluate_closed([s], thetas=(0.4,), ks=(4,))
    assert r.grid[0].ndcg == pytest.approx(1.0)


def test_invalid_sample_excluded_not_averaged():
    good = ClosedSample(ranked=("t0",), relevance={"t0": 0.9},
                        system_scores={"t0": 1.0})
    bad = ClosedSample(ranked=(), relevance={}, system_scores={})  # 无交集
    r = evaluate_closed([good, bad], thetas=(0.5,), ks=(1,))
    assert (r.n_total, r.n_valid, r.n_invalid) == (2, 1, 1)
    assert r.grid[0].precision == pytest.approx(1.0)


def test_theta_empty_relevant_excluded_per_grid():  # spec §7：该 θ 下相关集空的影片不混进平均
    hot = ClosedSample(ranked=("t0", "t1"),
                       relevance={"t0": 0.9, "t1": 0.8},
                       system_scores={"t0": 1.0, "t1": 0.5})
    cold = ClosedSample(ranked=("t0",), relevance={"t0": 0.3},
                        system_scores={"t0": 1.0})  # 0.3 < θ → 该 θ 下无相关
    r = evaluate_closed([hot, cold], thetas=(0.5,), ks=(2,))
    assert r.grid[0].n_valid == 1
    assert r.grid[0].precision == pytest.approx(1.0)  # 只算 hot：前 2 含 2 相关


def test_empty_samples_none_not_zero():
    r = evaluate_closed([], thetas=(0.5,), ks=(5,))
    assert r.grid[0].precision is None  # 分母为 0 → None（None≠0 纪律）
    assert (r.n_total, r.n_valid, r.n_invalid) == (0, 0, 0)
    assert (r.coverage_min, r.coverage_median, r.coverage_max) == (None, None, None)


# ---------------------------------------------------------------- coverage / CI / 常量

def test_coverage_stats():
    # 三样本 coverage = 0.5 / 0.25 / 1.0（relevance 4 键，ranked 2/1/4 条）
    rel = {"t0": 0.9, "t1": 0.8, "t2": 0.7, "t3": 0.6}
    samples = [
        ClosedSample(ranked=("t0", "t1"), relevance=rel, system_scores={}),
        ClosedSample(ranked=("t0",), relevance=rel, system_scores={}),
        ClosedSample(ranked=("t0", "t1", "t2", "t3"), relevance=rel,
                     system_scores={}),
    ]
    r = evaluate_closed(samples, thetas=(0.5,), ks=(4,))
    assert (r.coverage_min, r.coverage_median, r.coverage_max) == (0.25, 0.5, 1.0)


def test_bootstrap_ci_constant_samples():
    # 5 个完全同样本 → 每格 CI 退化为 (值, 值)
    rel = {f"t{i}": (0.9 if i < 3 else 0.1) for i in range(10)}
    s = ClosedSample(ranked=("t0", "t1", "t2", "t3", "t4"), relevance=rel,
                     system_scores={"t0": 1.0, "t1": 0.9, "t2": 0.8,
                                    "t3": 0.7, "t4": 0.6})
    r = evaluate_closed([s] * 5, thetas=(0.5,), ks=(5,))
    gp = r.grid[0]
    assert gp.precision_ci == pytest.approx((0.6, 0.6))
    assert gp.recall_ci == pytest.approx((1.0, 1.0))
    assert gp.ndcg_ci == pytest.approx((1.0, 1.0))
