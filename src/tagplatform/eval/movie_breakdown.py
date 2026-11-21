"""单片 badcase 指标：raw_results.json 行 → 逐片 P/R/Spearman 与明细三表（纯函数）。

对账口径：与闭环网格（`closed_ranking.evaluate_closed`）共用同一套原语
（`ranking.precision_at_k` / `recall_at_k`、`closed_ranking.spearman`）与
同一样本构造（ranked=计分清单 tuple、relevance=gt∩库、system_scores=产出
sf），因此单片指标与网格格值可精确对账（test_reconciliation_with_grid 钉
死）；该 θ 下相关集为空的影片，网格靠 valid_theta 预滤、单片自管 null，
两侧都不进平均。增补（2026-11-22）：P 门与 R 门可各自独立取 θ——p 对账
(θ_p, K_P) 格、r 对账 (θ_r, K_R) 格；n_relevant/hit/misses 一律跟 θ_r
（漏检=召回侧语义）；Spearman 与 θ 无关。缺省取格走统一约定
（DEFAULT_THETA_P=0.2 / DEFAULT_THETA_R=0.8，见 closed_ranking）。
已知取舍：precision/recall 原语对空相关集记 0.0，本模块先判 θ-相关集为空
→ None、非空才调（None≠0）；计分清单与明细顺序防御性按 (-sf, tag_id) 重
排，不信任输入序。

分层红线：只用标准库 + 上述两个 eval 模块，不 import fastapi / numpy /
scipy（与 closed_ranking.py 同款约束）。
"""

from __future__ import annotations

from typing import Sequence

from tagplatform.eval.closed_ranking import (
    DEFAULT_K_P,
    DEFAULT_K_R,
    DEFAULT_THETA_P,
    DEFAULT_THETA_R,
    spearman,
)
from tagplatform.eval.ranking import precision_at_k, recall_at_k

__all__ = [
    "breakdown_movie",
    "pick_k",
    "pick_theta",
    "summarize_movies",
]

# θ/k 选取目标统一走 closed_ranking 的取格约定常量（2026-11-22：
# P/nDCG@K=10 → θ≈0.2、R@K=20 → θ≈0.8；并列取小）


def pick_theta(thetas: Sequence[float], target: float) -> float:
    """选最接近 target 的 θ；距离并列取小（确定性，防实现漂移）。

    距离先按 9 位小数取整再比——浮点表示噪声（如 0.6-0.4=0.1999…96
    比 0.4-0.2=0.2000…01 略小）会把"并列取小"击穿，不取整不行。
    """
    return min(thetas,
               key=lambda t: (round(abs(t - target), 9), t))


def pick_k(ks: Sequence[int], target: int) -> int:
    """选最接近 target 的 k；距离并列取小。"""
    return min(ks, key=lambda k: (abs(k - target), k))


# ---------------------------------------------------------------- 内部口径

def _gt_space(rows: list[dict]) -> set[str]:
    """GT 空间：本批各行 gt 键的并集（produced 的计分资格以批内 gt 为准）。"""
    space: set[str] = set()
    for row in rows:
        space |= set(row.get("gt") or {})
    return space


def _relevance(row: dict, library_ids: set[str]) -> dict[str, float]:
    """该片相关度表：gt ∩ 库（库外标签无基准分，不可评）。"""
    return {tag: float(score) for tag, score in (row.get("gt") or {}).items()
            if tag in library_ids}


def _scoring(row: dict, gt_space: set[str]) -> list[dict]:
    """计分清单：produced 中落在 GT 空间的条目，防御性按 (-sf, tag_id) 排。

    空间外条目剔除（不占 P@K 的坑），由调用方另行列名。
    """
    entries = [e for e in (row.get("produced") or [])
               if e.get("tag_id") in gt_space]
    entries.sort(key=lambda e: (-float(e.get("sf", 0.0)), e["tag_id"]))
    return entries


def _movie_metrics(row: dict, gt_space: set[str], relevance: dict[str, float],
                   theta_p: float, theta_r: float, k_p: int, k_r: int,
                   ) -> tuple[float | None, float | None, float | None]:
    """单片 (p_at_k, r_at_k, spearman)；算不出的项为 None（None≠0）。

    - 失败片或无交集（relevance 空）→ 三项全 None；
    - p 与 r 各用各的门：θ_p-相关集为空 → p=None、θ_r-相关集为空 → r=None
      （原语对空相关集记 0.0，网格靠 valid_theta 预滤规避，单片必须先判空
      再调；各 θ 各对齐各 θ 的网格排除规则）；
    - spearman 与 θ 无关、与 assemble_report 同式：sorted(relevance) 上
      [计分 sf.get(t, 0.0)] × [gt 分]（未产出的交集标签按 0 分补位）。
    """
    if not row.get("ok") or not relevance:
        return (None, None, None)
    scoring = _scoring(row, gt_space)
    ranked = tuple(e["tag_id"] for e in scoring)
    p = (precision_at_k(ranked, relevance, k_p, relevant_grade=theta_p)
         if any(s >= theta_p for s in relevance.values()) else None)
    r = (recall_at_k(ranked, relevance, k_r, relevant_grade=theta_r)
         if any(s >= theta_r for s in relevance.values()) else None)
    system_scores = {e["tag_id"]: float(e.get("sf", 0.0)) for e in scoring}
    keys = sorted(relevance)
    sp = spearman([system_scores.get(t, 0.0) for t in keys],
                  [relevance[t] for t in keys])
    return (p, r, sp)


# ---------------------------------------------------------------- 公共入口

