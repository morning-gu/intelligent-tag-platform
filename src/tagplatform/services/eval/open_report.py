"""开放评测报告层：样本构造 → evaluate_open → report.json / report.md / metrics.csv。

报告组装（Task 5；镜像闭环 report.py 的组装/渲染/写三分）
==========================================================

`assemble_open_report` 把 ok 片构造成 `OpenSample` 喂 `evaluate_open`
（与闭环 assemble_report 同款过滤纪律：produce 失败片结构上无法表达，
不进样本，只进 failed_movies 与 sample_accounting.failed——n_failed 在
组装层自算）；伴生 judge 段读 run 目录 audit/<tt>.json（mtg 管线自落的
评审官审计，跨片微聚合计数；容错：文件缺失/损坏跳过该片贡献，目录整体
缺失 → 空段 + 警示，绝不抛错）。

banner 聚合（闭环的单条 banner 在开放侧扩成 **list[str]**）：部分运行
（status 未「全量成功完成」——cancelled/aborted/failed/done<total 一律
不可与全量横比）→ 同模评审警示（calibre.review_same_model，spec §6 文案）
→ 小样本警示（失败核算三类 failed/review_failed/invalid 任一占比 >10%）。
warnings[] = evaluate_open 的口径警示（评审失败片/invalid 片/θ 覆盖空格）
+ judge 审计缺失警示，原样透传。

写盘纪律：报告层只读写文件，DB 零写入；`write_open_report` 三件套写序在
meta/raw 之后（worker 终态序），读侧（open_service.history →
_read_open_summary）对缺失/截断的 report.json 按「该 run 无报告」容错
（宁缺勿假）。`_dump_json` / `NOTE_SILVER` / `BANNER_PARTIAL` 复用闭环
report.py（同一落盘原语、同一银标准警示原文与同一部分运行文案）。

分层红线：本层不 import fastapi（设计文档 §9.4）；OpenMovieResult 以类型
标注消费（TYPE_CHECKING 引用，运行时零反向依赖 service）。
"""
from __future__ import annotations

import csv
import json
import statistics
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from tagplatform.services.eval.open_ranking import (
    DEFAULT_THETA_HEADLINE,
    OpenSample,
    evaluate_open,
)
from tagplatform.services.eval.report import (
    BANNER_PARTIAL,
    NOTE_SILVER,
    _dump_json,
)

if TYPE_CHECKING:
    from tagplatform.services.eval.open_service import OpenMovieResult

__all__ = [
    "assemble_open_report",
    "render_open_report_md",
    "write_open_report",
]

# 同模评审警示（spec §6 文案）：评审与生成同模型 → 通过率绝对值系统性偏高
BANNER_SAME_MODEL = ("评审与生成同模型，通过率绝对值系统性偏高，趋势对比可用、"
                     "绝对值需打折解读")
# 小样本警示（闭环 warning 文案的开放版：失败核算三类分开点名）
_BANNER_SMALL_SAMPLE = ("小样本警示：{detail}，占比超 10%——成功样本少，"
                        "指标波动大，勿单独引用")
# judge 审计整体缺失警示（warnings[] 项；audit/ 由 mtg 管线经 run 级
# settings 副本落进 run 目录，缺失 = 旧管线/替身产出，宁缺勿假）
_JUDGE_MISSING = "评审官审计（audit/）缺失或为空，judge 段无数据"

# judge verdicts 的三个已知取值（parse_judge_items 的 _VERDICTS 集合），
# 其余取值按原样聚合计数
_VERDICT_KEYS = ("supported", "insufficient", "contradicted")
# 目录对齐分档的三个已知档位（catalog_align 的 BAND_* 常量）
_BAND_KEYS = ("duplicate", "suspect", "new")

# metrics.csv 长表列（spec §8：Excel/后续分析消费；θ 无关行 theta 空）
_CSV_HEADER = ("metric", "theta", "macro", "ci_low", "ci_high",
               "random_baseline", "prior_baseline", "n_valid")


def _ci_list(value):
    """CI 元组 → [lo, hi] 列表（JSON 安全）；None 原样。"""
    return None if value is None else [value[0], value[1]]


