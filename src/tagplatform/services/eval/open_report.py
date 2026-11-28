"""开放评测报告组装 / 渲染 / 落盘 / 历史摘要（三维评审口径）。

2026-11-23 三维化（open-judge-quality）：θ 网格 / 集合准确率 / nDCG /
随机与先验基线 / 标定全部移除——那些是闭环检测的口径，开放生成没有
gt 排序任务。报告改为分支式内容组织：汇总卡（通过率 + 三维均分/满分率
+ 新词占比）→ 评分分布 → 逐片明细（每标签三维分 + 理由 + 摘录 + judge
参考）→ 异常表 → judge 审计与数量漏斗聚合。

输入是 OpenMovieResult 列表（干跑运行器的原子产出）：hits/new_words
行带 review = {"pass","relevance","semantics","value","reason","evidence_scene_ids","evaluated"}
（三维各 {"score","reason"}，pass 和总评由评审模型直接给出）或 None（该片
评审失败，产出不作废）。三维汇总复用 services/eval/tag_quality 的
quality_metrics（与生产批次汇总同一套指标机制）。

分层红线：本层不 import fastapi；OpenMovieResult 以 TYPE_CHECKING 引用
防循环。读侧（history → _read_open_summary）对缺失/截断的 report.json
按「该 run 无报告」容错（宁缺勿假）。
"""
from __future__ import annotations

import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from tagplatform.services.eval.report import (
    BANNER_PARTIAL,
    _dump_json,
)
from tagplatform.services.eval.tag_quality import MAX_SCORE, quality_metrics
from tagplatform.services.generation.open.mtg.pipeline import is_filter_stage_drop
from tagplatform.services.tags.value_scoring import format_score, score_bucket

if TYPE_CHECKING:
    from tagplatform.services.eval.open_service import OpenMovieResult

OPEN_REPORT_SCHEMA_VERSION = 3

__all__ = [
    "assemble_open_report",
    "fill_open_report_mounts",
    "render_open_report_md",
    "upgrade_open_report",
    "write_open_report",
]

# 评审和生成共享模型时提示可能的判断偏差。
BANNER_SAME_MODEL = ("评审与生成复用同一模型，可能共享判断偏差，"
                     "绝对值需打折解读")
# 小样本警示（失败核算三类分开点名）
_BANNER_SMALL_SAMPLE = ("小样本警示：{detail}，占比超 10%——成功样本少，"
                        "指标波动大，勿单独引用")
# judge 审计整体缺失警示（audit/ 由 mtg 管线经 run 级 settings 副本落进
# run 目录，缺失 = 旧管线/替身产出，宁缺勿假）
_JUDGE_MISSING = "评审官审计（audit/）缺失或为空，judge 段无数据"

# 目录对齐分档的三个已知档位（catalog_align 的 BAND_* 常量）
_BAND_KEYS = ("duplicate", "suspect", "new")
# judge 三维评分轴（与 mtg.verdicts.parse_verdicts / pipeline 阈值互为协议）
_SCORE_AXES = ("relevance", "semantics", "value")

# metrics.csv 长表列（Excel/后续分析消费；None → 空格）
_CSV_HEADER = ("metric", "value", "n")
OPEN_MOUNT_FIELDS = ("dir_id", "dir_name", "target_type")


def _rate(passed: int, judged: int) -> float | None:
    """通过率微口径：分母只计有判定（pass 非 None）的行；无判定 → None。"""
    return passed / judged if judged else None


