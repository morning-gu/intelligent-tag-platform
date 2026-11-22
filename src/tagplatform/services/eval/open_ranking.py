"""开放评测指标装配：库内命中集合准确率 × 评审通过率/标定的宏平均（纯标准库）。

为什么需要这个模块
==================

开放生成（`OpenTagProducer`）此前没有数字化的质量标尺。开放评测以
MovieTags genome gt 为真值干跑整条管线，回答两个问题：

1. **库内命中排得准不准**：命中集合对 gt 的集合式准确率（θ 网格全报）+
   nDCG（产出顺序）+ 覆盖率，并配随机（r/n 解析解）与先验（全影片 gt
   均值序）两条基线——没有基线对照的准确率没有解读；
2. **评审员可不可信**：新词/命中分别报二值评审通过率，再用 gt≥θ 标定
   评审员（一致率 + 假阳/假阴方向分解）——评审 pass 的语义是质量三条件，
   gt≥θ 的语义是 genome 相关性，一致率衡量两个口径的对齐程度，**不是
   评审员「错误率」**。

口径纪律（spec §5，三条通用纪律原样继承 eval.md §5）
====================================================

- **None≠0**：分母为 0（无有效片、该 θ 下覆盖率无片可算、无新词片等）
  时报 None，与"得 0 分"严格区分；计数类（`no_gt` / `review_missing` /
  `n_*`）如实记 0；
- **无效影片单独报**：交集内命中（`hits_in`）为空的片不进任何 θ 族平均，
  只计 `n_invalid`（评审成功时仍进通过率/新词占比——invalid 只描述准确率
  族不可评）；
- **交集外命中只计数**（spec §5.1）：带 tag_id 但不在 gt 交集内的命中
  只进 `no_gt` 计数与命中通过率，不占准确率/标定的分子分母（不白给
  precision 也不冤枉 recall）；
- **评审失败片契约**（spec §6）：`hit_pass`/`new_pass` 整体 None 的片
  不进任何通过率/标定，准确率照常可算（produce 产出不作废），命中标签
  计入 `review_missing`；该片保留在 `n_ok`；
- **漏判条目**（Review Focus 1）：`hit_pass`/`new_pass` 条目 None = 模型
  漏判，排除出通过率与标定的分子分母，计入 `review_missing`（≠0 单列）；
- **随机基线**：同长度集合从交集均匀抽的期望准确率 = r/n（r=交集内
  gt≥θ 数，n=交集标签数），解析解不采样、逐片宏平均；
- **先验基线**：标签先验均值 = gt **全影片**（按"有分的影片"取均值——
  稀疏 gt 缺行 = 没评过，不是不相关）降序的固定排序（并列取 tag_id 字典
  序），逐片取前 |hits_in| 条的 precision@θ——「不看内容只看先验」能到哪。

样本会计约定
============

- 样本 = produce 成功片（含评审失败片）；produce 失败片结构上无法表达，
  由调用方在报告组装层另行核算（镜像闭环 assemble_report 的 n_failed
  自算模式）——若调用方以「空 hit_ids + 整体 None」传入，会计上计
  `n_failed`（评审失败片有产出必带 hit_ids，两者可区分）；
- `n_ok` = 有命中产出的可评片数（评审失败片在内），
  `n_total = n_ok + n_failed + n_invalid` 分块核算。

分层红线：本模块只用标准库，平台侧只 import `tagplatform.services.eval.
ranking` 的既有原语（macro_average / bootstrap_ci / ndcg_at_k），不
import fastapi / numpy / 其他平台模块——脚本与未来离线工具可独立复用
（与 ranking.py §9.4 同款约束）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from tagplatform.services.eval.ranking import (
    bootstrap_ci,
    macro_average,
    ndcg_at_k,
)

__all__ = [
    "DEFAULT_THETAS",
    "DEFAULT_THETA_HEADLINE",
    "OpenReport",
    "OpenSample",
    "ThetaRow",
    "evaluate_open",
]

# 相关门槛网格：gt 分 ≥θ 才算相关；θ 拍一个数会漂移，整条网格一起看
DEFAULT_THETAS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)
# 三处展示位（历史摘要 _read_open_summary、报告汇总卡、单片明细缺省）共用
# 的取格目标（spec §5.1 头条取格约定前置）。前端以常量镜像本约定（TS 无法
# import，值变更须两侧同步）。
DEFAULT_THETA_HEADLINE: float = 0.4


# ---------------------------------------------------------------- 样本与报告

@dataclass(frozen=True)
class OpenSample:
    """一条开放评测样本：一片的全部命中 + 交集 gt 表 + 评审判定。

    - `hit_ids`：全部命中（含交集外）的 tag_id，**保持产出序**；
    - `hit_in_flags`：与 `hit_ids` 对齐，True = tag_id ∈ 该片交集（库 ∩ gt）；
    - `intersection`：该片交集内 gt 分表 `{tag_id: 0–1 连续分}`——覆盖交集
      全部标签（不止命中的），覆盖率/随机基线的分母要用；
    - `hit_pass`：与 `hit_ids` 对齐的评审判定；条目 None = 模型漏判（不计
      分子分母、计 `review_missing`）；**整体 None = 该片评审失败**；
    - `new_pass`：与该片新词列表对齐的评审判定，None 语义同上。

    produce 失败片可由调用方以「空 hit_ids + 整体 None」传入，会计上计
    `n_failed`（见模块 docstring 样本会计约定）。
    """

    label: str
    hit_ids: tuple[str, ...]
    hit_in_flags: tuple[bool, ...]
    intersection: dict[str, float]
    hit_pass: tuple[bool | None, ...] | None
    new_pass: tuple[bool | None, ...] | None


@dataclass
class ThetaRow:
    """θ 网格上的一行：某门槛下的宏平均与置信区间。

    - `n_valid`：交集内命中非空的可评片数（θ 无关，各行相同）；
    - `accuracy/accuracy_random/accuracy_prior`：同一有效片集上的宏平均，
      该集合为空时为 None（None≠0）；准确率分子随 θ 变、分母（|hits_in|）
      不变——交集内无 gt≥θ 标签的片以合法 0 参与，不剔除；
    - `coverage`：逐片分母 = 交集内 gt≥θ 总数，为 0 的片不进该 θ 格
      （该 θ 下相关集空的片不进该格）；无片可算时为 None；
    - `n_calibrated`：进入该 θ 标定宏平均的影片数（评审成功且有可标定
      命中条目）；
    - `agreement/fp_rate/fn_rate`：标定宏平均；无片可标定时为 None；
    - `*_ci`：百分位自举置信区间（复用 ranking 默认 n_boot/seed）。
    """

    theta: float
    n_valid: int
    accuracy: float | None
    accuracy_ci: tuple[float, float] | None
    accuracy_random: float | None
    accuracy_prior: float | None
    coverage: float | None
    n_calibrated: int
    agreement: float | None
    agreement_ci: tuple[float, float] | None
    fp_rate: float | None
    fn_rate: float | None


@dataclass
class OpenReport:
    """一次开放评测的完整结果：样本核算 + θ 网格 + 通过率/标定 + nDCG。"""

    n_total: int
    n_ok: int
    n_failed: int
    n_review_failed: int
    n_invalid: int
    no_gt: int
    theta_rows: list[ThetaRow] = field(default_factory=list)
    ndcg_macro: float | None = None
    ndcg_ci: tuple[float, float] | None = None
    new_pass_macro: float | None = None
    new_pass_micro: float | None = None
    new_pass_n: int = 0
    hit_pass_macro: float | None = None
    hit_pass_micro: float | None = None
    hit_pass_n: int = 0
    review_missing: int = 0
    new_ratio_macro: float | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 先验均值

def _prior_means(gt: dict[str, dict[str, float]]) -> dict[str, float]:
    """标签先验均值：gt 全影片、按「有分的影片」取均值。

    稀疏 gt 里缺行 = 没评过，不是「不相关」（None≠0 的均值版，与闭环
    report._prior_ranking 同款口径）；排序时并列再取 tag_id 字典序。
    """
    values: dict[str, list[float]] = {}
    for scores in gt.values():
        for tag, value in scores.items():
            values.setdefault(tag, []).append(value)
    return {tag: sum(vals) / len(vals) for tag, vals in values.items()}


def _verdict(passes: tuple[bool | None, ...] | None, index: int) -> bool | None:
    """取对齐位置的评审判定；整体 None 或条目缺位（长度不足）按漏判处理。"""
    if passes is None:
        return None
    return passes[index] if index < len(passes) else None


# ---------------------------------------------------------------- 主装配

def evaluate_open(samples: list[OpenSample],
                  gt: dict[str, dict[str, float]],
                  thetas: Sequence[float] = DEFAULT_THETAS) -> OpenReport:
    """对一批开放评测样本算完整报告（样本核算 + θ 网格 + 通过率/标定）。

    先算与 θ 无关的量（样本核算/通过率/nDCG/新词占比），再逐 θ 装配
    `ThetaRow`。口径细则见模块 docstring。
    """
    report = OpenReport(
        n_total=len(samples),
        n_ok=0, n_failed=0, n_review_failed=0, n_invalid=0, no_gt=0,
    )
    if not samples:
        report.warnings.append("没有样本可评")
        report.theta_rows = [
            ThetaRow(theta=theta, n_valid=0, accuracy=None, accuracy_ci=None,
                     accuracy_random=None, accuracy_prior=None, coverage=None,
                     n_calibrated=0, agreement=None, agreement_ci=None,
                     fp_rate=None, fn_rate=None)
            for theta in thetas
        ]
        return report

    means = _prior_means(gt)

    # ---- 逐片预处理：样本核算 + θ 无关量 -------------------------------
    valid: list[dict] = []            # 可评片（hits_in 非空）：θ 族参与
    invalid_labels: list[str] = []
    review_failed_labels: list[str] = []
    ndcg_values: list[float] = []
    new_rate_values: list[float] = []
    hit_rate_values: list[float] = []
    new_pass_sum = new_rate_den = 0   # 微平均池（pass 数 / 非 None 数）
    hit_pass_sum = hit_rate_den = 0
    new_ratio_values: list[float] = []
    review_missing = 0

    for s in samples:
        hit_in_indexed = [(i, tag) for i, (tag, flag)
                          in enumerate(zip(s.hit_ids, s.hit_in_flags)) if flag]
        hit_in = tuple(tag for _, tag in hit_in_indexed)
        report.no_gt += len(s.hit_ids) - len(hit_in)

        whole_hit_none = s.hit_pass is None
        whole_new_none = s.new_pass is None
        # 会计分块：空产出 + 评审缺席 = produce 失败片（调用方若传入）——
        # 该片评审是「跳过」而非「失败」，不进 n_review_failed
        if not s.hit_ids and whole_hit_none and whole_new_none:
            report.n_failed += 1
            continue
        review_failed = whole_hit_none or whole_new_none
        if review_failed:
            report.n_review_failed += 1
            review_failed_labels.append(s.label)
        # 漏判/缺席标签计数：整片失败按命中标签数计（新词数整体 None 时
        # 不可知，结构性局限）；评审成功按条目 None 计，缺位尾项同漏判
        if whole_hit_none:
            review_missing += len(s.hit_ids)
        else:
            review_missing += (sum(1 for v in s.hit_pass if v is None)  # type: ignore[union-attr]
                               + max(0, len(s.hit_ids) - len(s.hit_pass)))  # type: ignore[union-attr]
        if not whole_new_none:
            review_missing += sum(1 for v in s.new_pass if v is None)  # type: ignore[union-attr]

        if not hit_in:
            report.n_invalid += 1
            invalid_labels.append(s.label)
        else:
            report.n_ok += 1

        # θ 族原料（θ 无关部分）：gt 增益、nDCG、先验序、标定条目
        film: dict = {
            "hit_in": hit_in,
            "intersection": s.intersection,
            "review_ok": not review_failed,
        }
        if hit_in:
            gains = [s.intersection.get(tag, 0.0) for tag in hit_in]
            film["gains"] = gains
            # nDCG：产出序、连续增益、k=len(hit_in) 全列表不截断
            ndcg_values.append(ndcg_at_k(hit_in, dict(zip(hit_in, gains)),
                                         len(hit_in)))
            film["prior_top"] = sorted(
                s.intersection,
                key=lambda tag: (-means.get(tag, 0.0), tag))[:len(hit_in)]
            # 标定条目：仅交集内命中、漏判条目排除——(判定, gt) 对，θ 上复用
            film["calib_entries"] = [
                (verdict, gain)
                for (idx, tag), gain in zip(hit_in_indexed, gains)
                if (verdict := _verdict(s.hit_pass, idx)) is not None
            ]
            valid.append(film)

        # 通过率原料：评审成功片才有分母；漏判条目排除出分子分母
        if not review_failed:
            hit_entries = [_verdict(s.hit_pass, i)
                           for i in range(len(s.hit_ids))]
            n_hit = sum(1 for v in hit_entries if v is not None)
            if n_hit:
                n_pass = sum(1 for v in hit_entries if v is True)
                hit_rate_values.append(n_pass / n_hit)
                hit_pass_sum += n_pass
                hit_rate_den += n_hit
            new_entries = list(s.new_pass)  # type: ignore[arg-type]
            n_new = sum(1 for v in new_entries if v is not None)
            if n_new:
                n_pass_new = sum(1 for v in new_entries if v is True)
                new_rate_values.append(n_pass_new / n_new)
                new_pass_sum += n_pass_new
                new_rate_den += n_new
            # 新词占比：0 总产出片排除（评审成功时新词数必然可知）
            total_out = len(s.hit_ids) + len(new_entries)
            if total_out:
                new_ratio_values.append(len(new_entries) / total_out)

    if report.n_review_failed:
        report.warnings.append(
            f"{report.n_review_failed} 部影片评审失败（不进通过率/标定，"
            f"评审标签计入 review_missing）："
            f"{', '.join(review_failed_labels[:5])}")
    if report.n_invalid:
        report.warnings.append(
            f"{report.n_invalid} 条样本交集内命中为空（不进 θ 族平均，只计 "
            f"n_invalid）：{', '.join(invalid_labels[:5])}")
    if not valid:
        report.warnings.append("无有效影片（交集内命中全空），θ 族各格值为 None")

    # ---- θ 无关聚合 ----------------------------------------------------
    report.ndcg_macro = macro_average(ndcg_values)
    report.ndcg_ci = bootstrap_ci(ndcg_values)
    report.new_pass_macro = macro_average(new_rate_values)
    report.new_pass_micro = (new_pass_sum / new_rate_den
                             if new_rate_den else None)
    report.new_pass_n = len(new_rate_values)
    report.hit_pass_macro = macro_average(hit_rate_values)
    report.hit_pass_micro = (hit_pass_sum / hit_rate_den
                             if hit_rate_den else None)
    report.hit_pass_n = len(hit_rate_values)
    report.review_missing = review_missing
    report.new_ratio_macro = macro_average(new_ratio_values)

    # ---- 逐 θ 装配 ThetaRow ---------------------------------------------
    theta_rows: list[ThetaRow] = []
    for theta in thetas:
        accuracies: list[float] = []
        randoms: list[float] = []
        priors: list[float] = []
        coverages: list[float] = []
        agreements: list[float] = []
        fps: list[float] = []
        fns: list[float] = []
        n_calibrated = 0
        for film in valid:
            intersection = film["intersection"]
            gains = film["gains"]
            n_hit = len(film["hit_in"])
            # 准确率@θ = |hit_in 中 gt≥θ| / |hit_in|（合法 0 照进）
            accuracies.append(sum(1 for g in gains if g >= theta) / n_hit)
            # 交集内相关总数：随机基线的 r（分子）、覆盖率分母共用
            n_relevant = sum(1 for v in intersection.values() if v >= theta)
            # 随机基线 r/n（n = 交集标签数；r=0 是解析解的合法 0）
            randoms.append(n_relevant / len(intersection))
            # 先验基线：先验序前 |hit_in| 条的 precision@θ（分母同准确率）
            priors.append(sum(1 for tag in film["prior_top"]
                              if intersection[tag] >= theta) / n_hit)
            # 覆盖率@θ：分母 = 交集内 gt≥θ 总数，为 0 的片不进该格
            if n_relevant:
                coverages.append(
                    sum(1 for g in gains if g >= theta) / n_relevant)
            # 标定：仅评审成功、有可标定条目的片（漏判条目已在原料排除）
            entries = film["calib_entries"]
            if film["review_ok"] and entries:
                n_calibrated += 1
                n = len(entries)
                tp = sum(1 for verdict, g in entries
                         if verdict and g >= theta)
                fp = sum(1 for verdict, g in entries
                         if verdict and g < theta)
                fn = sum(1 for verdict, g in entries
                         if not verdict and g >= theta)
                tn = n - tp - fp - fn
                agreements.append((tp + tn) / n)
                fps.append(fp / n)
                fns.append(fn / n)
        if valid and not coverages:
            report.warnings.append(
                f"θ={theta} 下无影片可算覆盖率（各片交集内无 gt≥θ 标签）")
        theta_rows.append(ThetaRow(
            theta=theta,
            n_valid=len(valid),
            accuracy=macro_average(accuracies),
            accuracy_ci=bootstrap_ci(accuracies),
            accuracy_random=macro_average(randoms),
            accuracy_prior=macro_average(priors),
            coverage=macro_average(coverages),
            n_calibrated=n_calibrated,
            agreement=macro_average(agreements),
            agreement_ci=bootstrap_ci(agreements),
            fp_rate=macro_average(fps),
            fn_rate=macro_average(fns),
        ))
    report.theta_rows = theta_rows
    return report
