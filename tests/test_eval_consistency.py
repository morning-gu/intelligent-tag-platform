"""一致性指标：手算锁定口径，避免"看起来合理"的实现漂移。

上游（genometag-mllm `genometag/eval/metrics.py`）用 numpy 写；本地按分层
红线（§9.4，`eval` 包纯标准库）重写，所以**数值必须逐一对齐**——下面每个
期望值都是手算的，不是跑一遍抄输出。
"""

from __future__ import annotations

import math

import pytest

from tagplatform.eval import (
    consistent_share,
    consistency_std,
    icc_2_1,
    jaccard,
    mean_pairwise_jaccard,
    presence_matrix,
    stable_share,
)


# ---------------------------------------------------------------- 逐行离散度

def test_consistency_std_is_population_std():
    """ddof=0：`[0,1,1]` 均值 2/3，方差 (4/9+1/9+1/9)/3 = 2/9，std = 0.4714。"""
    values = consistency_std([[1.0, 1.0, 1.0], [0.0, 1.0, 1.0]])

    assert values[0] == 0.0
    assert values[1] == pytest.approx(math.sqrt(2 / 9))


def test_consistency_std_ddof0_is_smaller_than_ddof1():
    """3 轮时两者差 √(3/2)≈1.2247 倍；口径写错会让回归数字整体偏大。"""
    population = consistency_std([[0.0, 1.0, 1.0]])[0]
    sample = math.sqrt(2 / 9) * math.sqrt(3 / 2)

    assert population == pytest.approx(sample / math.sqrt(3 / 2))


def test_consistency_std_rejects_ragged_matrix():
    with pytest.raises(ValueError, match="每行长度必须一致"):
        consistency_std([[1.0, 2.0], [1.0]])


def test_consistency_std_empty_input():
    assert consistency_std([]) == []


# ---------------------------------------------------------------- ICC(2,1)

def test_icc_perfect_agreement_is_one():
    """每行内部完全一致、行间明显不同 → 单次测量完全可信。"""
    assert icc_2_1([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0], [3.0, 3.0, 3.0]]) == pytest.approx(1.0)


def test_icc_hand_computed_two_by_two():
    """2×2 手算：[[0,1],[1,1]]。

    grand = 3/4 = 0.75；行均值 0.5 / 1.0；列均值 0.5 / 1.0。
    SS行 = 2·[(0.5−0.75)² + (1.0−0.75)²] = 2·(0.0625+0.0625) = 0.25
    SS列 = 2·[(0.5−0.75)² + (1.0−0.75)²] = 0.25
    SS总 = (0−0.75)² + 3·(1−0.75)² = 0.5625 + 0.1875 = 0.75
    SSE  = 0.75 − 0.25 − 0.25 = 0.25   →  MSE = 0.25/1 = 0.25
    MSR  = 0.25/1 = 0.25；MSC = 0.25/1 = 0.25
    denom = 0.25 + 1·0.25 + 2·(0.25−0.25)/2 = 0.5
    ICC  = (MSR − MSE)/denom = (0.25−0.25)/0.5 = 0.0

    即“受试间差异完全被残差解释”，单轮读数与噪声无差别。
    """
    assert icc_2_1([[0.0, 1.0], [1.0, 1.0]]) == pytest.approx(0.0)


def test_icc_negative_when_runs_disagree_more_than_items_differ():
    """2×3 手算：[[0,1,0],[1,0,1]]。

    grand=0.5；行均值 1/3 与 2/3；三列均值都是 0.5（列间无系统偏移）。
    SS行 = 3·[(−1/6)²+(1/6)²] = 1/6；SS列 = 0；SS总 = 6·0.25 = 1.5
    SSE = 1.5 − 1/6 = 4/3 → MSE = (4/3)/2 = 2/3
    denom = 1/6 + 2·(2/3) + 3·(0−2/3)/2 = 1/6 + 4/3 − 1 = 1/2
    ICC = (1/6 − 2/3)/(1/2) = −1.0

    读法：轮与轮之间几乎完全相反，单轮结果比取均值差得多。
    """
    assert icc_2_1([[0.0, 1.0, 0.0], [1.0, 0.0, 1.0]]) == pytest.approx(-1.0)