def summarize_movies(rows: list[dict], thetas: Sequence[float],
                     ks: Sequence[int], library_ids: set[str], *,
                     theta_p: float | None = None,
                     theta_r: float | None = None) -> dict:
    """逐片摘要（badcase 视图主表）：指标 + 计数 + 排序。

    - `theta_p`/`theta_r`：P 门与 R 门各自独立；缺省 None → 统一取格
      约定（θ_p≈0.2、θ_r≈0.8，closed_ranking 常量）；`thetas` 回传网格
      档（选择器选项源）；
    - 每片一行：指标（float | None，None=算不出；p 用 θ_p、r 用 θ_r）+
      n_produced（原始产出条数含空间外）+ n_relevant（gt∩库 ≥**θ_r**
      计数，与漏检/命中分类同门；失败片也照数）；
    - 排序：失败置顶 → p_at_k 非空升序 → null 垫底 → 同分按 tt 字典序
      （垫底键只看 p，θ_r 不参与排序）。
    """
    theta_p = (pick_theta(thetas, DEFAULT_THETA_P) if theta_p is None
               else theta_p)
    theta_r = (pick_theta(thetas, DEFAULT_THETA_R) if theta_r is None
               else theta_r)
    k_p = pick_k(ks, DEFAULT_K_P)
    k_r = pick_k(ks, DEFAULT_K_R)
    gt_space = _gt_space(rows)
    out: list[dict] = []
    for row in rows:
        relevance = _relevance(row, library_ids)
        p, r, sp = _movie_metrics(row, gt_space, relevance,
                                  theta_p, theta_r, k_p, k_r)
        out.append({
            "tt": row["tt"],
            "title": row.get("title"),
            "ok": row.get("ok"),
            "error": row.get("error"),
            "spearman": sp,
            "p_at_k": p,
            "r_at_k": r,
            "elapsed_ms": row.get("elapsed_ms"),
            "n_produced": len(row.get("produced") or []),
            "n_relevant": sum(1 for s in relevance.values() if s >= theta_r),
        })
    out.sort(key=lambda r: (r["ok"] is True, r["p_at_k"] is None,
                            r["p_at_k"] if r["p_at_k"] is not None else 0.0,
                            r["tt"]))
    return {"theta_p": theta_p, "theta_r": theta_r, "k_p": k_p, "k_r": k_r,
            "thetas": list(thetas), "rows": out}


def breakdown_movie(rows: list[dict], tt: str, thetas: Sequence[float],
                    ks: Sequence[int], library_ids: set[str],
                    library_names: dict[str, str], *,
                    theta_p: float | None = None,
                    theta_r: float | None = None) -> dict | None:
    """单片明细：计分清单（hit/gt_score）+ 空间外 + misses 三张表。

    - `theta_p`/`theta_r` 同 summarize（缺省 → pick_theta）；明细侧
      `theta_p` 只回显——**hit 标记与 misses 跟 θ_r**（漏检=召回侧
      「相关未产出」；GT 原分行内可见，别的门槛肉眼可判）；
    - `produced`：计分条目（GT 空间 = 各行 gt 键并集；防御性 (-sf, tag_id)
      排），每条附 `gt_score`（该片 gt 原分，不在该片 gt → None）与
      `hit`（gt_score 非 None 且 ≥ theta_r）；
    - `out_of_space`：produced 中不在 GT 空间的条目（不计分只列名，
      与 θ 无关）；
    - `misses`：相关（gt∩库 ≥θ_r）未产出标签，按 (-gt_score, tag_id) 排，
      tag_name 从 library_names 回填（缺 → None）；
    - 失败片三表全空、error 原样；未知 tt → None。
    """
    row = next((r for r in rows if r.get("tt") == tt), None)
    if row is None:
        return None
    theta_p = (pick_theta(thetas, DEFAULT_THETA_P) if theta_p is None
               else theta_p)
    theta_r = (pick_theta(thetas, DEFAULT_THETA_R) if theta_r is None
               else theta_r)
    detail = {
        "tt": row["tt"],
        "title": row.get("title"),
        "ok": row.get("ok"),
        "error": row.get("error"),
        "elapsed_ms": row.get("elapsed_ms"),
        "theta_p": theta_p,
        "theta_r": theta_r,
        "k_p": pick_k(ks, DEFAULT_K_P),
        "k_r": pick_k(ks, DEFAULT_K_R),
        "produced": [],
        "out_of_space": [],
        "misses": [],
    }
    if not row.get("ok"):
        return detail  # 失败片：三表全空、error 原样
    gt = row.get("gt") or {}
    gt_space = _gt_space(rows)
    for entry in _scoring(row, gt_space):
        gt_score = gt.get(entry["tag_id"])
        detail["produced"].append({
            "tag_id": entry["tag_id"],
            "tag_name": entry.get("tag_name"),
            "sf": entry.get("sf"),
            "confidence": entry.get("confidence"),
            "evidence_count": entry.get("evidence_count"),
            "gt_score": gt_score,
            "hit": gt_score is not None and gt_score >= theta_r,
        })
    out_of_space = [{"tag_id": e["tag_id"], "tag_name": e.get("tag_name"),
                     "sf": e.get("sf")}
                    for e in (row.get("produced") or [])
                    if e.get("tag_id") not in gt_space]
    out_of_space.sort(key=lambda e: (-float(e.get("sf", 0.0)), e["tag_id"]))
    detail["out_of_space"] = out_of_space
    produced_ids = {e.get("tag_id") for e in (row.get("produced") or [])}
    misses = sorted(
        ({"tag_id": t, "gt_score": s}
         for t, s in _relevance(row, library_ids).items()
         if s >= theta_r and t not in produced_ids),
        key=lambda m: (-m["gt_score"], m["tag_id"]))
    detail["misses"] = [{"tag_id": m["tag_id"],
                         "tag_name": library_names.get(m["tag_id"]),
                         "gt_score": m["gt_score"]} for m in misses]
    return detail
