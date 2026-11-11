"""排序指标：目录归属（新词 → 目录）的离线评测口径（纯函数，零依赖）。

为什么需要这个模块
==================

`library.rank_dir_assignments` 本来就产出一个按目录描述相似度降序的候选
列表（`top_n=5`），但落地链路只取第 1 条（`assign_dir`），于是"排得准不准"
除了翻人工审核记录以外没有任何可观测面。本模块把"排得准不准"换算成
可回归的数字，供 `scripts/eval_dir_assignment.py` 调用。

指标选择与退化情形
==================

单相关项（一条样本通常只有一个"正确目录"）时几个指标会**互相退化**，
这里如实计算、不做等价性特殊处理，只在文档里写清楚：

- `Recall@k == HitRate@k`：相关集大小为 1，命中即全召回；
- `Precision@k == HitRate@k / k`：只有 1 个相关项；
- `NDCG@k == 1/log2(rank+1)`：理想排序只有一项，折扣因子单调。

因此对外汇报**以 NDCG@3/@5 + HitRate@k + MRR 为主**，Precision@k 只在
"分级相关"（`rel=2` 精确目录 / `rel=1` 同父兄弟目录）下才有区分度——
本模块的相关度用 `Mapping[str, float]` 表达，天然支持分级。

k 的取值约束
============

候选列表只有 `top_n=5` 条，`k > 5` 的指标没有意义（恒等于 k=5）。
`evaluate_samples` 不强制，但会在 `warnings` 里点出来。

分层红线：本模块只用标准库，不 import fastapi / numpy / 平台模块
（设计文档 §9.4）。
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_K_VALUES",
    "EvaluationReport",
    "RankingSample",
    "bootstrap_ci",
    "evaluate_samples",
    "hit_rate_at_k",
    "lift",
    "macro_average",
    "mrr",
    "ndcg_at_k",
    "precision_at_k",
    "random_baseline",
    "recall_at_k",
]

# 与 `rank_dir_assignments` 的 top_n 对齐（k 超过它就没有新信息）
DEFAULT_K_VALUES = (3, 5)

# 相关度档位（分级）：精确等同目录 / 同父兄弟目录 / 其他
GRADE_EXACT = 2.0
GRADE_SIBLING = 1.0
GRADE_NONE = 0.0

# Recall/HitRate/Precision 认定"相关"的最低档（>= 该值算相关）
DEFAULT_RELEVANT_GRADE = GRADE_SIBLING


@dataclass(frozen=True)
class RankingSample:
    """一条评测样本：一个排序候选列表 + 该样本的相关度表。

    - `ranked`：模型/回退给出的目录 id，**按置信度从高到低**（缺失目录用
      不在相关度表里的占位 id 也能算，会自然得 0 分）；
    - `relevance`：`{dir_id: 相关度档位}`，档位见 `GRADE_*`；空表表示该样本
      没有任何正确目录（脚本应把它计进"不可归属"而不是混入平均）；
    - `label`：仅用于输出的样本标识（标签名等）。
    """

    ranked: tuple[str, ...]
    relevance: dict[str, float]
    label: str = ""


# ---------------------------------------------------------------- 单点指标

def _relevant_ids(relevance: dict[str, float],
                  relevant_grade: float) -> set[str]:
    return {k for k, grade in relevance.items() if grade >= relevant_grade}


def hit_rate_at_k(ranked: tuple[str, ...], relevance: dict[str, float],
                  k: int, *, relevant_grade: float = DEFAULT_RELEVANT_GRADE) -> float:
    """前 k 条里是否至少命中一个相关目录（1.0 / 0.0）。"""
    if k <= 0:
        return 0.0
    relevant = _relevant_ids(relevance, relevant_grade)
    if not relevant:
        return 0.0
    return 1.0 if any(item in relevant for item in ranked[:k]) else 0.0


def recall_at_k(ranked: tuple[str, ...], relevance: dict[str, float],
                k: int, *, relevant_grade: float = DEFAULT_RELEVANT_GRADE) -> float:
    """前 k 条覆盖了多少相关目录（单相关项时与 HitRate 完全等价）。"""
    if k <= 0:
        return 0.0
    relevant = _relevant_ids(relevance, relevant_grade)
    if not relevant:
        logger.debug("recall@%d：相关度为空（或全在门槛以下），记 0", k)
        return 0.0
    found = sum(1 for item in ranked[:k] if item in relevant)
    value = found / len(relevant)
    logger.debug("recall@%d = %.3f：命中 %d/%d 个相关目录，top%d=%s",
                 k, value, found, len(relevant), k, ranked[:k])
    return value


def precision_at_k(ranked: tuple[str, ...], relevance: dict[str, float],
                   k: int, *, relevant_grade: float = DEFAULT_RELEVANT_GRADE) -> float:
    """前 k 条里相关目录的占比（单相关项时 = HitRate / k）。

    分母固定为 k：候选列表短于 k 时，缺的位置算作"没命中"，这样不同样本
    的 P@k 才可比（与 NDCG 的 k 处截断口径一致）。
    """
    if k <= 0:
        return 0.0
    relevant = _relevant_ids(relevance, relevant_grade)
    if not relevant:
        logger.debug("precision@%d：相关度为空（或全在门槛以下），记 0", k)
        return 0.0
    found = sum(1 for item in ranked[:k] if item in relevant)
    value = found / k
    logger.debug("precision@%d = %.3f：top%d 里相关 %d 条，top%d=%s",
                 k, value, k, found, k, ranked[:k])
    return value


def _dcg(grades: list[float]) -> float:
    """折损累积增益：DCG@k = Σ rel_i / log2(i+1)，i 从 1 起。"""
    total = 0.0
    for index, grade in enumerate(grades):
        total += grade / math.log2(index + 2)  # index 0-based → 排名 index+1
    return total


def ndcg_at_k(ranked: tuple[str, ...], relevance: dict[str, float], k: int) -> float:
    """归一化折损累积增益（k 处截断）。

    相关度表为空（该样本没有任何正确目录）时返回 0.0——这类样本应当由
    调用方计进"不可归属率"，而不是混进平均值。
    """
    if k <= 0 or not relevance:
        logger.debug("ndcg@%d：相关度为空，记 0（该样本应计入不可归属率）", k)
        return 0.0
    ideal_grades = sorted(relevance.values(), reverse=True)[:k]
    ideal = _dcg(ideal_grades)
    if ideal <= 0:
        logger.debug("ndcg@%d：理想 DCG 非正（相关度全 0），记 0", k)
        return 0.0
    actual = _dcg([relevance.get(item, GRADE_NONE) for item in ranked[:k]])
    value = actual / ideal
    logger.debug("ndcg@%d = %.3f：DCG=%.3f / 理想=%.3f，top%d=%s",
                 k, value, actual, ideal, k, ranked[:k])
    return value


def mrr(ranked: tuple[str, ...], relevance: dict[str, float],
        *, relevant_grade: float = DEFAULT_RELEVANT_GRADE) -> float:
    """平均倒数排名：第一个相关目录的名次取倒数，未命中为 0。"""
    relevant = _relevant_ids(relevance, relevant_grade)
    if not relevant:
        return 0.0
    for index, item in enumerate(ranked):
        if item in relevant:
            return 1.0 / (index + 1)
    return 0.0


# ---------------------------------------------------------------- 聚合

def macro_average(values) -> float | None:
    """宏平均：先按样本算分，再对样本平均（每样本权重相同）。

    空序列返回 None（对应 `metrics._ratio` 的"分母为 0 → 暂无"口径，
    与 0.0 区分开）。
    """
    items = list(values)
    if not items:
        return None
    return sum(items) / len(items)


def lift(score: float | None, baseline: float | None) -> float | None:
    """相对随机基线的提升倍数（score / baseline）；基线为 0/None 时 None。"""
    if score is None or baseline is None or baseline <= 0:
        return None
    return score / baseline


def random_baseline(n_candidates: int, relevance: dict[str, float], k: int,
                    *, relevant_grade: float = DEFAULT_RELEVANT_GRADE) -> dict[str, float]:
    """均匀随机排序下的期望指标（解析解，不采样）。

    - 期望 HitRate@k = 1 - C(n - r, k) / C(n, k)（r = 相关目录数）；
    - 期望 NDCG@k：每一位的期望增益都是"相关度全表均值"，再按折损加权。
      因此 `relevance` 需要**覆盖全部候选**（不相关的填 `GRADE_NONE`），
      否则均值算错；
    - 期望 MRR：按"前 i 名都不相关"的概率累加（不放回抽样的精确式）。
    """
    if n_candidates <= 0 or not relevance:
        return {"hit_rate": 0.0, "ndcg": 0.0, "mrr": 0.0}
    r = len(_relevant_ids(relevance, relevant_grade))
    effective_k = min(k, n_candidates)
    if r <= 0:
        hit = 0.0
    else:
        miss = 1.0
        for i in range(effective_k):
            if n_candidates - r - i <= 0:
                miss = 0.0
                break
            miss *= (n_candidates - r - i) / (n_candidates - i)
        hit = 1.0 - miss
    ideal = _dcg(sorted(relevance.values(), reverse=True)[:k])
    if ideal <= 0:
        ndcg = 0.0
    else:
        mean_grade = sum(relevance.values()) / n_candidates
        expected_dcg = _dcg([mean_grade] * effective_k)
        ndcg = expected_dcg / ideal
    # 期望 MRR：Σ P(前 i 名全不相关) × P(第 i+1 名相关 | 前 i 名全不相关) / (i+1)
    mrr_value = 0.0
    prior = 1.0
    for i in range(effective_k):
        remaining = n_candidates - i
        if remaining <= 0 or r <= 0:
            break
        mrr_value += prior * (r / remaining) / (i + 1)
        prior *= (remaining - r) / remaining
    return {"hit_rate": hit, "ndcg": ndcg, "mrr": mrr_value}


# ---------------------------------------------------------------- 报告

@dataclass
class EvaluationReport:
    """一次评测的完整结果：逐 k 的宏平均 + 每样本原值（供自举置信区间）。"""

    n_samples: int
    k_values: tuple[int, ...]
    metrics: dict[int, dict[str, float | None]] = field(default_factory=dict)
    per_sample: dict[str, list[float]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def metric(self, name: str, k: int) -> float | None:
        return self.metrics.get(k, {}).get(name)

    def raw(self, name: str, k: int) -> list[float]:
        return self.per_sample.get(f"{name}@{k}", [])


def evaluate_samples(samples: list[RankingSample],
                     k_values: tuple[int, ...] = DEFAULT_K_VALUES,
                     *, relevant_grade: float = DEFAULT_RELEVANT_GRADE,
                     max_ranked: int = 5) -> EvaluationReport:
    """对一批样本算全部指标（宏平均），并保留每样本原值。

    `max_ranked` 用来提示"k 超过候选列表长度"——超过之后指标不再变化，
    报出来的数字会误导人，所以在 `warnings` 里点明。
    """
    ks = tuple(sorted({k for k in k_values if k > 0})) or DEFAULT_K_VALUES
    report = EvaluationReport(n_samples=len(samples), k_values=ks)
    if not samples:
        report.warnings.append("没有样本可评")
        return report
    if max(ks) > max_ranked:
        report.warnings.append(
            f"k={max(ks)} 超过候选列表长度 {max_ranked}，该 k 的指标与 "
            f"k={max_ranked} 等价（数字会被虚高）")

    empty_labels = [s.label for s in samples if not s.relevance]
    if empty_labels:
        report.warnings.append(
            f"{len(empty_labels)} 条样本没有正确目录（应计入不可归属率，"
            f"不参与平均）：{', '.join(empty_labels[:5])}")

    usable = [s for s in samples if s.relevance]
    buckets: dict[str, list[float]] = {}
    for sample in usable:
        for k in ks:
            buckets.setdefault(f"hit_rate@{k}", []).append(
                hit_rate_at_k(sample.ranked, sample.relevance, k,
                              relevant_grade=relevant_grade))
            buckets.setdefault(f"recall@{k}", []).append(
                recall_at_k(sample.ranked, sample.relevance, k,
                            relevant_grade=relevant_grade))
            buckets.setdefault(f"precision@{k}", []).append(
                precision_at_k(sample.ranked, sample.relevance, k,
                               relevant_grade=relevant_grade))
            buckets.setdefault(f"ndcg@{k}", []).append(
                ndcg_at_k(sample.ranked, sample.relevance, k))
        buckets.setdefault("mrr", []).append(
            mrr(sample.ranked, sample.relevance, relevant_grade=relevant_grade))

    report.per_sample = buckets
    macro_mrr = macro_average(buckets.get("mrr", []))
    for k in ks:
        report.metrics[k] = {
            name: macro_average(buckets.get(f"{name}@{k}", []))
            for name in ("hit_rate", "recall", "precision", "ndcg")
        }
        # MRR 与 k 无关，但随 k 一起给出，调用方取用更顺手
        report.metrics[k]["mrr"] = macro_mrr
    return report


# ---------------------------------------------------------------- 置信区间

def bootstrap_ci(values, *, n_boot: int = 2000, alpha: float = 0.05,
                 seed: int = 20260101) -> tuple[float, float] | None:
    """样本均值的百分位自举置信区间（默认 95%）。

    只有几十条样本时，报一个点估计没有说服力——区间与随机基线是否重叠，
    才是"这套排序器到底有没有用"的判据。空序列返回 None。
    """
    data = [v for v in values if v is not None]
    if not data:
        return None
    if len(data) == 1:
        return (data[0], data[0])
    rng = random.Random(seed)
    n = len(data)
    means = []
    for _ in range(max(1, n_boot)):
        total = 0.0
        for _ in range(n):
            total += data[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    low_index = int((alpha / 2) * len(means))
    high_index = min(len(means) - 1, int((1 - alpha / 2) * len(means)) - 1)
    return (means[max(0, low_index)], means[high_index])