def _group_stats(rows: list[dict]) -> dict:
    """一组产出行的评审统计：pass_rate / review_missing / metrics。

    rows 为 hits 或 new_words 行（review 可能 None = 该片评审失败，整组
    排除出分母）。metrics 用 quality_metrics（固定五档），输入直接是
    review dict（其 relevance/semantics/value 即 {"score","reason"}）。
    """
    reviews = [row["review"] for row in rows
               if row.get("review") is not None and row["review"].get("evaluated") is True]
    passed = sum(review["pass"] is True for review in reviews)
    judged = sum(review["pass"] is not None for review in reviews)
    missing = sum(row.get("review") is not None and not row["review"].get("evaluated")
                  for row in rows)
    pending = sum(row.get("review") is None for row in rows)
    return {
        "pass_rate": _rate(passed, judged), "passed": passed, "judged": judged,
        "review_missing": missing, "pending": pending,
        "three_judged": sum(all(review[axis]["score"] is not None for axis in _SCORE_AXES)
                            for review in reviews),
        "metrics": quality_metrics(len(rows), reviews).model_dump() if rows else None,
    }


def _movie_row(result: "OpenMovieResult") -> dict:
    """单片明细行：产出原样（含三维评审与 judge 参考）+ 片级统计。"""
    hits = result.hits
    new_words = result.new_words
    all_rows = hits + new_words
    stats = _group_stats(all_rows)
    return {
        "tt": result.tt,
        "title": result.title,
        "ok": result.ok,
        "error": result.error,
        "review_error": result.review_error,
        "elapsed_ms": result.elapsed_ms,
        "n_hits": len(hits),
        "n_new_words": len(new_words),
        "hits": hits,
        "new_words": new_words,
        "pass_rate": stats["pass_rate"],
        "new_pass": _group_stats(new_words)["pass_rate"],
        "hit_pass": _group_stats(hits)["pass_rate"],
        "review_missing": stats["review_missing"] + (stats["pending"] if result.review_error else 0),
        "review_pending": stats["pending"] if not result.review_error else 0,
        "metrics": stats["metrics"],
    }