def test_icc_returns_none_not_zero_when_degenerate():
    """两种退化情形都返回 `None`，且都**不是** 0.0。

    1) 全表同值 → 所有平方和为 0，分母不存在；
    2) 每个受试项的各轮均值都相同（如 `[[1,0],[0,1]]`）→ MSR=0，代入
       `denom = 0 + MSE + k·(MSC−MSE)/n` 恰好相互抵消也为 0。

    "算不出"和"完全不一致"是两件事，混成一个数字会让报表得出相反结论
    （这也是本实现唯一有意偏离上游的地方——上游记 0.0）。
    """
    assert icc_2_1([[1.0, 1.0], [1.0, 1.0]]) is None
    assert icc_2_1([[1.0, 0.0], [0.0, 1.0]]) is None


def test_icc_returns_none_for_too_few_items_or_runs():
    assert icc_2_1([[1.0, 2.0, 3.0]]) is None       # n=1
    assert icc_2_1([[1.0], [2.0]]) is None          # k=1
    assert icc_2_1([]) is None


def test_icc_tolerates_float_noise_in_residual():
    """浮点累加可能给出 −1e-17 的"负平方和"，必须夹成 0 而不是判成非法。"""
    rows = [[0.1, 0.2], [0.3, 0.4000000000000001]]

    assert icc_2_1(rows) is not None


# ---------------------------------------------------------------- 集合口径

def test_presence_matrix_sorted_and_deduped():
    """行按字典序、同轮重复标签只算一次（否则会让权重被放大）。"""
    items, matrix = presence_matrix([["b", "a", "a"], ["a"], ["c"]])

    assert items == ["a", "b", "c"]
    assert matrix == [[1.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


def test_presence_matrix_empty():
    assert presence_matrix([]) == ([], [])


def test_jaccard_bounds_and_empty_rule():
    assert jaccard(["a"], ["a"]) == pytest.approx(1.0)
    assert jaccard(["a"], ["b"]) == pytest.approx(0.0)
    assert jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
    # 两边都空 → 0.0（不是 1.0）：没有内容就没有可谈的一致性。
    assert jaccard([], []) == 0.0
    assert jaccard([], ["a"]) == 0.0


def test_mean_pairwise_jaccard_hand_computed():
    """三轮两两：J({a,b},{a,b})=1、J({a,b},{a})=1/2、J({a,b},{a})=1/2 → 2/3。"""
    value = mean_pairwise_jaccard([["a", "b"], ["a", "b"], ["a"]])

    assert value == pytest.approx(2 / 3)


def test_mean_pairwise_jaccard_needs_two_runs():
    assert mean_pairwise_jaccard([["a"]]) is None
    assert mean_pairwise_jaccard([]) is None


def test_stable_share_is_intersection_over_union():
    """三轮：并集 {a,b}，交集 {a} → 1/2。"""
    assert stable_share([["a", "b"], ["a", "b"], ["a"]]) == pytest.approx(0.5)


def test_stable_share_none_cases():
    assert stable_share([["a"]]) is None            # 轮数不足
    assert stable_share([[], []]) is None           # 并集为空


def test_consistent_share_counts_rows_within_threshold():
    """两行：第一行 std=0 稳定，第二行 std=0.471 不稳定 → 1/2。"""
    scores = [[1.0, 1.0, 1.0], [0.0, 1.0, 1.0]]

    assert consistent_share(scores) == pytest.approx(0.5)
    assert consistent_share(scores, std_threshold=0.5) == pytest.approx(1.0)
    assert consistent_share(scores, std_threshold=0.1) == pytest.approx(0.5)


def test_consistent_share_empty_is_none_not_zero():
    """"没有样本"不能用 0 表示——那会被读成"全都不可复现"。"""
    assert consistent_share([]) is None