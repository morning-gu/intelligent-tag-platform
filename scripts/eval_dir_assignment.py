"""目录归属评测：现有排序器到底准不准（零标注、可回归）。

用法（仓库根目录）：

    python scripts/eval_dir_assignment.py                    # 库内标签留一法
    python scripts/eval_dir_assignment.py --strict            # 只认精确目录
    python scripts/eval_dir_assignment.py --k 3 5 --limit 200
    python scripts/eval_dir_assignment.py --json out.json     # 落盘供前后对比

口径
====

**库内标签留一法**（零标注、随时可跑）：

对每一枚库内标签（有效、非占位、真实目录是可扩展目录）：
1. 用标签名做查询向量（`role="query"`）；
2. 用**其余**标签名拼出的目录文本做候选向量（`role="passage"`），排除
   被评测的那一枚——否则它自己的名字就写在目录文本的"已收录"清单里，
   归属是白送的（这是这套评测最容易踩的自欺）；
3. 按余弦降序取 top_n，与"这枚标签真实的 dir_id"比对打分。

这条口径测的不是"模型有多聪明"，而是**目录体系本身的分辨力**：目录描述
写得清不清楚、动态目录之间有没有重叠。它完全确定性（同一份库 + 同一个
模型永远同一个分数），因此可直接当回归基线——改提示词、改目录描述、换
嵌入模型，分数掉下去就是真掉下去了。

指标解读（实现见 `tagplatform/eval/ranking.py`）
=================================================

- **NDCG@3/@5**：主指标。分级相关度——预测到精确目录得 2 分，到同父
  兄弟目录得 1 分，其余 0 分（目录树有层级：`dir_genre` 与 `dir_theme`
  都挂在 `dir_content` 下，"差一点"和"完全错"该分开算）；
- **HitRate@k**：前 k 条里有没有命中（业务上等价于"人工看一眼就能选到"）；
- **MRR**：命中的目录排在第几位（决定人工要往下翻几条）；
- **Random Baseline**：同候选集、同相关度下均匀随机排序的期望值，逐样本
  解析计算再宏平均。**没有这条基线，绝对分数不可解读**——候选只有 3 个
  目录时 NDCG@5 天然就高；
- **95% 置信带**：自举得出，用来看"与随机基线是否真的分得开"；
- **不可归属率**：真实目录不是可扩展目录的样本占比，单独报，不混进平均
  （这些标签的归属问题不是排序器能解决的）。

`--strict` 把所有非精确目录降为 0 分，得到"Top-1 严格准确率"；默认口径
（宽松）把同父兄弟目录算半分，衡量"大方向对不对"。

运行开销与模型选择
==================

编码只发生**两批**：① 全部标签名（`role="query"`）② 目录基座 + 每枚标签
"去掉自己"的变体（`role="passage"`，按 64 条一批）。变体是可枚举且稀疏
的，所以批次上限与标签数无关——早期实现每枚标签单跑一次编码，514 枚标签
要 514 次模型前向，在 CPU 上看起来像卡死。

**默认配置的 `BAAI/bge-m3` 在本机可能直接崩**（可用内存不足 2 GB 时，
568M 参数的前向会触发 0xC0000005 原生访问冲突，进程无 Python 栈直接退
出）。评测时改用轻量中文模型即可，脚本本身不挑模型：

    TAGPLT_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5 python scripts/eval_dir_assignment.py

注意：不同嵌入模型的分数**不可直接比较**，只在自己模型内部做回归。

安全设计：本脚本**只读**，不写库、不改标签库、不落任何状态。

分层红线：脚本可用平台模块，但指标全在 `tagplatform/eval`（纯标准库）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from tagplatform.db import create_engine_for, make_session_factory
from tagplatform.embed import make_embedder
from tagplatform.eval import (
    GRADE_EXACT,
    GRADE_NONE,
    GRADE_SIBLING,
    EvaluationReport,
    RankingSample,
    bootstrap_ci,
    evaluate_samples,
    lift,
    random_baseline,
)
from tagplatform.library import (
    _DIR_SAMPLE_TAGS,
    TagLibrary,
    _is_placeholder,
    dir_text,
    is_deprecated,
    library_from_session,
)
from tagplatform.settings import get_settings

# 与 `rank_dir_assignments` 的 top_n 对齐：候选列表就这么多条
TOP_N = 5


# ---------------------------------------------------------------- 数据构造

def _dir_tag_lists(library: TagLibrary) -> dict[str, list[tuple[str, str]]]:
    """每个目录下的（tag_id, tag_name），按库内顺序，已过滤废弃与占位。"""
    buckets: dict[str, list[tuple[str, str]]] = {}
    for tag in library.tags():
        if is_deprecated(tag.status) or _is_placeholder(tag.tag_name):
            continue
        buckets.setdefault(tag.dir_id, []).append((tag.tag_id, tag.tag_name))
    return buckets


def _dir_texts_and_variants(library: TagLibrary, dirs) -> tuple[list[str], dict]:
    """目录文本 + "去掉某枚标签"的变体文本（防泄漏）。

    留一法的泄漏点：目录文本的"已收录："清单里如果还留着被评测的那一枚，
    归属就是白送的。因此每枚标签都要用"去掉自己"的文本。

    但变体是**可枚举且稀疏**的——只有当被评测标签落在该目录前
    `_DIR_SAMPLE_TAGS` 枚内时，文本才会变（共 ≤ 目录数 × 12 条）。所以
    这里一次算全所有变体，交给调用方一次性批量编码，避免"每标签一次模型
    前向"这种把评测算成几小时的写法。
    """
    lists = _dir_tag_lists(library)
    base_texts: list[str] = []
    variants: dict[tuple[str, str], str] = {}
    for directory in dirs:
        items = lists.get(directory.dir_id, [])
        base_texts.append(dir_text(directory, [name for _, name in items[:_DIR_SAMPLE_TAGS]]))
        for index, (tag_id, _name) in enumerate(items[:_DIR_SAMPLE_TAGS]):
            # 去掉自己后从后一位回填，等价于"扫描全部标签时跳过这一枚"
            rest = items[:index] + items[index + 1:]
            variants[(directory.dir_id, tag_id)] = dir_text(
                directory, [name for _, name in rest[:_DIR_SAMPLE_TAGS]])
    return base_texts, variants


def _relevance(library: TagLibrary, truth_dir: str, candidates: list[str],
               *, strict: bool) -> dict[str, float]:
    """分级相关度表：精确 = 2，同父兄弟 = 1（strict 时为 0），其余 0。"""
    truth_node = library.directory(truth_dir)
    truth_parent = truth_node.parent_id if truth_node else None
    table: dict[str, float] = {}
    for dir_id in candidates:
        if dir_id == truth_dir:
            table[dir_id] = GRADE_EXACT
            continue
        node = library.directory(dir_id)
        same_parent = (truth_parent is not None
                       and node is not None and node.parent_id == truth_parent)
        table[dir_id] = (GRADE_NONE if strict else GRADE_SIBLING) if same_parent else GRADE_NONE
    return table


def _rank_scores(scores: np.ndarray, ids: list[str]) -> tuple[str, ...]:
    """余弦降序取 top_n 的目录 id。"""
    if scores.size == 0:
        return ()
    order = np.argsort(-scores)[:TOP_N]
    return tuple(ids[int(i)] for i in order)


def _encode(embedder, texts: list[str], *, role: str, batch: int = 64) -> np.ndarray:
    """分批编码：把 N 次前向压成 ceil(N/batch) 次，避免 CPU 上跑到几小时。"""
    chunks = [np.asarray(embedder.embed(texts[start:start + batch], role=role),
                         dtype=np.float32)
              for start in range(0, len(texts), batch)]
    return np.vstack(chunks) if chunks else np.zeros((0, 0), dtype=np.float32)


def library_samples(library: TagLibrary, embedder, dir_ids: list[str] | None,
                    limit: int | None, *, strict: bool):
    """库内标签留一法：逐标签预测它自己所属的目录。"""
    dirs = library.new_tag_dirs(dir_ids)  # 候选集 = 可扩展目录
    candidate_ids = [d.dir_id for d in dirs]
    if not candidate_ids:
        return [], ["标签库里没有可扩展目录（dir_dynamic），无法评测"]

    tags = [t for t in library.tags_in_dirs(dir_ids)
            if not is_deprecated(t.status) and not _is_placeholder(t.tag_name)]
    if limit:
        tags = tags[:limit]
    if not tags:
        return [], ["范围内没有有效标签，无法评测"]

    unassignable = [t.tag_name for t in tags if t.dir_id not in candidate_ids]
    usable = [t for t in tags if t.dir_id in candidate_ids]
    if not usable:
        return [], [f"范围内 {len(tags)} 枚标签的真实目录都不是可扩展目录"
                    f"（不可归属 {len(unassignable)} 枚），没有可评测样本"]

    # 编码只在两批上发生：① 全部标签名 ② 目录基座 + 全部"去自己"变体。
    # 否则 514 枚标签 × 每个一次模型前向，BGE-M3 在 CPU 上要跑几小时。
    queries = _encode(embedder, [t.tag_name for t in usable], role="query")

    base_texts, variants = _dir_texts_and_variants(library, dirs)
    variant_keys = list(variants)
    passages = _encode(embedder, base_texts + [variants[key] for key in variant_keys],
                       role="passage")
    base_matrix = passages[:len(base_texts)]
    variant_rows = {key: passages[len(base_texts) + i]
                    for i, key in enumerate(variant_keys)}

    truth_index = {dir_id: i for i, dir_id in enumerate(candidate_ids)}
    samples: list[RankingSample] = []
    for tag, vector in zip(usable, queries):
        home = truth_index.get(tag.dir_id)
        if home is None:
            continue
        # 只替换被评测标签所在目录那一行，其余目录与基座完全相同
        matrix = base_matrix.copy()
        own_text = variants.get((tag.dir_id, tag.tag_id))
        if own_text is not None:
            matrix[home] = variant_rows[(tag.dir_id, tag.tag_id)]
        samples.append(RankingSample(
            ranked=_rank_scores(matrix @ vector, candidate_ids),
            relevance=_relevance(library, tag.dir_id, candidate_ids, strict=strict),
            label=tag.tag_name,
        ))

    warnings = []
    if unassignable:
        ratio = len(unassignable) / len(tags)
        warnings.append(
            f"不可归属 {len(unassignable)}/{len(tags)} 枚（{ratio:.0%}）：真实目录"
            f"不是可扩展目录，已排除出平均——例如 {', '.join(unassignable[:5])}")
    return samples, warnings


def _fmt(value: float | None) -> str:
    return "暂无" if value is None else f"{value:.3f}"


def _ci_text(raw: list[float]) -> str:
    ci = bootstrap_ci(raw)
    return f"[{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "-"


def _baselines(samples: list[RankingSample], k_values, n_candidates: int) -> dict[str, float]:
    """逐样本解析计算随机基线，再宏平均（与上方宏平均口径一致）。"""
    result: dict[str, float] = {}
    if not samples:
        return result
    for k in k_values:
        values = [random_baseline(n_candidates, s.relevance, k) for s in samples]
        for name in ("hit_rate", "ndcg", "mrr"):
            result[f"{name}@{k}"] = sum(v[name] for v in values) / len(values)
    return result


def print_report(report: EvaluationReport, samples: list[RankingSample],
                 n_candidates: int, extra_warnings: list[str]) -> None:
    print(f"\n样本数 {report.n_samples}　候选目录 {n_candidates} 个")
    for message in extra_warnings + report.warnings:
        print(f"  ! {message}")
    if not report.n_samples:
        return

    baseline = _baselines(samples, report.k_values, n_candidates)
    header = (f"{'指标':<14}{'值':>8}{'95% CI':>18}{'随机基线':>11}{'提升':>9}")
    print()
    print(header)
    print("-" * 60)
    rows = []
    for k in report.k_values:
        for name, label in (("hit_rate", "HitRate"), ("ndcg", "NDCG"),
                            ("precision", "Precision"), ("recall", "Recall"),
                            ("mrr", "MRR")):
            rows.append((f"{label}@{k}", name, k))
    for text, name, k in rows:
        value = report.metric(name, k)
        raw = report.raw(name, k)
        base = baseline.get(f"{name}@{k}")
        ratio = lift(value, base)
        ratio_text = f"×{ratio:.2f}" if ratio is not None else "-"
        print(f"{text:<14}{_fmt(value):>8}{_ci_text(raw):>18}"
              f"{_fmt(base):>11}{ratio_text:>9}")


def build_payload(report: EvaluationReport, samples: list[RankingSample],
                  n_candidates: int, warnings: list[str], args) -> dict:
    metrics = {}
    for k in report.k_values:
        for name in ("hit_rate", "recall", "precision", "ndcg", "mrr"):
            metrics[f"{name}@{k}"] = report.metric(name, k)
    return {
        "strict": args.strict,
        "database": get_settings().database_path,
        "embedding_model": get_settings().embedding_model,
        "n_samples": report.n_samples,
        "n_candidates": n_candidates,
        "metrics": metrics,
        "random_baseline": _baselines(samples, report.k_values, n_candidates),
        "ci95": {key: bootstrap_ci(values)
                 for key, values in report.per_sample.items()},
        "warnings": warnings + report.warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="目录归属排序评测（只读）")
    parser.add_argument("--k", type=int, nargs="+", default=[3, 5],
                        help="截断位置（默认 3 5；不要超过候选列表长度 5）")
    parser.add_argument("--strict", action="store_true",
                        help="只认精确目录（同父兄弟目录不再算半分）")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多评测多少条样本（抽查用）")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="把结果写成 JSON（供改动前后对比）")
    args = parser.parse_args(argv)

    settings = get_settings()
    session = make_session_factory(create_engine_for(settings.database_path))()
    try:
        library = library_from_session(session)
        n_dirs = len(library.dirs())
        n_dynamic = len(library.new_tag_dirs(None))
        embedder = make_embedder(settings.embedding_model,
                                 mock=settings.use_mock_embedder)
        samples, warnings = library_samples(library, embedder, None,
                                            args.limit, strict=args.strict)
        n_candidates = n_dynamic

        print(f"严格口径 {args.strict}　库 {settings.database_path}")
        print(f"目录 {n_dirs} 个（可扩展 {n_dynamic}），标签 {len(library.tags())} 枚")

        report = evaluate_samples(samples, tuple(args.k))
        print_report(report, samples, n_candidates, warnings)

        if args.json_path:
            payload = build_payload(report, samples, n_candidates, warnings, args)
            Path(args.json_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n已写入 {args.json_path}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())