def fill_open_report_mounts(movies: list[dict], audit_dir: Path) -> None:
    """用原运行审计补齐旧标签行的挂载字段，保留报告及目录历史口径。"""
    groups = (("hits", "tag_name", "catalog"), ("new_words", "name", "generated"))
    for movie in movies:
        if not isinstance(movie, dict):
            continue
        tt = movie.get("tt")
        if not isinstance(tt, str) or not re.fullmatch(r"tt[0-9]+", tt):
            continue
        if not any("dir_id" not in row or "target_type" not in row
                   for key, _, _ in groups for row in movie.get(key, [])):
            continue
        try:
            audit = json.loads((audit_dir / f"{tt}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        accepted = audit.get("accepted") if isinstance(audit, dict) else None
        if not isinstance(accepted, list):
            continue
        mounts = defaultdict(list)
        for row in accepted:
            if (isinstance(row, dict) and row.get("origin") in ("catalog", "generated")
                    and isinstance(row.get("tag"), str)):
                mounts[row.get("origin"), row["tag"]].append(row)
        for key, name_key, origin in groups:
            rows = list(movie.get(key, []))
            indexes = defaultdict(list)
            for index, row in enumerate(rows):
                indexes[row.get(name_key)].append(index)
            for name, positions in indexes.items():
                matches = mounts.get((origin, name), [])
                # 产出与审计同序，报告只是按来源分组。缺行时避免猜测同名词归属。
                if len(matches) != len(positions):
                    continue
                for index, match in zip(positions, matches, strict=True):
                    rows[index] = {**{field: match[field] for field in OPEN_MOUNT_FIELDS
                                      if field in match}, **rows[index]}
            movie[key] = rows


def _judge_score_summary(verdict_rows: list[dict]) -> dict:
    """judge 三维分跨片聚合：逐维均分（保留精确小数）/ 分布（0-4 四舍五入归档）/ null 计数。"""
    summary = {}
    for axis in _SCORE_AXES:
        values = [row.get(axis) for row in verdict_rows]
        scored = [float(v) for v in values
                  if isinstance(v, (int, float)) and not isinstance(v, bool)]
        distribution = [0] * (MAX_SCORE + 1)
        for v in scored:
            distribution[score_bucket(v, MAX_SCORE)] += 1
        summary[axis] = {
            "n": len(scored),
            "average": (sum(scored) / len(scored)) if scored else None,
            "distribution": distribution,
            "null": len(values) - len(scored),
        }
    return summary


def _judge_section(audit_dir: Path | None, ok_films: list[str],
                   warnings: list[str]) -> dict:
    """评审官审计跨片聚合（微口径、整 run 汇总；audit/<tt>.json 键形见
    mtg pipeline._build_report）。

    - `funnel`：生产数量漏斗——逐片求和后整 run 算比率（None≠0）：
      raw（原始生成 Σruns.items）→ merged（两级合并保留）→ verdicts
      （judge 返回判决）→ judge_passed（过小节 4 过滤：merged − 过滤段
      淘汰，按 is_filter_stage_drop 区分过滤段与目录归属段）→ cluster_kept
      （一阶段聚类代表及二阶段通过项）→ rank_kept（配额保留）→ final（目录
      归属成功 Σaccepted）。比率七项：合并保留率 / judge 返回覆盖率 /
      judge 通过率（按合并候选与按已评分候选两个分母）/ 排序配额保留率 /
      目录挂载成功率 / 最终生产保留率。审计无漏斗键（替身/旧管线）→
      None；
    - `scores`：judge 三维分聚合（均分/分布/null）；
    - `dropped`：drop 原因（why 字段）计数；
    - `bands`：目录对齐分档计数（library.align 的 band 字段）。
    容错：文件缺失/损坏/非 dict → 跳过该片贡献；有 ok 片但一篇审计都
    读不到 → 空段 + warnings 追加警示。
    """
    totals = dict.fromkeys(("raw", "merged", "verdicts", "judge_passed",
                            "cluster_kept", "rank_kept", "final"), 0)
    funnel_contributed = contributed = 0
    has_clustering = False
    verdict_rows: list[dict] = []
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
            verdicts = audit.get("verdicts")
            runs = audit.get("runs")
            merged_n = len(merged) if isinstance(merged, list) else 0
            accepted_n = len(accepted) if isinstance(accepted, list) else 0
            verdicts_n = len(verdicts) if isinstance(verdicts, list) else 0
            raw_n = (sum(row["items"] for row in runs
                         if isinstance(row, dict)
                         and isinstance(row.get("items"), int))
                     if isinstance(runs, list) else 0)
            if any(isinstance(audit.get(key), list)
                   for key in ("runs", "merged", "verdicts", "accepted")):
                funnel_contributed += 1
            filter_dropped = 0
            for row in audit.get("dropped") or []:
                if isinstance(row, dict):
                    reason = str(row.get("why") or "未知")
                    dropped[reason] = dropped.get(reason, 0) + 1
                    if is_filter_stage_drop(reason):
                        filter_dropped += 1
            judge_passed = max(0, merged_n - filter_dropped)
            cluster_kept = judge_passed
            clustering = audit.get("clustering")
            if (isinstance(clustering, dict) and isinstance(clustering.get("input"), int)
                    and isinstance(clustering.get("clusters"), list)):
                has_clustering = True
                pruned = max(0, clustering["input"] - len(clustering["clusters"]))
                cluster_kept = max(0, judge_passed - pruned)
            library = audit.get("library")
            align = library.get("align") if isinstance(library, dict) else None
            truncated = (library.get("truncated")
                         if isinstance(library, dict) else None)
            truncated_n = len(truncated) if isinstance(truncated, list) else 0
            totals["raw"] += raw_n
            totals["merged"] += merged_n
            totals["verdicts"] += verdicts_n
            totals["judge_passed"] += judge_passed
            totals["cluster_kept"] += cluster_kept
            totals["rank_kept"] += max(0, cluster_kept - truncated_n)
            totals["final"] += accepted_n
            for row in verdicts or []:
                if isinstance(row, dict):
                    verdict_rows.append(row)
            for row in align or []:
                if isinstance(row, dict) and row.get("band") is not None:
                    band = str(row["band"])
                    bands[band] = bands.get(band, 0) + 1
    if ok_films and contributed == 0:
        warnings.append(_JUDGE_MISSING)
    funnel = None
    if funnel_contributed:
        funnel = {
            **totals,
            "merge_keep_rate": _rate(totals["merged"], totals["raw"]),
            "judge_coverage": _rate(totals["verdicts"], totals["merged"]),
            "judge_pass_rate": _rate(totals["judge_passed"], totals["merged"]),
            "judge_pass_rate_scored": _rate(totals["judge_passed"],
                                            totals["verdicts"]),
            "rank_keep_rate": _rate(totals["rank_kept"], totals["cluster_kept"]),
            "dir_mount_rate": _rate(totals["final"], totals["rank_kept"]),
            "final_keep_rate": _rate(totals["final"], totals["merged"]),
        }
        if has_clustering:
            funnel["cluster_keep_rate"] = _rate(totals["cluster_kept"], totals["judge_passed"])
        else:
            funnel.pop("cluster_kept")
    return {
        "funnel": funnel,
        "scores": _judge_score_summary(verdict_rows),
        "dropped": dropped,
        "bands": bands,
    }


def _banner(meta: dict, total: int, done: int, n_failed: int,
            n_review_failed: int) -> list[str]:
    """banner 聚合（list[str]）：部分运行 → 同模评审 → 小样本。

    部分运行口径与闭环同款：status 未 succeeded 或 done < total 一律不可
    与全量横比（覆盖 cancelled/aborted 与 worker 兜底 failed、取消后余量
    未跑的 done<total）。
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
        if parts:
            banner.append(_BANNER_SMALL_SAMPLE.format(detail="；".join(parts)))
    return banner


def assemble_open_report(results: list["OpenMovieResult"], meta: dict, *,
                         audit_dir: Path | None = None) -> dict:
    """干跑结果 → report.json 结构（分支式内容组织，report.md 同数据）。

    汇总口径：全部 ok 片产出计 total_tags / new_ratio；评审成功片的产出
    计通过率与三维指标。覆盖率的分母包括评审失败片的产出。
    `audit_dir` = run 目录的 audit/ 子目录（None = 无审计，judge 空段 +
    警示）。行序按 tt 稳定排序——运行器 results 是线程池完成序，报告
    落盘要可跨 run 对比，不随并发调度漂移。
    """
    results = sorted(results, key=lambda r: r.tt)
    counts: list[dict] = []
    movies: list[dict] = []
    ok_films: list[str] = []
    n_failed = 0
    n_review_failed = 0
    all_rows: list[dict] = []
    review_failed_tags = 0
    new_total = hit_total = 0
    for result in results:
        if result.ok:
            ok_films.append(result.tt)
            counts.append({"tt": result.tt, "hits": len(result.hits),
                           "new_words": len(result.new_words)})
            rows = result.hits + result.new_words
            all_rows.extend(rows)
            new_total += len(result.new_words)
            hit_total += len(result.hits)
            if result.review_error is not None:
                n_review_failed += 1
                review_failed_tags += sum(row.get("review") is None for row in rows)
        else:
            n_failed += 1
        movies.append(_movie_row(result))

    if audit_dir is not None:
        fill_open_report_mounts(movies, audit_dir)

    total = meta.get("total")
    if not isinstance(total, int):
        total = len(results)
    done = meta.get("done")
    if not isinstance(done, int):
        done = len(results)
    warnings: list[str] = []
    if meta.get("warning"):
        warnings.append(str(meta["warning"]))

    overall = _group_stats(all_rows)
    new_stats = _group_stats([r for r in all_rows if "name" in r])
    hit_stats = _group_stats([r for r in all_rows if "tag_id" in r])
    totals = [c["hits"] + c["new_words"] for c in counts]
    metrics = overall["metrics"]
    return {
        "schema_version": OPEN_REPORT_SCHEMA_VERSION,
        "status": meta.get("status"),
        "source_run_id": meta.get("source_run_id"),
        "banner": _banner(meta, total, done, n_failed, n_review_failed),
        "sample_accounting": {
            "total": total,
            "ok": len(ok_films),
            "failed": n_failed,
            "review_failed": n_review_failed,
        },
        "summary": {
            "total_tags": len(all_rows),
            "reviewed_tags": metrics["evaluated_tags"] if metrics else 0,
            "passed_tags": overall["passed"],
            "judged_tags": overall["judged"],
            "new_judged_tags": new_stats["judged"],
            "hit_judged_tags": hit_stats["judged"],
            "three_judged_tags": overall["three_judged"],
            "review_pending": overall["pending"] - review_failed_tags,
            "hits": hit_total,
            "new_words": new_total,
            "new_ratio": (new_total / len(all_rows)) if all_rows else None,
            "pass_rate": overall["pass_rate"],
            "new_pass": new_stats["pass_rate"],
            "hit_pass": hit_stats["pass_rate"],
            "review_missing": overall["review_missing"] + review_failed_tags,
            "metrics": metrics,
        },
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
            "review_config": meta.get("review") or {},
            "review_prompt_version": (meta.get("review") or {}).get(
                "prompt_version"),
            "library_version": (meta.get("library") or {}).get("version"),
            "dataset_id": (meta.get("dataset") or {}).get("id"),
            "modalities": meta.get("modalities"),
        },
        "movies": movies,
        "failed_movies": [{"tt": r.tt, "error": r.error}
                          for r in results if not r.ok],
        "review_failed_movies": [{"tt": r.tt, "review_error": r.review_error}
                                 for r in results
                                 if r.ok and r.review_error is not None],
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 渲染

def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_cal(value) -> str:
    return "—" if value in (None, "") else str(value)


def _score_cell(review: dict | None, axis: str) -> str:
    if review is None:
        return "—"
    dim = review.get(axis) or {}
    return format_score(dim.get("score"))


def render_open_report_md(report: dict) -> str:
    """report.json → Markdown（与前端同数据；下载/离线阅读用）。"""
    summary = report.get("summary") or {}
    metrics = summary.get("metrics") or {}
    acc = report.get("sample_accounting") or {}
    lines = ["# 开放生成评测报告", ""]
    for banner in report.get("banner") or []:
        lines.append(f"> ⚠ {banner}")
    if report.get("banner"):
        lines.append("")
    lines += [
        "## 汇总",
        "",
        f"- 影片：{acc.get('ok', 0)} 成功 / {acc.get('failed', 0)} produce 失败"
        f" / {acc.get('review_failed', 0)} 评审失败（共 {acc.get('total', 0)}）",
        f"- 产出标签：{summary.get('total_tags', 0)}（新词 {summary.get('new_words', 0)}"
        f" / 库内命中 {summary.get('hits', 0)}），已评审 {summary.get('reviewed_tags', 0)}",
        f"- 新词占比：{_pct(summary.get('new_ratio'))}",
        f"- 评审通过率：{_pct(summary.get('pass_rate'))}"
        f"（新词 {_pct(summary.get('new_pass'))} / 命中 {_pct(summary.get('hit_pass'))}）",
        f"- 评审漏判：{summary.get('review_missing', 0)} 条",
    ]
    if metrics:
        lines.append(f"- 评分覆盖：{_pct(metrics.get('coverage'))}"
                     f"，无法判断 {metrics.get('undetermined_tags', 0)} 条")
        for axis, label in (("relevance", "内容相关性"),
                            ("semantics", "语义准确性"), ("value", "标签价值")):
            dim = metrics.get(axis) or {}
            lines.append(f"- {label}：均分 {_fmt(dim.get('average'))}"
                         f" / 满分率 {_pct(dim.get('high_rate'))}")
        lines.append(f"- 三维满分率：{_pct(metrics.get('three_dimension_high_rate'))}")
    lines += ["", "## 评分分布（评审，0-4）", "",
              "| 维度 | 0 | 1 | 2 | 3 | 4 | null | 均分 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for axis, label in (("relevance", "相关性"),
                        ("semantics", "语义"), ("value", "价值")):
        dim = metrics.get(axis) or {}
        dist = dim.get("distribution") or [0] * 5
        lines.append(f"| {label} | " + " | ".join(str(d) for d in dist)
                     + f" | {dim.get('undetermined', 0)} | {_fmt(dim.get('average'))} |")
    judge = report.get("judge") or {}
    lines += ["", "## judge 审计与生产漏斗", ""]
    funnel = judge.get("funnel")
    if funnel:
        rank_input = funnel.get("cluster_kept", funnel["judge_passed"])
        lines += [
            f"- 原始生成 → 合并保留：{funnel['merged']}/{funnel['raw']}"
            f"（{_pct(funnel.get('merge_keep_rate'))}）",
            f"- Judge 返回覆盖率：{funnel['verdicts']}/{funnel['merged']}"
            f"（{_pct(funnel.get('judge_coverage'))}）",
            f"- Judge 通过率：按合并候选 {funnel['judge_passed']}/{funnel['merged']}"
            f"（{_pct(funnel.get('judge_pass_rate'))}）/ 按已评分候选"
            f" {funnel['judge_passed']}/{funnel['verdicts']}"
            f"（{_pct(funnel.get('judge_pass_rate_scored'))}）",
            *([f"- 聚类后保留率：{rank_input}/{funnel['judge_passed']}"
               f"（{_pct(funnel.get('cluster_keep_rate'))}）"] if "cluster_kept" in funnel else []),
            f"- 排序配额保留率：{funnel['rank_kept']}/{rank_input}"
            f"（{_pct(funnel.get('rank_keep_rate'))}）",
            f"- 目录挂载成功率：{funnel['final']}/{funnel['rank_kept']}"
            f"（{_pct(funnel.get('dir_mount_rate'))}）",
            f"- 最终生产保留率：{funnel['final']}/{funnel['merged']}"
            f"（{_pct(funnel.get('final_keep_rate'))}）",
        ]
    else:
        lines.append("- 数量漏斗：—（审计缺失）")
    lines += [f"- 分档：{judge.get('bands')}",
              f"- 淘汰原因：{judge.get('dropped')}"]
    jscores = judge.get("scores") or {}
    for axis, label in (("relevance", "相关性"),
                        ("semantics", "语义"), ("value", "价值")):
        dim = jscores.get(axis) or {}
        lines.append(f"- judge {label}：均分 {_fmt(dim.get('average'))}"
                     f"（{dim.get('n', 0)} 条评分，{dim.get('null', 0)} 条 null）")
    lines += ["", "## 逐片明细", ""]
    for movie in report.get("movies") or []:
        status = "ok" if movie.get("ok") else "produce 失败"
        if movie.get("ok") and movie.get("review_error"):
            status = "评审失败（产出保留）"
        lines.append(f"### {movie.get('tt')} {movie.get('title') or ''}（{status}）")
        if movie.get("error"):
            lines.append(f"- produce 错误：{movie['error']}")
        if movie.get("review_error"):
            lines.append(f"- 评审错误：{movie['review_error']}")
        if not movie.get("ok"):
            lines.append("")
            continue
        lines.append(f"- 通过率：{_pct(movie.get('pass_rate'))}"
                     f"（新词 {_pct(movie.get('new_pass'))}"
                     f" / 命中 {_pct(movie.get('hit_pass'))}）")
        rows = ([{**r, "_group": "命中"} for r in movie.get("hits") or []]
                + [{**r, "_group": "新词"} for r in movie.get("new_words") or []])
        if rows:
            lines += ["", "| 组 | 标签 | 生成阶段 | 挂载目录 | 挂载层级 | 相关 | 语义 | 价值 | 通过 | 理由 |",
                      "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
            for row in rows:
                review = row.get("review")
                name = row.get("tag_name") or row.get("name")
                stage = {"scene": "一阶段", "film": "二阶段"}.get(row.get("level"), "—")
                directory = row.get("dir_name") or row.get("dir_id") or "—"
                mount_level = {"content": "全片级", "scene": "场景级", "character": "角色级"}.get(
                    row.get("target_type") or "", "—")
                if review is None:
                    passed = "评审失败" if movie.get("review_error") else "待评"
                elif review["pass"] is None:
                    passed = "无法判断" if review["evaluated"] else "漏判"
                else:
                    passed = "是" if review["pass"] else "否"
                reason = (review or {}).get("reason") or ""
                lines.append(f"| {row['_group']} | {name} | {stage} | {directory} | {mount_level} "
                             f"| {_score_cell(review, 'relevance')} "
                             f"| {_score_cell(review, 'semantics')} "
                             f"| {_score_cell(review, 'value')} "
                             f"| {passed} | {reason} |")
        lines.append("")
    if report.get("failed_movies"):
        lines += ["## produce 失败影片", ""]
        lines += [f"- {m['tt']}：{m['error']}" for m in report["failed_movies"]]
        lines.append("")
    if report.get("review_failed_movies"):
        lines += ["## 评审失败影片（产出保留）", ""]
        lines += [f"- {m['tt']}：{m['review_error']}"
                  for m in report["review_failed_movies"]]
        lines.append("")
    cal = report.get("calibre") or {}
    lines += ["## 口径", "",
              f"- 生成模型：{_fmt_cal(cal.get('llm_model'))}"
              f"（prompt {_fmt_cal(cal.get('prompt_version'))}）",
              f"- 评审模型：{_fmt_cal(cal.get('review_model'))}"
              f"（prompt {_fmt_cal(cal.get('review_prompt_version'))}，"
              f"同模：{'是' if cal.get('review_same_model') else '否'}）",
              f"- 嵌入模型：{_fmt_cal(cal.get('embedding_model'))}",
              f"- 标签库版本：{_fmt_cal(cal.get('library_version'))}",
              f"- 数据集：{_fmt_cal(cal.get('dataset_id'))}"
              f"，模态：{cal.get('modalities')}", ""]
    for warning in report.get("warnings") or []:
        lines.append(f"> ⚠ {warning}")
    return "\n".join(lines) + "\n"


def write_open_report(run_dir: Path, report: dict) -> None:
    """报告三件套落盘：report.json / report.md / metrics.csv。

    长表行 `metric,value,n`：通过率族 + 三维均分/满分率 + 新词占比。
    写序在 meta/raw 之后（worker 终态序），读侧（history →
    _read_open_summary）对缺失/截断的 report.json 容错。
    """
    _dump_json(run_dir / "report.json", report)
    (run_dir / "report.md").write_text(render_open_report_md(report),
                                       encoding="utf-8")
    summary = report.get("summary") or {}
    metrics = summary.get("metrics") or {}
    with open(run_dir / "metrics.csv", "w", encoding="utf-8",
              newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for metric, value, n in (
                ("pass_rate", summary.get("pass_rate"),
                 summary.get("judged_tags")),
                ("new_pass", summary.get("new_pass"), summary.get("new_judged_tags")),
                ("hit_pass", summary.get("hit_pass"), summary.get("hit_judged_tags")),
                ("new_ratio", summary.get("new_ratio"),
                 summary.get("total_tags"))):
            writer.writerow([metric, value if value is not None else "", n])
        for axis in _SCORE_AXES:
            dim = metrics.get(axis) or {}
            writer.writerow([f"{axis}_average",
                             dim.get("average") if dim.get("average") is not None else "",
                             sum(dim.get("distribution") or [])])
            writer.writerow([f"{axis}_high_rate",
                             dim.get("high_rate") if dim.get("high_rate") is not None else "",
                             sum(dim.get("distribution") or [])])
        writer.writerow(["three_dimension_high_rate",
                         metrics.get("three_dimension_high_rate")
                         if metrics.get("three_dimension_high_rate") is not None else "",
                         summary.get("three_judged_tags")])
        # judge 数量漏斗七率（n = 对应分母；审计缺失 → 空值）
        funnel = (report.get("judge") or {}).get("funnel") or {}
        rank_denominator = "cluster_kept" if "cluster_kept" in funnel else "judge_passed"
        if "cluster_kept" in funnel:
            writer.writerow(["cluster_keep_rate", funnel.get("cluster_keep_rate"),
                             funnel.get("judge_passed")])
        for metric, denominator in (("merge_keep_rate", "raw"),
                                    ("judge_coverage", "merged"),
                                    ("judge_pass_rate", "merged"),
                                    ("judge_pass_rate_scored", "verdicts"),
                                    ("rank_keep_rate", rank_denominator),
                                    ("dir_mount_rate", "rank_kept"),
                                    ("final_keep_rate", "merged")):
            value = funnel.get(metric)
            writer.writerow([metric, value if value is not None else "",
                             funnel.get(denominator, "")])


def upgrade_open_report(run_dir: Path) -> dict | None:
    """旧 schema 报告按原始材料重建并重写三件套（读侧惰性升级）。

    report.json 读侧原样透出，schema 升级后旧 run 会被 410 整体拒收。
    这里用与终态组装完全相同的输入（meta.json + raw_results.json +
    audit/）重新 assemble：成功 → 重写三件套并返回新报告；材料缺失/
    损坏/run 非终态 → None（调用方回落原错误语义）。升级幂等——重写后
    schema 即匹配，不会再次触发。
    """
    from tagplatform.services.eval.open_service import OpenMovieResult  # 局部导入防循环

    try:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        rows = json.loads((run_dir / "raw_results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not isinstance(rows, list):
        return None
    if meta.get("status") == "running":
        return None
    try:
        results = [OpenMovieResult(**row) for row in rows]
    except TypeError:
        return None
    report = assemble_open_report(results, meta, audit_dir=run_dir / "audit")
    write_open_report(run_dir, report)
    return report


# ---------------------------------------------------------------- 历史摘要

def _read_open_summary(run_dir: Path) -> dict:
    """report.json → 历史摘要五键（pass_rate / new_pass / hit_pass /
    new_ratio / three_high）。

    直读 summary 与 summary.metrics；report.json 缺失/损坏/缺段 → 对应
    键 None，不报错（宁缺勿假）。
    """
    summary = {"pass_rate": None, "new_pass": None, "hit_pass": None,
               "new_ratio": None, "three_high": None}
    try:
        report = json.loads((run_dir / "report.json")
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return summary
    if not isinstance(report, dict):
        return summary
    if report.get("schema_version") != OPEN_REPORT_SCHEMA_VERSION:
        # 旧 schema：按原始材料惰性升级（与报告端点同源）；失败按无报告
        report = upgrade_open_report(run_dir)
        if report is None:
            return summary

    def _numeric(value):
        return value if isinstance(value, (int, float)) and not isinstance(
            value, bool) else None

    head = report.get("summary")
    if isinstance(head, dict):
        summary["pass_rate"] = _numeric(head.get("pass_rate"))
        summary["new_pass"] = _numeric(head.get("new_pass"))
        summary["hit_pass"] = _numeric(head.get("hit_pass"))
        summary["new_ratio"] = _numeric(head.get("new_ratio"))
        metrics = head.get("metrics")
        if isinstance(metrics, dict):
            summary["three_high"] = _numeric(
                metrics.get("three_dimension_high_rate"))
    return summary
