"""闭环评测报告层：样本构造 → 网格打分 → report.json / report.md / metrics.csv 三件套。

报告组装（Task 5；closed_eval 三分后本层负责组装 + 渲染 + 写）
==============================================================

`assemble_report` 把 ok 片构造成 ClosedSample 喂 `evaluate_closed`：
`ranked` = 产出行限「库∩gt」交集、防御性再排 (-sf, tag_id)；交集外
产出条目剔除、只计 `sample_accounting.no_gt`（spec §5 保护原则——白给
precision / 冤枉 recall 都不要）；先验基线（tag_universe 按 gt 全影片
均值降序、并列 tag_id 字典序）用同一机制并排给出，格值才可比。部分
口径响亮：status != succeeded 或 done < total → banner 明写「部分运行」；
失败率 > 10% → 小样本警示；样本核算（total/ok/failed/invalid/no_gt）
永远随报告落盘。

写盘纪律：报告层只读写文件，DB 零写入；`write_report` 三件套写序在
meta/raw 之后（worker 终态序），读侧（service.history）对缺失/截断的
report.json 按「该 run 无报告」容错。`_dump_json` 是本层的 JSON 落盘
原语——service 终态写 meta.json / raw_results.json 共用（closed_eval
三分，原函数随「写」职责归本层）。

分层红线：本层不 import fastapi（设计文档 §9.4）；MovieResult 以类型
标注消费（TYPE_CHECKING 引用，运行时零反向依赖 service）。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from tagplatform.services.eval.closed_ranking import (
    ClosedSample,
    evaluate_closed,
    spearman,
)

if TYPE_CHECKING:
    from tagplatform.services.eval.service import MovieResult

__all__ = [
    "assemble_report",
    "render_report_md",
    "write_report",
]


def _dump_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------- 报告组装与历史扫描

# 部分运行口径（banner 文案；eval.md §4 同口径）
BANNER_PARTIAL = "部分运行：指标仅覆盖完成影片，与全量口径不可直接横比"
# 银标准警示（MovieTags README §6.5 同口径）：gt 是相关性模型输出非人工标注
NOTE_SILVER = (
    "银标准警示：genome relevance 是相关性模型输出，近似而非人工标注"
    "（MovieTags README §6.5 同口径）——指标衡量的是系统与该模型口径的一致"
    "性，不是与人工真理的一致；不同口径（LLM/嵌入/模板/标签库/数据集）的"
    "分数不可跨组比较。")

# metrics.csv 长表列（spec §8：Excel/后续分析消费）
_CSV_HEADER = ("theta", "k", "metric", "macro", "ci_low", "ci_high",
               "random_baseline")


def _grid_rows(grid) -> list[dict]:
    """ClosedReport.grid → report.json 格列表（CI 元组 → [lo, hi] 列表）。"""
    def _ci(value):
        return None if value is None else [value[0], value[1]]

    return [{"theta": gp.theta, "k": gp.k, "n_valid": gp.n_valid,
             "precision": gp.precision, "recall": gp.recall,
             "ndcg": gp.ndcg,
             "precision_ci": _ci(gp.precision_ci),
             "recall_ci": _ci(gp.recall_ci), "ndcg_ci": _ci(gp.ndcg_ci),
             "precision_random": gp.precision_random,
             "recall_random": gp.recall_random}
            for gp in grid]


def _prior_ranking(gt: dict[str, dict[str, float]],
                   tag_universe: Sequence[str]
                   ) -> tuple[tuple[str, ...], dict[str, float]]:
    """先验序：tag_universe 按 gt 全影片均值降序（并列 tag_id 字典序）。

    均值用 gt **全表**（数据集全部影片，非仅选片/成功片——spec 决定清单
    #7：先验基线衡量「不看电影只看先验」能到哪）。按「有分的影片」取
    均值：genome 矩阵稠密无缺失，两种算法同数；稀疏 gt 里缺行 = 没评过
    ，不是「不相关」（None≠0 的均值版）。universe ⊆ gt 标签集，均值分母
    恒 ≥ 1。
    """
    means: dict[str, float] = {}
    for tag in tag_universe:
        values = [scores[tag] for scores in gt.values() if tag in scores]
        means[tag] = sum(values) / len(values) if values else 0.0
    order = tuple(sorted(tag_universe, key=lambda tag: (-means[tag], tag)))
    return order, means


def assemble_report(results: list[MovieResult],
                    gt: dict[str, dict[str, float]],
                    tag_universe: Sequence[str],
                    thetas: Sequence[float],
                    ks: Sequence[int], *, meta: dict) -> dict:
    """干跑结果 → report.json 结构（前端 Task 7 契约，report.md 同数据）。

    样本构造（spec §5/§7 钉死）：每部 ok 片一条 ClosedSample——

    - `ranked` = 产出行中 tag_universe 内的条目，防御性再排
      (-sf, tag_id)（produced 本已排好，但本函数是公共入口，不信任输入序）；
      交集外产出条目剔除、计入 `sample_accounting.no_gt`（白给 precision /
      冤枉 recall 都不要）；失败片 produced 已清空，只进 failed_movies；
    - `relevance` = gt 影片向量限 tag_universe（空表 → evaluate_closed 记
      invalid，不进任何平均）；
    - `system_scores` = {tag_id: sf}（Spearman 里未产出的交集标签按 0 分
      补位）。

    先验基线（钉死）：先验序 = `_prior_ranking`，每部 ok 片用**同一
    ranked** 构造先验样本、同机制喂 `evaluate_closed`——基线与系统吃完全
    相同的指标管线，格值才可比。

    banner / warning 语义：status != succeeded 或 done < total → 部分运行
    banner（取消/中止/缩量跑都不可与全量横比）；失败率（failed/total）
    > 10% → 小样本警示，否则 None。
    """
    universe = set(tag_universe)
    n_failed = sum(1 for r in results if not r.ok)
    no_gt = 0
    samples: list[ClosedSample] = []
    per_movie: list[dict] = []
    for result in results:
        if not result.ok:
            continue
        rows = [row for row in result.produced
                if row.get("tag_id") in universe]
        no_gt += len(result.produced) - len(rows)
        rows.sort(key=lambda row: (-float(row.get("sf", 0.0)),
                                   row["tag_id"]))
        system_scores = {row["tag_id"]: float(row.get("sf", 0.0))
                         for row in rows}
        relevance = {tag: score for tag, score
                     in gt.get(result.tt, {}).items() if tag in universe}
        samples.append(ClosedSample(
            ranked=tuple(row["tag_id"] for row in rows),
            relevance=relevance, system_scores=system_scores,
            label=result.tt))
        if relevance:  # 无交集影片 Spearman 算不出，不入 per_movie
            keys = sorted(relevance)
            per_movie.append({"tt": result.tt, "spearman": spearman(
                [system_scores.get(tag, 0.0) for tag in keys],
                [relevance[tag] for tag in keys])})
    closed = evaluate_closed(samples, thetas, ks)

    prior_ranked, prior_means = _prior_ranking(gt, tag_universe)
    prior_closed = evaluate_closed(
        [ClosedSample(ranked=prior_ranked, relevance=s.relevance,
                      system_scores={tag: prior_means[tag]
                                     for tag in s.relevance},
                      label=s.label)
         for s in samples], thetas, ks)

    total = meta.get("total")
    if not isinstance(total, int):
        total = len(results)
    done = meta.get("done")
    if not isinstance(done, int):
        done = len(results)
    banner = (None
              if meta.get("status") == "succeeded" and done >= total
              else BANNER_PARTIAL)
    warning = None
    if total and n_failed / total > 0.1:
        warning = (f"失败率 {n_failed / total:.0%}（{n_failed}/{total}）超过"
                   " 10%：成功样本少，指标波动大，勿单独引用；失败影片见"
                   " failed_movies")
    return {
        "banner": banner,
        "sample_accounting": {"total": total, "ok": len(samples),
                              "failed": n_failed,
                              "invalid": closed.n_invalid, "no_gt": no_gt},
        "grid": _grid_rows(closed.grid),
        "baselines": {"prior": _grid_rows(prior_closed.grid)},
        "coverage": {"min": closed.coverage_min,
                     "median": closed.coverage_median,
                     "max": closed.coverage_max,
                     "values": closed.coverage},
        "spearman": {"macro": closed.spearman_macro, "per_movie": per_movie},
        "failed_movies": [{"tt": r.tt, "error": r.error}
                          for r in results if not r.ok],
        "warning": warning,
        "calibre": {
            "llm_model": meta.get("llm_model", ""),
            "embedding_model": meta.get("embedding_model", ""),
            "prompt_version": (meta.get("prompt") or {}).get("version"),
            "library_version": (meta.get("library") or {}).get("version"),
            "dataset_id": (meta.get("dataset") or {}).get("id"),
            "gt_sha8": (meta.get("dataset") or {}).get("gt_sha8"),
        },
        "note": NOTE_SILVER,
    }


def _fmt(value) -> str:
    """md 数值格：None → 暂无（None≠0 纪律的报表侧），浮点保留三位。"""
    return "暂无" if value is None else f"{value:.3f}"


def _fmt_ci(ci) -> str:
    """md 置信区间格：[lo, hi]；None → 暂无。"""
    return "暂无" if not ci else f"[{ci[0]:.3f}, {ci[1]:.3f}]"


def _fmt_cal(value) -> str:
    """md 口径格：None / 空串 → 暂无，其余原样。"""
    return "暂无" if value is None or value == "" else str(value)


def render_report_md(report: dict) -> str:
    """report dict → 人读 markdown（report.md，与 report.json 同数据）。

    头部固定渲染 banner / warning（部分口径不藏在正文里）与口径三件套
    （calibre）+ 银标准警示（note）；θ×K 网格与先验基线并排两张表，
    覆盖率/Spearman/失败清单各一节。None 一律「暂无」。
    """
    lines: list[str] = ["# 闭环排序评测报告", ""]
    if report.get("banner"):
        lines += [f"> ⚠ {report['banner']}", ""]
    if report.get("warning"):
        lines += [f"> ⚠ {report['warning']}", ""]
    acc = report["sample_accounting"]
    lines += [
        (f"样本核算：共 {acc['total']} 部——成功 {acc['ok']}、失败"
         f" {acc['failed']}、无交集不可评 {acc['invalid']}；产出行落在"
         f" 库∩gt 交集外 {acc['no_gt']} 条（不计分只计数）。"),
        "",
        "## θ×K 网格（宏平均 ± 95% 自举 CI）",
        "",
        "| θ | K | n | P@K | R@K | nDCG@K | 随机P | 随机R |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for g in report["grid"]:
        lines.append(
            f"| {g['theta']} | {g['k']} | {g['n_valid']} | "
            f"{_fmt(g['precision'])} {_fmt_ci(g['precision_ci'])} | "
            f"{_fmt(g['recall'])} {_fmt_ci(g['recall_ci'])} | "
            f"{_fmt(g['ndcg'])} {_fmt_ci(g['ndcg_ci'])} | "
            f"{_fmt(g['precision_random'])} | {_fmt(g['recall_random'])} |")
    lines += [
        "",
        "## 先验基线（tag_universe 按 gt 全影片均值降序，同机制并排）",
        "",
        "| θ | K | n | P@K | R@K | nDCG@K |",
        "|---|---|---|---|---|---|",
    ]
    for g in report["baselines"]["prior"]:
        lines.append(
            f"| {g['theta']} | {g['k']} | {g['n_valid']} | "
            f"{_fmt(g['precision'])} | {_fmt(g['recall'])} | "
            f"{_fmt(g['ndcg'])} |")
    coverage = report["coverage"]
    lines += [
        "",
        "## 覆盖率（产出行数 / 交集标签数）",
        "",
        (f"- min {_fmt(coverage['min'])} / 中位 {_fmt(coverage['median'])}"
         f" / max {_fmt(coverage['max'])}"
         f"（{len(coverage['values'])} 部有交集）——R@K 上限被产出行数封顶，"
         "读召回必须同读覆盖率。"),
        "",
        "## Spearman 秩相关（系统分 vs gt 分，未产出交集标签按 0 分补位）",
        "",
        f"- 宏平均：{_fmt(report['spearman']['macro'])}",
        "",
        "| tt | Spearman |",
        "|---|---|",
    ]
    for row in report["spearman"]["per_movie"]:
        lines.append(f"| {row['tt']} | {_fmt(row['spearman'])} |")
    if report["failed_movies"]:
        lines += ["", f"## 失败影片（{len(report['failed_movies'])} 部）", "",
                  "| tt | 错误 |", "|---|---|"]
        for row in report["failed_movies"]:
            lines.append(f"| {row['tt']} | {row['error']} |")
    cal = report["calibre"]
    lines += [
        "",
        "## 口径",
        "",
        (f"- LLM `{_fmt_cal(cal['llm_model'])}`；嵌入"
         f" `{_fmt_cal(cal['embedding_model'])}`；prompt"
         f" v{_fmt_cal(cal['prompt_version'])}；标签库"
         f" v{_fmt_cal(cal['library_version'])}"),
        (f"- 数据集 `{_fmt_cal(cal['dataset_id'])}`（gt sha8"
         f" `{_fmt_cal(cal['gt_sha8'])}`）"),
        "",
        f"> {report['note']}",
        "",
    ]
    return "\n".join(lines)


def write_report(run_dir: Path, report: dict) -> None:
    """报告三件套落盘：report.json / report.md / metrics.csv。

    长表行 `theta,k,metric,macro,ci_low,ci_high,random_baseline`（系统
    网格；None → 空格，ndcg 无解析随机基线）。写序在 meta/raw 之后
    （worker 终态序）；读侧（history）对缺失/截断的 report.json 容错。
    """
    _dump_json(run_dir / "report.json", report)
    (run_dir / "report.md").write_text(render_report_md(report),
                                       encoding="utf-8")
    with open(run_dir / "metrics.csv", "w", encoding="utf-8",
              newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for g in report["grid"]:
            for metric, random_key in (("precision", "precision_random"),
                                       ("recall", "recall_random"),
                                       ("ndcg", None)):
                ci = g.get(f"{metric}_ci")
                writer.writerow([
                    g["theta"], g["k"], metric, g[metric],
                    ci[0] if ci else "", ci[1] if ci else "",
                    g[random_key] if random_key else "",
                ])