# ---------------------------------------------------------------- 样本构造

def _hit_flags(result: "OpenMovieResult", intersection: dict) -> tuple[bool, ...]:
    """逐命中行求「∈ 库∩gt 交集」标志（OpenSample 契约的装配侧实现）。

    OpenSample 契约：hit_in_flags True ⟺ tag_id ∈ 该片交集（库 ∩ gt）——
    直接按交集成员判定。生产条件下行 gt 与 gt 表同源（同一 job.gt），与
    brief 的 `r["gt"] is not None` 简写等价；按交集成员判定可防产出越库
    （替身/脏数据 corner）把交集外命中误进准确率族（spec §5.1：交集外
    命中只计数），也让 evaluate_open 的随机基线 r/n 不会在空交集上除零。
    """
    return tuple(row["tag_id"] in intersection for row in result.hits)


def _row_pass(row: dict):
    """行级评审判定：review dict 的 pass 值（None = 漏判/未评审）。"""
    return (row.get("review") or {}).get("pass")


def _sample_for(result: "OpenMovieResult", intersection: dict) -> OpenSample:
    """单片 → OpenSample（映射规则见 brief；评审失败契约 spec §6）。"""
    if result.review_error is None:
        hit_pass = tuple(_row_pass(row) for row in result.hits)
        new_pass = tuple(_row_pass(row) for row in result.new_words)
    else:
        # 评审失败：hit_pass/new_pass 整体 None（排除出通过率/标定，
        # 准确率照常可算——产出不作废）
        hit_pass = None
        new_pass = None
    return OpenSample(
        label=result.tt,
        hit_ids=tuple(row["tag_id"] for row in result.hits),
        hit_in_flags=_hit_flags(result, intersection),
        intersection=intersection,
        hit_pass=hit_pass,
        new_pass=new_pass,
    )


def _movie_row(result: "OpenMovieResult", flags: tuple[bool, ...],
               intersection: dict) -> dict:
    """单片 → movies 折叠明细行（前端 T7 契约键）。

    - `review` 三态与 progress.jsonl 同口径：ok / failed（评审失败）/
      skipped（produce 失败未发起评审）；
    - `accuracy`：片级集合准确率@DEFAULT_THETA_HEADLINE（与历史摘要/报告
      汇总卡同款取格约定；分子用交集分值——与 θ 族同源），交集内命中为
      空 → None（θ 族不可评，None≠0）；
    - `new_pass`/`hit_pass`：片级评审通过率（漏判条目排除出分子分母），
      评审失败片整体 None，无可判定条目 → None。
    """
    if result.review_error is not None:
        hit_pass = new_pass = None
    else:
        hit_pass = _rate(result.hits)
        new_pass = _rate(result.new_words)
    n_in = sum(flags)
    accuracy = None
    if n_in:
        hits_relevant = sum(1 for row, flag in zip(result.hits, flags)
                            if flag
                            and intersection[row["tag_id"]]
                            >= DEFAULT_THETA_HEADLINE)
        accuracy = hits_relevant / n_in
    return {
        "tt": result.tt,
        "title": result.title,
        "ok": result.ok,
        "review": ("skipped" if not result.ok
                   else "failed" if result.review_error is not None else "ok"),
        "hits_n": len(result.hits),
        "new_n": len(result.new_words),
        "accuracy": accuracy,
        "new_pass": new_pass,
        "hit_pass": hit_pass,
        "error": result.error,
        "review_error": result.review_error,
        "elapsed_ms": result.elapsed_ms,
    }


def _rate(rows: list) -> float | None:
    """行级二值通过率：True 数 / 非 None 判定数；无可判定条目 → None。"""
    verdicts = [_row_pass(row) for row in rows]
    judged = [v for v in verdicts if v is not None]
    if not judged:
        return None
    return sum(1 for v in judged if v) / len(judged)


# ---------------------------------------------------------------- judge 自省

