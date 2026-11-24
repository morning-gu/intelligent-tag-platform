"""评测域（四层重构 Task 10 归位）：两族评测 + 只读质量指标。

- **归属评测**：把"目录归属排得准不准"变成可回归的数字——`ranking`
  （纯 stdlib 指标原语）、`ablation`（模态消融变体）、`consistency`
  （跨轮一致性）；
- **闭环评测**：`closed_ranking`（θ×K 网格指标装配）、`dataset`
  （数据集发现/加载/选片）、`service`（ClosedEvalService 干跑运行器，
  守护线程 + run 生命周期）、`report`（报告组装/渲染/三件套落盘）、
  `movie_breakdown`（单片 badcase 明细）；
- **质量指标**：`metrics`（审核与生成数据的只读聚合）、`tag_quality`
  （三维评分五档汇总指标，开放评测报告复用）。

分层红线：本包不 import fastapi（设计文档 §9.4）；指标原语（ranking /
closed_ranking）零平台依赖、纯标准库，脚本与测试都可独立复用。
"""

from tagplatform.services.eval.ablation import (
    ABLATION_VARIANTS,
    MODALITIES,
    Variant,
    VariantResult,
    compare,
    paired_bootstrap,
    variant_by_name,
)
from tagplatform.services.eval.consistency import (
    consistent_share,
    consistency_std,
    icc_2_1,
    jaccard,
    mean_pairwise_jaccard,
    presence_matrix,
    stable_share,
)
from tagplatform.services.eval.ranking import (
    DEFAULT_K_VALUES,
    GRADE_EXACT,
    GRADE_NONE,
    GRADE_SIBLING,
    EvaluationReport,
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

__all__ = [
    "ABLATION_VARIANTS",
    "DEFAULT_K_VALUES",
    "GRADE_EXACT",
    "GRADE_NONE",
    "GRADE_SIBLING",
    "MODALITIES",
    "EvaluationReport",
    "RankingSample",
    "Variant",
    "VariantResult",
    "bootstrap_ci",
    "compare",
    "consistent_share",
    "consistency_std",
    "evaluate_samples",
    "hit_rate_at_k",
    "icc_2_1",
    "jaccard",
    "lift",
    "macro_average",
    "mean_pairwise_jaccard",
    "mrr",
    "ndcg_at_k",
    "paired_bootstrap",
    "precision_at_k",
    "presence_matrix",
    "random_baseline",
    "recall_at_k",
    "stable_share",
    "variant_by_name",
]