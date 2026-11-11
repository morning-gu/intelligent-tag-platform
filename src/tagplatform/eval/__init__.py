"""标签归属评测包：把"目录归属排得准不准"变成可回归的数字。

分层红线：本包不 import fastapi（设计文档 §9.4），也不 import 任何平台
业务模块——`ranking` 是纯 stdlib 指标函数，脚本与测试都可独立复用。
"""

from tagplatform.eval.ablation import (
    ABLATION_VARIANTS,
    MODALITIES,
    Variant,
    VariantResult,
    compare,
    paired_bootstrap,
    variant_by_name,
)
from tagplatform.eval.consistency import (
    consistent_share,
    consistency_std,
    icc_2_1,
    jaccard,
    mean_pairwise_jaccard,
    presence_matrix,
    stable_share,
)
from tagplatform.eval.ranking import (
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