def _judge_section(audit_dir: Path | None, ok_films: list[str],
                   warnings: list[str]) -> dict:
    """评审官审计跨片聚合（微口径、整 run 汇总；audit/<tt>.json 键形见
    mtg pipeline._write_report）。

    - `self_filter` = Σaccepted / Σmerged（合并候选被 judge 自滤掉的比
      例）；merged 全空 → None（None≠0）；
    - `verdicts`：verdict 取值分布（三已知档前置 0，其余取值按原样追加）；
    - `dropped`：drop 原因（why 字段）计数；
    - `bands`：目录对齐分档计数（library.align 的 band 字段）。
    容错：文件缺失/损坏/非 dict → 跳过该片贡献；有 ok 片但一篇审计都
    读不到 → 空段 + warnings 追加警示。
    """
    merged_total = accepted_total = contributed = 0
    verdicts = {key: 0 for key in _VERDICT_KEYS}
    dropped: dict[str, int] = {}
    bands = {key: 0 for key in _BAND_KEYS}
    if audit_dir is not None:
        for tt in ok_films:
            try:
                audit = json.loads((audit_dir / f"{tt}.json")
                                   .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(audit, dict):
                continue
            contributed += 1
            merged = audit.get("merged")
            accepted = audit.get("accepted")
            if isinstance(merged, list):
                merged_total += len(merged)
            if isinstance(accepted, list):
                accepted_total += len(accepted)
            for row in audit.get("verdicts") or []:
                if isinstance(row, dict):
                    label = str(row.get("verdict") or "未知")
                    verdicts[label] = verdicts.get(label, 0) + 1
            for row in audit.get("dropped") or []:
                if isinstance(row, dict):
                    reason = str(row.get("why") or "未知")
                    dropped[reason] = dropped.get(reason, 0) + 1
            library = audit.get("library")
            align = library.get("align") if isinstance(library, dict) else None
            for row in align or []:
                if isinstance(row, dict) and row.get("band") is not None:
                    band = str(row["band"])
                    bands[band] = bands.get(band, 0) + 1
    if ok_films and contributed == 0:
        warnings.append(_JUDGE_MISSING)
    return {
        "self_filter": (accepted_total / merged_total
                        if merged_total else None),
        "verdicts": verdicts,
        "dropped": dropped,
        "bands": bands,
    }


# ---------------------------------------------------------------- banner

def _banner(meta: dict, total: int, done: int, n_failed: int,
            n_review_failed: int, n_invalid: int) -> list[str]:
    """banner 聚合（list[str]）：部分运行 → 同模评审 → 小样本。

    部分运行口径与闭环同款：status 未 succeeded 或 done < total 一律不可
    与全量横比（覆盖 brief 钉死的 cancelled/aborted，也覆盖 worker 兜底
    的 failed 与取消后余量未跑的 done<total）。
    """
    banner: list[str] = []
    if not (meta.get("status") == "succeeded" and done >= total):
        banner.append(BANNER_PARTIAL)
    if (meta.get("review") or {}).get("same_model"):
        banner.append(BANNER_SAME_MODEL)
    if total:
        parts = []
        if n_failed / total > 0.1:
            parts.append(f"produce 失败 {n_failed}/{total}")
        if n_review_failed / total > 0.1:
            parts.append(f"评审失败 {n_review_failed}/{total}")
        if n_invalid / total > 0.1:
            parts.append(f"交集内命中为空 {n_invalid}/{total}")
        if parts:
            banner.append(_BANNER_SMALL_SAMPLE.format(detail="；".join(parts)))
    return banner


# ---------------------------------------------------------------- 主装配

def assemble_open_report(results: list["OpenMovieResult"],
                         gt: dict[str, dict[str, float]],
                         library_ids: Collection[str],
                         thetas: Sequence[float],
                         meta: dict, *,
                         audit_dir: Path | None = None) -> dict:
    """干跑结果 → report.json 结构（前端 T7 契约，report.md 同数据）。

    样本构造：每部 ok 片一条 `OpenSample`（`intersection` = 该片 gt 向量
    限库∩gt；`hit_in_flags` = 按交集成员判定，见 _hit_flags）；produce
    失败片不进样本（只进 failed_movies，n_failed 组装层自算）；评审失败
    片进样本但 hit/new_pass 整体 None（evaluate_open 排除出通过率/标定，
    保留在准确率族）。`audit_dir` = run 目录的 audit/ 子目录（接线处传
    入；None = 无审计，judge 空段 + 警示）。
    """
    library = set(library_ids)
    samples: list[OpenSample] = []
    counts: list[dict] = []
    movies: list[dict] = []
    ok_films: list[str] = []
    n_failed = 0
    n_review_failed = 0
    for result in results:
        intersection = {tag: score
                        for tag, score in gt.get(result.tt, {}).items()
                        if tag in library}
        flags = _hit_flags(result, intersection)
        if result.ok:
            ok_films.append(result.tt)
            samples.append(_sample_for(result, intersection))
            counts.append({"tt": result.tt, "hits_in": sum(flags),
                           "hits_out": len(result.hits) - sum(flags),
                           "new_words": len(result.new_words)})
        else:
            n_failed += 1
        if result.ok and result.review_error is not None:
            n_review_failed += 1
        movies.append(_movie_row(result, flags, intersection))
    report = evaluate_open(samples, gt, thetas=thetas or DEFAULT_THETAS)

    total = meta.get("total")
    if not isinstance(total, int):
        total = len(results)
    done = meta.get("done")
    if not isinstance(done, int):
        done = len(results)
    warnings = list(report.warnings)
    totals = [c["hits_in"] + c["hits_out"] + c["new_words"] for c in counts]
    return {
        "banner": _banner(meta, total, done, n_failed, n_review_failed,
                          report.n_invalid),
        "sample_accounting": {
            "total": total,
            "ok": len(ok_films),
            "failed": n_failed,
            "review_failed": n_review_failed,
            "invalid": report.n_invalid,
        },
        "no_gt": report.no_gt,
        "theta_rows": [{
            "theta": row.theta,
            "n_valid": row.n_valid,
            "accuracy": row.accuracy,
            "accuracy_ci": _ci_list(row.accuracy_ci),
            "accuracy_random": row.accuracy_random,
            "accuracy_prior": row.accuracy_prior,
            "coverage": row.coverage,
            "n_calibrated": row.n_calibrated,
            "agreement": row.agreement,
            "agreement_ci": _ci_list(row.agreement_ci),
            "fp_rate": row.fp_rate,
            "fn_rate": row.fn_rate,
        } for row in report.theta_rows],
        "pass_rates": {
            "new_macro": report.new_pass_macro,
            "new_micro": report.new_pass_micro,
            "new_n": report.new_pass_n,
            "hit_macro": report.hit_pass_macro,
            "hit_micro": report.hit_pass_micro,
            "hit_n": report.hit_pass_n,
            "review_missing": report.review_missing,
        },
        "ndcg": {"macro": report.ndcg_macro, "ci": _ci_list(report.ndcg_ci)},
        "new_ratio": {"macro": report.new_ratio_macro},
        "produced_stats": {
            "counts": counts,
            "min": min(totals) if totals else None,
            "median": statistics.median(totals) if totals else None,
            "max": max(totals) if totals else None,
        },
        "judge": _judge_section(audit_dir, ok_films, warnings),
        "calibre": {
            "llm_model": meta.get("llm_model", ""),
            "embedding_model": meta.get("embedding_model", ""),
            "prompt_version": (meta.get("prompt") or {}).get("version"),
            "review_model": (meta.get("review") or {}).get("model"),
            "review_same_model": (meta.get("review") or {}).get("same_model"),
            "review_prompt_version": (meta.get("review") or {}).get(
                "prompt_version"),
            "library_version": (meta.get("library") or {}).get("version"),
            "dataset_id": (meta.get("dataset") or {}).get("id"),
            "gt_sha8": (meta.get("dataset") or {}).get("gt_sha8"),
            "modalities": meta.get("modalities"),
        },
        "movies": movies,
        "failed_movies": [{"tt": r.tt, "error": r.error}
                          for r in results if not r.ok],
        "review_failed_movies": [{"tt": r.tt, "review_error": r.review_error}
                                 for r in results
                                 if r.ok and r.review_error is not None],
        "warnings": warnings,
        "note": NOTE_SILVER,
    }


# ---------------------------------------------------------------- 渲染

def _fmt(value) -> str:
    """md 数值格：None → 暂无（None≠0 纪律的报表侧），浮点保留三位。"""
    return "暂无" if value is None else f"{value:.3f}"


def _fmt_ci(ci) -> str:
    """md 置信区间格：[lo, hi]；None → 暂无。"""
    return "暂无" if not ci else f"[{ci[0]:.3f}, {ci[1]:.3f}]"


def _fmt_cal(value) -> str:
    """md 口径格：None / 空串 → 暂无，其余原样。"""
    return "暂无" if value is None or value == "" else str(value)


def render_open_report_md(report: dict) -> str:
    """report dict → 人读 markdown（report.md，与 report.json 同数据）。

    头部固定渲染 banner 聚合与 warnings（部分口径不藏在正文里）+ 样本
    核算 + θ 网格 + 通过率/标定 + 产出规模 + judge 审计 + 失败清单 +
    口径三件套 + 银标准警示；末节固定「与闭环评测的口径关系」（spec §12：
    独立口径组不横比，preset30 同 seed 只做定性对照）。None 一律「暂无」。
    """
    lines: list[str] = ["# 开放评测报告", ""]
    for item in report.get("banner") or []:
        lines += [f"> ⚠ {item}", ""]
    for item in report.get("warnings") or []:
        lines += [f"> ⚠ {item}", ""]
    acc = report["sample_accounting"]
    lines += [
        (f"样本核算：共 {acc['total']} 部——produce 成功 {acc['ok']}"
         f"（评审失败 {acc['review_failed']} 部在内）、失败 {acc['failed']}"
         f"、交集内命中为空 {acc['invalid']} 部（不进 θ 族平均）；产出行落"
         f"在 库∩gt 交集外 {report['no_gt']} 条（不计分只计数）。"),
        "",
        "## θ 网格（宏平均 ± 95% 自举 CI；n = 交集内命中非空的片数）",
        "",
        "| θ | n | 准确率@θ | 随机基线 | 先验基线 | 覆盖率 | 一致率 | FP率 |"
        " FN率 | 标定片数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["theta_rows"]:
        lines.append(
            f"| {row['theta']} | {row['n_valid']} | "
            f"{_fmt(row['accuracy'])} {_fmt_ci(row['accuracy_ci'])} | "
            f"{_fmt(row['accuracy_random'])} | {_fmt(row['accuracy_prior'])}"
            f" | {_fmt(row['coverage'])} | "
            f"{_fmt(row['agreement'])} {_fmt_ci(row['agreement_ci'])} | "
            f"{_fmt(row['fp_rate'])} | {_fmt(row['fn_rate'])} | "
            f"{row['n_calibrated']} |")
    pr = report["pass_rates"]
    lines += [
        "",
        "## 评审通过率与新词占比（评审成功片；n 为片数口径）",
        "",
        (f"- 命中通过率：宏 {_fmt(pr['hit_macro'])} / 微"
         f" {_fmt(pr['hit_micro'])}（{pr['hit_n']} 片）"),
        (f"- 新词通过率：宏 {_fmt(pr['new_macro'])} / 微"
         f" {_fmt(pr['new_micro'])}（{pr['new_n']} 片）"),
        (f"- 新词占比（新词条数 / 全部产出行数）：宏"
         f" {_fmt(report['new_ratio']['macro'])}"),
        (f"- 缺评审条目 {pr['review_missing']} 条（缺评审 ≠ 模型漏判：含"
         "评审失败片的整体缺位与评审成功片的单条缺席）"),
        "",
        "## nDCG（产出序、连续增益、全列表不截断）",
        "",
        (f"- 宏平均：{_fmt(report['ndcg']['macro'])} "
         f"{_fmt_ci(report['ndcg']['ci'])}"),
        "",
        "## 产出规模（成功片逐片产出行数：交集内 + 交集外命中 + 新词）",
        "",
    ]
    stats = report["produced_stats"]
    n_films = len(stats["counts"])
    if stats["min"] is None:
        lines.append(f"- 暂无（{n_films} 部成功片无任何产出统计可算）")
    else:
        # 产出规模是条数（median 可为 x.5），不用三位小数的比率格式
        lines.append(
            f"- min {format(stats['min'], 'g')} / 中位 "
            f"{format(stats['median'], 'g')} / max "
            f"{format(stats['max'], 'g')}（{n_films} 部成功片）")
    lines += ["", "## 评审官审计（judge 自省，跨片聚合）", ""]
    judge = report["judge"]
    verdict_parts = [f"{key} {count}"
                     for key, count in judge["verdicts"].items()]
    lines += [
        f"- 自滤率（accepted / merged）：{_fmt(judge['self_filter'])}",
        f"- verdicts 分布：{' / '.join(verdict_parts) or '暂无'}",
        ("- dropped 原因：" + ("、".join(
            f"{reason} ×{count}"
            for reason, count in judge["dropped"].items()) or "暂无")),
        ("- 对齐分档：" + " / ".join(
            f"{key} {judge['bands'].get(key, 0)}" for key in _BAND_KEYS)),
    ]
    if report["failed_movies"]:
        lines += ["", f"## 失败影片（{len(report['failed_movies'])} 部）", "",
                  "| tt | 错误 |", "|---|---|"]
        for row in report["failed_movies"]:
            lines.append(f"| {row['tt']} | {row['error']} |")
    if report["review_failed_movies"]:
        lines += ["", (f"## 评审失败影片（{len(report['review_failed_movies'])}"
                       f" 部——产出不作废，不进通过率/标定）"), "",
                  "| tt | 原因 |", "|---|---|"]
        for row in report["review_failed_movies"]:
            lines.append(f"| {row['tt']} | {row['review_error']} |")
    cal = report["calibre"]
    same_model = "（同模评审）" if cal.get("review_same_model") else ""
    lines += [
        "",
        "## 口径",
        "",
        (f"- LLM `{_fmt_cal(cal['llm_model'])}`；嵌入"
         f" `{_fmt_cal(cal['embedding_model'])}`；prompt"
         f" v{_fmt_cal(cal['prompt_version'])}；标签库"
         f" v{_fmt_cal(cal['library_version'])}"),
        (f"- 评审模型 `{_fmt_cal(cal['review_model'])}`{same_model}；评审"
         f" prompt v{_fmt_cal(cal['review_prompt_version'])}"),
        (f"- 数据集 `{_fmt_cal(cal['dataset_id'])}`（gt sha8"
         f" `{_fmt_cal(cal['gt_sha8'])}`）；模态"
         f" {', '.join(cal.get('modalities') or ()) or '暂无'}"),
        "",
        f"> {report['note']}",
        "",
        "## 与闭环评测的口径关系",
        "",
        "- 开放评测与闭环评测是**独立口径组**：样本构造（集合式命中 vs",
        "  定长 K 截断）、指标族（集合准确率 + 评审通过率 vs P/R/nDCG@K）",
        "  与评审语义（质量三条件二值评审 vs 无评审）均不同，数值不可直接",
        "  横比；",
        "- 同片集（preset30 同 seed）下两条口径只做**定性对照**：结论方向",
        "  一致互相印证，数值差异不构成任何一方的错误证据。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 落盘

def write_open_report(run_dir: Path, report: dict) -> None:
    """报告三件套落盘：report.json / report.md / metrics.csv。

    长表行 `metric,theta,macro,ci_low,ci_high,random_baseline,
    prior_baseline,n_valid`（θ 无关行 theta 空；None → 空格）。accuracy
    行带随机/先验基线；agreement/fp/fn 行的 n_valid 列填 n_calibrated
    （一致率宏平均的片数口径）；写序在 meta/raw 之后（worker 终态序），
    读侧（history → _read_open_summary）对缺失/截断的 report.json 容错。
    """
    _dump_json(run_dir / "report.json", report)
    (run_dir / "report.md").write_text(render_open_report_md(report),
                                       encoding="utf-8")
    with open(run_dir / "metrics.csv", "w", encoding="utf-8",
              newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for row in report["theta_rows"]:
            n_cal = row["n_calibrated"]
            for metric, macro, ci, rand, prior, n in (
                    ("accuracy", row["accuracy"], row["accuracy_ci"],
                     row["accuracy_random"], row["accuracy_prior"],
                     row["n_valid"]),
                    ("coverage", row["coverage"], None, "", "",
                     row["n_valid"]),
                    ("agreement", row["agreement"], row["agreement_ci"],
                     "", "", n_cal),
                    ("fp_rate", row["fp_rate"], None, "", "", n_cal),
                    ("fn_rate", row["fn_rate"], None, "", "", n_cal)):
                writer.writerow([metric, row["theta"], macro,
                                 ci[0] if ci else "", ci[1] if ci else "",
                                 rand, prior, n])
        ndcg = report["ndcg"]
        ndcg_ci = ndcg["ci"]
        pr = report["pass_rates"]
        # θ 无关行：theta 空；CI 只有 ndcg 有（通过率宏平均无区间）
        writer.writerow(["ndcg", "", ndcg["macro"],
                         ndcg_ci[0] if ndcg_ci else "",
                         ndcg_ci[1] if ndcg_ci else "", "", "", ""])
        for metric, macro, n in (
                ("new_pass", pr["new_macro"], pr["new_n"]),
                ("new_pass_micro", pr["new_micro"], ""),
                ("hit_pass", pr["hit_macro"], pr["hit_n"]),
                ("hit_pass_micro", pr["hit_micro"], ""),
                ("new_ratio", (report["new_ratio"] or {}).get("macro"), "")):
            writer.writerow([metric, "", macro, "", "", "", "", n])


# ---------------------------------------------------------------- 历史摘要

def _read_open_summary(run_dir: Path) -> dict:
    """report.json → 历史摘要五键（统一取格约定，与闭环 _read_summary 同款）。

    accuracy40 / agreement40 = 最接近 DEFAULT_THETA_HEADLINE(0.4) 的最近
    **有值** θ 格（「有值」= 字段值为数值：该 θ 无有效影片/无可标定片 →
    格值 None 不算）；距离并列取小（round 9 防浮点噪声）；无有值格 →
    None（宁缺勿假——custom-θ run 回退最近有值档，不再因缺 0.4 档空列）。
    new_pass / hit_pass / new_ratio 直读 pass_rates / new_ratio 的宏平均。
    report.json 缺失/损坏/缺段 → 对应键 None，不报错。
    """
    summary = {"accuracy40": None, "new_pass": None, "hit_pass": None,
               "agreement40": None, "new_ratio": None}
    try:
        report = json.loads((run_dir / "report.json")
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return summary
    if not isinstance(report, dict):
        return summary
    theta_rows = report.get("theta_rows")
    if isinstance(theta_rows, list):

        def _pick(field: str):
            """统一取格：最接近 headline 的有值格（并列取小）。"""
            valued = [row for row in theta_rows
                      if isinstance(row, dict)
                      and isinstance(row.get("theta"), (int, float))
                      and isinstance(row.get(field), (int, float))]
            if not valued:
                return None
            return min(valued, key=lambda row: (
                round(abs(row["theta"] - DEFAULT_THETA_HEADLINE), 9),
                row["theta"]))[field]

        summary["accuracy40"] = _pick("accuracy")
        summary["agreement40"] = _pick("agreement")

    def _numeric(value):
        return value if isinstance(value, (int, float)) and not isinstance(
            value, bool) else None

    pass_rates = report.get("pass_rates")
    if isinstance(pass_rates, dict):
        summary["new_pass"] = _numeric(pass_rates.get("new_macro"))
        summary["hit_pass"] = _numeric(pass_rates.get("hit_macro"))
    new_ratio = report.get("new_ratio")
    if isinstance(new_ratio, dict):
        summary["new_ratio"] = _numeric(new_ratio.get("macro"))
    return summary
