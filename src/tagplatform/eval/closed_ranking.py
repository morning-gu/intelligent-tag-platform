"""闭环评测指标装配：系统产出排序 × MovieTags 基准的 θ×K 网格打分（纯标准库）。

为什么需要这个模块
==================

闭环链路里系统只打出它认为值得打的标签（`ClosedSample.ranked`，按系统分
降序），而 MovieTags genome 侧对同一影片有一份 0–1 连续相关度表
（`ClosedSample.relevance`，只覆盖系统产出与基准的交集标签）。离线评测
要回答三个问题，缺一不可：

1. **排得准不准**：P/R/NDCG@k 在（相关门槛 θ × 截断 k）网格上的宏平均。
   θ 的选取是主观的——拍一个 θ 报一组数，结论会随拍法漂移；一次算完整
   网格，让"结论是否随 θ 稳健"本身成为结果的一部分；
2. **分数有没有排序意义**：系统分（`system_scores`）与 gt 分的 Spearman
   秩相关——指标只看"过没过门槛"，Spearman 看的是连续分的一致性；
3. **比随机好多少**：与"不放回均匀抽 k 个"的解析期望（`r/n`、
   `min(k,n)/n`）对比。解析解不采样、完全可复现，逐片算好再宏平均。

口径纪律
========

- **None≠0**：分母为 0（空输入、该 θ 下相关集为空的影片被剔除后无剩余、
  全部影片 Spearman 算不出）时报 None，与"得 0 分"严格区分；
- **无效影片单独报**（spec §7）：`relevance` 为空（影片无交集标签）的
  样本不进任何平均，只计 `n_invalid`；某 θ 下相关集为空的影片不进该 θ
  的格平均（`GridPoint.n_valid` 记实际参与数）；
- **coverage 与 θ 无关**：`len(ranked)/len(relevance)` 衡量系统产出对
  gt 交集的覆盖面，按"有交集"的样本算一次，不做 θ 上的重复计算。

分层红线：本模块只用标准库，平台侧只 import `tagplatform.eval.ranking`
的既有原语（precision_at_k / recall_at_k / ndcg_at_k / macro_average /
bootstrap_ci），不 import fastapi / numpy / scipy——脚本与未来离线工具
可独立复用（与 ranking.py §9.4 同款约束）。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from tagplatform.eval.ranking import (
    bootstrap_ci,
    macro_average,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

__all__ = [
    "DEFAULT_KS",
    "DEFAULT_K_P",
    "DEFAULT_K_R",
    "DEFAULT_THETA_P",
    "DEFAULT_THETA_R",
    "DEFAULT_THETAS",
    "ClosedReport",
    "ClosedSample",
    "GridPoint",
    "evaluate_closed",
    "random_precision_recall",
    "spearman",
]

# ---------------------------------------------------------------- 统一取格约定
# 三处展示位（评测历史 _read_summary、报告汇总卡/基线对照 pickField、单片
# 明细 movie_breakdown 缺省）共用的取格目标（2026-11-22 用户拍板）：
# K=10 类指标（P@10/nDCG@10）取 θ≈0.2 行、R@20 取 θ≈0.8 行；取格规则
# 「最接近目标的有值格、并列取小、无有值格 → None」。前端 ClosedEvalReport
# 以常量镜像本约定（TS 无法 import，值变更须两侧同步）。
DEFAULT_THETA_P: float = 0.2
DEFAULT_THETA_R: float = 0.8
DEFAULT_K_P: int = 10
DEFAULT_K_R: int = 20

# 相关门槛网格：gt 分 ≥θ 才算相关；θ 拍一个数会漂移，整条网格一起看
DEFAULT_THETAS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)
# 截断位置网格（与 ranking.DEFAULT_K_VALUES 风格一致，闭环候选量级更大）
DEFAULT_KS: tuple[int, ...] = (5, 10, 20, 40)


# ---------------------------------------------------------------- Spearman

def _average_ranks(values: Sequence[float]) -> list[float]:
    """并列值取平均秩（1 起）：[1, 1, 2] → [1.5, 1.5, 3.0]。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while (end + 1 < len(order)
               and values[order[end + 1]] == values[order[start]]):
            end += 1
        # 0-based 位次 start..end 对应 1-based 秩 start+1..end+1，并列取均值
        average = (start + end) / 2 + 1
        for index in order[start:end + 1]:
            ranks[index] = average
        start = end + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman 秩相关：两侧各自换平均秩（并列取均值）后在秩上做 Pearson。

    任一侧秩方差为 0（全并列）→ 算不出，返回 None（None≠0）；长度不等
    或为空同样返回 None。值域 [-1, 1]。
    """
    if len(xs) != len(ys) or not xs:
        return None
    rank_x = _average_ranks(xs)
    rank_y = _average_ranks(ys)
    mean_x = sum(rank_x) / len(rank_x)
    mean_y = sum(rank_y) / len(rank_y)
    dx = [v - mean_x for v in rank_x]
    dy = [v - mean_y for v in rank_y]
    ss_x = sum(d * d for d in dx)
    ss_y = sum(d * d for d in dy)
    if ss_x <= 0 or ss_y <= 0:
        return None
    numerator = sum(a * b for a, b in zip(dx, dy))
    return numerator / math.sqrt(ss_x * ss_y)


# ---------------------------------------------------------------- 随机基线

def random_precision_recall(n_candidates: int, n_relevant: int,
                            k: int) -> tuple[float, float]:
    """不放回均匀抽 k 个的解析期望：E[P@k] = r/n，E[R@k] = min(k, n)/n。

    n=0（无交集候选）或 r=0（无相关项）→ (0.0, 0.0)。
    """
    if n_candidates <= 0 or n_relevant <= 0 or k <= 0:
        return (0.0, 0.0)
    precision = n_relevant / n_candidates
    recall = min(k, n_candidates) / n_candidates
    return (precision, recall)


# ---------------------------------------------------------------- 样本与报告

@dataclass(frozen=True)
class ClosedSample:
    """一条闭环评测样本：系统产出排序 + 该影片的 gt 相关度表。

    - `ranked`：系统产出的 tag_id，**按系统分从高到低**；
    - `relevance`：`{tag_id: gt 分}`（0–1 连续），覆盖该影片全部交集
      标签——空表表示影片无交集（不可评，只计 `n_invalid`，不混入任何
      平均）；
    - `system_scores`：系统给每个产出标签的分数（Sf，仅产出标签）；
      交集里缺失的标签在 Spearman 里按 0.0 参与；
    - `label`：仅用于输出的样本标识（影片 id 等）。
    """

    ranked: tuple[str, ...]
    relevance: dict[str, float]
    system_scores: dict[str, float]
    label: str = ""


@dataclass
class GridPoint:
    """网格上的一格：某（θ, k）组合下的宏平均与置信区间。

    - `n_valid`：该 θ 下相关集非空的影片数（实际参与平均的样本数）；
    - `precision/recall/ndcg`：`valid_θ` 上的宏平均；ndcg 用连续增益
      （不按 θ 二值化）；该 θ 下无有效影片时为 None（None≠0）；
    - `*_ci`：逐样本值的百分位自举置信区间（复用 ranking 默认
      n_boot=2000、seed）；无有效影片时为 None；
    - `*_random`：逐片解析随机基线（`random_precision_recall`）的宏平均。
    """

    theta: float
    k: int
    n_valid: int
    precision: float | None
    recall: float | None
    ndcg: float | None
    precision_ci: tuple[float, float] | None
    recall_ci: tuple[float, float] | None
    ndcg_ci: tuple[float, float] | None
    precision_random: float | None
    recall_random: float | None


@dataclass
class ClosedReport:
    """一次闭环评测的完整结果：样本计数 + θ×K 网格 + coverage/Spearman。"""

    n_total: int
    n_valid: int
    n_invalid: int
    grid: list[GridPoint] = field(default_factory=list)
    coverage: list[float] = field(default_factory=list)
    coverage_min: float | None = None
    coverage_median: float | None = None
    coverage_max: float | None = None
    spearman_macro: float | None = None
    warnings: list[str] = field(default_factory=list)


def _n_relevant(sample: ClosedSample, theta: float) -> int:
    """该影片在门槛 θ 下的相关标签数（relevance 值 ≥θ 的条数）。"""
    return sum(1 for v in sample.relevance.values() if v >= theta)


def evaluate_closed(samples: list[ClosedSample],
                    thetas: Sequence[float] = DEFAULT_THETAS,
                    ks: Sequence[int] = DEFAULT_KS) -> ClosedReport:
    """对一批闭环样本算完整报告（θ×K 网格 + coverage + Spearman）。

    - 逐 θ 先筛出相关集非空（存在 gt 分 ≥θ）的影片再进该格平均——该 θ
      下无相关的影片不拉低该格（`GridPoint.n_valid` 记实际参与数）；
    - `relevance` 为空的影片（无交集）不进任何平均，只计 `n_invalid`；
    - 格值为 None 表示该格无有效影片（None≠0）。
    """
    usable = [s for s in samples if s.relevance]
    report = ClosedReport(
        n_total=len(samples),
        n_valid=len(usable),
        n_invalid=len(samples) - len(usable),
    )

    if not samples:
        report.warnings.append("没有样本可评")
    if report.n_invalid:
        invalid_labels = [s.label for s in samples if not s.relevance]
        report.warnings.append(
            f"{report.n_invalid} 条样本无交集标签（不计入任何平均，只计 "
            f"n_invalid）：{', '.join(invalid_labels[:5])}")

    # coverage 与 θ 无关：按"有交集"的样本算一次
    coverage = [len(s.ranked) / len(s.relevance) for s in usable]
    report.coverage = coverage
    if coverage:
        report.coverage_min = min(coverage)
        report.coverage_median = statistics.median(coverage)
        report.coverage_max = max(coverage)

    # Spearman：逐片在交集标签上比系统分与 gt 分；零方差等算不出的片
    # 过滤掉再宏平均——全部算不出就是 None（None≠0）
    spearman_values = []
    for s in usable:
        keys = list(s.relevance)
        value = spearman([s.system_scores.get(t, 0.0) for t in keys],
                         [s.relevance[t] for t in keys])
        if value is not None:
            spearman_values.append(value)
    report.spearman_macro = macro_average(spearman_values)

    grid: list[GridPoint] = []
    for theta in thetas:
        valid_theta = [s for s in usable
                       if any(v >= theta for v in s.relevance.values())]
        if not valid_theta:
            report.warnings.append(f"θ={theta} 下无有效影片（该 θ 各格值为 None）")
        for k in ks:
            precisions = [precision_at_k(s.ranked, s.relevance, k,
                                         relevant_grade=theta)
                          for s in valid_theta]
            recalls = [recall_at_k(s.ranked, s.relevance, k,
                                   relevant_grade=theta)
                       for s in valid_theta]
            ndcgs = [ndcg_at_k(s.ranked, s.relevance, k) for s in valid_theta]
            random_pairs = [
                random_precision_recall(len(s.relevance), _n_relevant(s, theta), k)
                for s in valid_theta
            ]
            grid.append(GridPoint(
                theta=theta,
                k=k,
                n_valid=len(valid_theta),
                precision=macro_average(precisions),
                recall=macro_average(recalls),
                ndcg=macro_average(ndcgs),
                precision_ci=bootstrap_ci(precisions),
                recall_ci=bootstrap_ci(recalls),
                ndcg_ci=bootstrap_ci(ndcgs),
                precision_random=macro_average([p for p, _ in random_pairs]),
                recall_random=macro_average([r for _, r in random_pairs]),
            ))
    report.grid = grid
    return report
