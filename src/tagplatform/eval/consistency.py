"""生成一致性：同一份输入跑多次，结果到底稳不稳（纯函数，零依赖）。

为什么需要这个模块
==================

开放式生成是多轮采样：`mtg_runs=3` 意味着同一部片、同一份提示词，模型会
被问三次。三次给出的标签集合如果差得很远，那"最终标签"很大程度上取决于
运气——审核员看到的好结果可能只是这一遍恰好抽到了。把"稳不稳"变成数字，
才能回答两个具体问题：

1. 这批任务的标签是不是**可复现**的？（一致性低 ⇒ 提高轮数/降温才能收敛）
2. 换了模型、改了提示词之后，一致性是**变好还是变坏**？（回归判据）

指标口径（与 genometag-mllm 的 `genometag/eval/metrics.py` 对齐）
================================================================

- `consistency_std`：`(n_items, n_runs)` → 每行标准差，**总体标准差**
  （`ddof=0`，与 numpy `std(axis=1)` 同口径）。逐条标签看"这条标签各轮
  之间的分数抖动有多大"。
- `icc_2_1`：ICC(2,1)——双向随机效应模型、**单次测量**的组内相关系数。
  这是"单个评分者信度"：问"如果只跑一轮，这个分数有多可信"。ICC 越高，
  多轮均值越不代表某一轮的实际情况，但单轮作为估计越好用。
  公式与本模块实现见 `icc_2_1` 的 docstring。
- `mean_pairwise_jaccard` / `stable_share`：标签是**离散集合**，不适合直接
  套 ICC（0/1 矩阵的方差结构会让 ICC 虚高）。这两个是集合口径的补充读法。

一处**有意的口径偏离**
======================

上游在 ICC 分母为 0（全表同值、只剩一组有效变异）时返回 `0.0`，把"算不出"
记成"完全不一致"。本实现返回 `None`：分母为 0 说明样本没有可分离的变异，
不是坏结论，硬记 0.0 会让人误以为模型崩了。调用方必须显式处理 `None`。

分层红线：本模块只用标准库，不 import fastapi / numpy / 平台模块
（设计文档 §9.4）。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

__all__ = [
    "consistent_share",
    "consistency_std",
    "icc_2_1",
    "jaccard",
    "mean_pairwise_jaccard",
    "presence_matrix",
    "stable_share",
]


def _as_matrix(scores: Iterable[Iterable[float]]) -> list[list[float]]:
    """归一化成 `n_items × n_runs` 的浮点矩阵；行宽不一致直接报错。"""
    rows = [[float(value) for value in row] for row in scores]
    if not rows:
        return []
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValueError(
            "一致性矩阵每行长度必须一致（形状为 n_items × n_runs）："
            f"首行 {width} 列，实际出现 {sorted({len(r) for r in rows})}")
    return rows


# ---------------------------------------------------------------- 逐行离散度

def consistency_std(scores: Iterable[Iterable[float]]) -> list[float]:
    """`(n_items, n_runs)` → 每项跨轮次的**总体**标准差（ddof=0）。

    与 numpy `std(axis=1)` 同口径（上游用的是 numpy 默认 ddof=0）。用总体
    而不是样本标准差，是因为这里求的不是"对更大总体的推断"，而是**这一次
    实际跑出来的这几轮**离散了多少。3 轮时两者差 √(3/2)≈1.22 倍，口径必须
    先说清楚，否则数字对不上。

    空行返回 0.0（只有一轮时抖动必为 0）。
    """
    out: list[float] = []
    for row in _as_matrix(scores):
        if not row:
            out.append(0.0)
            continue
        mean = sum(row) / len(row)
        variance = sum((value - mean) ** 2 for value in row) / len(row)
        out.append(math.sqrt(variance))
    return out


# ---------------------------------------------------------------- ICC(2,1)

def icc_2_1(scores: Iterable[Iterable[float]]) -> float | None:
    """ICC(2,1)：双向随机效应、单次测量的组内相关系数。

    记号：`n` = 受试（行，一条标签/一部片），`k` = 评分者（列，第几轮生成）。

        MSR = k·Σ(行均值 - 总均值)² / (n-1)        # 受试间均方
        MSC = n·Σ(列均值 - 总均值)² / (k-1)        # 评分者间均方
        SSE = Σ(x - 总均值)² - k·Σ(行均值-总均值)² - n·Σ(列均值-总均值)²
        MSE = SSE / ((n-1)(k-1))                   # 残差均方
        ICC = (MSR - MSE) / (MSR + (k-1)·MSE + k·(MSC - MSE)/n)

    分子是"受试间真实差异"，分母额外减去"评分者系统性偏移"与"残差"，所以
    某一轮普遍打高分不会把 ICC 抬高。单次测量（不是平均测量）意味着它可以
    直接读作"只跑一轮的可信度"。

    `n < 2` 或 `k < 2` 无法计算，返回 `None`；分母为 0 也返回 `None`（见模块
    docstring 的"有意的口径偏离"）。
    """
    rows = _as_matrix(scores)
    n = len(rows)
    k = len(rows[0]) if rows else 0
    if n < 2 or k < 2:
        return None

    grand = sum(sum(row) for row in rows) / (n * k)
    row_means = [sum(row) / k for row in rows]
    col_means = [sum(row[col] for row in rows) / n for col in range(k)]

    ss_row = k * sum((mean - grand) ** 2 for mean in row_means)
    ss_col = n * sum((mean - grand) ** 2 for mean in col_means)
    ss_total = sum((value - grand) ** 2 for row in rows for value in row)
    ss_err = ss_total - ss_row - ss_col
    # 浮点累加可能给出 -1e-17 这种"负的平方和"，只在量级上是噪声时才夹零。
    if ss_err < 0 and ss_err > -1e-9:
        ss_err = 0.0

    msr = ss_row / (n - 1)
    msc = ss_col / (k - 1)
    mse = ss_err / ((n - 1) * (k - 1))
    denom = msr + (k - 1) * mse + k * (msc - mse) / n
    if denom == 0:
        return None
    return (msr - mse) / denom


# ---------------------------------------------------------------- 集合口径

def presence_matrix(
    run_tag_sets: Sequence[Iterable[str]],
) -> tuple[list[str], list[list[float]]]:
    """把"每轮产出的标签集合"转成 0/1 矩阵（行 = 标签并集，列 = 轮次）。

    返回 `(items, matrix)`；`items` 按字典序排列，保证多次运行输出可逐行
    比对。先转成集合再取并集，所以同一轮内重复的标签不会把权重放大。
    """
    sets = [frozenset(tags) for tags in run_tag_sets]
    union: set[str] = set()
    for tags in sets:
        union |= set(tags)
    items = sorted(union)
    matrix = [[1.0 if item in tags else 0.0 for tags in sets] for item in items]
    return items, matrix


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """两个标签集合的 Jaccard 重合度；两边都为空时返回 0.0。"""
    a, b = frozenset(left), frozenset(right)
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def mean_pairwise_jaccard(run_tag_sets: Sequence[Iterable[str]]) -> float | None:
    """多轮之间两两 Jaccard 的均值：最直观的"轮与轮有多像"。

    轮数 < 2 返回 `None`（没有对子可算）。注意它是**成对**平均，不是
    "并集里有多少被共同覆盖"，但两者在大样本下趋势一致，这里选前者是因为
    它对"某一轮特别短"不敏感。
    """
    sets = [frozenset(tags) for tags in run_tag_sets]
    if len(sets) < 2:
        return None
    total = 0.0
    pairs = 0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            total += jaccard(sets[i], sets[j])
            pairs += 1
    return total / pairs if pairs else None


def stable_share(run_tag_sets: Sequence[Iterable[str]]) -> float | None:
    """标签并集里"每一轮都出现过"的比例（稳定产出率）。

    这是审核员最关心的一条：稳定的标签才值得沉淀成库内词条，只出现一轮的
    更像是这一次采样的偶然产物。轮数 < 2 或无任何标签时返回 `None`。
    """
    sets = [frozenset(tags) for tags in run_tag_sets]
    if len(sets) < 2:
        return None
    union: set[str] = set()
    for tags in sets:
        union |= set(tags)
    if not union:
        return None
    common = set(sets[0])
    for tags in sets[1:]:
        common &= set(tags)
    return len(common) / len(union)


def consistent_share(scores: Iterable[Iterable[float]],
                     *, std_threshold: float = 0.0) -> float | None:
    """跨轮标准差 ≤ `std_threshold` 的项占比（逐条标签的"稳定率"）。

    默认阈值为 0：只统计"各轮完全同值"的项。对 0/1 矩阵即"要么每轮都有、
    要么每轮都没有"，与 `stable_share` 口径相通但维度不同（前者按行、后者
    按集合），两者一起看能区分"标签整体抖动"与"个别标签抖动"。
    """
    stds = consistency_std(scores)
    if not stds:
        return None
    return sum(1 for value in stds if value <= std_threshold) / len(stds)