"""生成一致性评测：同一份输入跑多轮，结果到底稳不稳（只读，不写库）。

用法（仓库根目录）：

    python scripts/eval_consistency.py
    python scripts/eval_consistency.py --dir data/audit/mtg
    python scripts/eval_consistency.py --min-runs 3 --json out.json

数据来源
========

直接读开放式生成的审计报告（`mtg_report_dir/<task_id>.json`）。每份报告里
`runs[].tags` 是**同一部片每一轮各自产出的标签名**，这正是一致性需要的
`(n_runs, tags)` 原始观测——`tagplatform/generation/mtg/pipeline.py` 逐轮
把它写进报告，所以算一致性不必重跑模型（重跑不但贵，而且每次都是新样本，
反而测不出"同一设置下的稳定程度"）。

为什么不在线跑模型
==================

在线跑 N 轮拿 N 轮结果算一致性，测的是"这一批任务的抖动"；离线读历史报告，
测的是"长期跑下来的抖动"。后者样本更多、免费，且能回溯"换了提示词之后
一致性是变好还是变坏"——这才是回归判据。只在需要测**新配置**时，
才先跑一批任务再用本脚本看结果。

指标读法
========

- `mean_pairwise_jaccard`：轮与轮之间标签集合有多像（0 = 完全不像，1 = 完全
  相同）。这是主指标，最直观；
- `stable_share`：所有轮次都出现的标签占并集的比例（"稳定产出率"）。
  **这才是审核员该沉淀的词条**，只出现一轮的多半是采样偶然；
- `consistency_std` / `icc_2_1`：把标签看成 0/1 矩阵逐行算跨轮离散度与
  ICC(2,1)。注意 0/1 矩阵的 ICC 会被"大多数标签只出现一次"拉向负值，
  它在这里是**辅助读法**，不与上游的电影级连续分数直接比较。

一个必须写明的限制
==================

`mtg_runs` 在默认配置下是 3，且**同一个 `run_id` 会在多轮里重复**——报告里
`runs` 的轮数就是实际跑了几轮。少于 2 轮的报告无法算一致性（脚本会跳过并
计数，不会把它当 0 分混进平均）。

安全设计：本脚本**只读**审计目录，不连数据库、不调模型。

分层红线：脚本可用平台模块，但指标全在 `tagplatform/eval`（纯标准库）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tagplatform.eval import (
    consistency_std,
    icc_2_1,
    macro_average,
    mean_pairwise_jaccard,
    presence_matrix,
    stable_share,
)
from tagplatform.settings import get_settings


def load_reports(directory: Path, *, min_runs: int) -> tuple[list[dict], list[str]]:
    """读取审计目录下所有 JSON，返回 `(可用报告, 问题清单)`。

    单份报告坏掉不该让整轮评测失败——跳过并记原因，最后一起报出来。
    """
    usable: list[dict] = []
    problems: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: 读不出来（{exc}）")
            continue
        if data.get("error"):
            problems.append(f"{path.name}: 生成本身失败（{data['error'][:60]}）")
            continue
        runs = [run for run in data.get("runs") or [] if run.get("tags") is not None]
        if len(runs) < min_runs:
            problems.append(
                f"{path.name}: 只有 {len(runs)} 轮有标签记录"
                f"（少于 --min-runs={min_runs}，旧版本报告可能没存 tags）")
            continue
        usable.append({**data, "runs": runs, "_file": path.name})
    return usable, problems


def evaluate_report(report: dict) -> dict:
    """单份报告 → 一致性指标（集合口径 + 逐条离散度口径）。"""
    tag_sets = [run.get("tags") or [] for run in report["runs"]]
    items, matrix = presence_matrix(tag_sets)
    return {
        "task_id": report.get("task_id") or report.get("_file"),
        "asset_id": report.get("asset_id"),
        "runs": len(tag_sets),
        "tags": len(items),
        "mean_pairwise_jaccard": mean_pairwise_jaccard(tag_sets),
        "stable_share": stable_share(tag_sets),
        "icc": icc_2_1(matrix),
        "mean_std": macro_average(consistency_std(matrix)),
        "per_run": [len(tags) for tags in tag_sets],
    }


def _fmt(value: float | None, digits: int = 3) -> str:
    return "暂无" if value is None else f"{value:.{digits}f}"


def _mean(values: list[float | None]) -> float | None:
    """宏平均，**先滤掉 `None`**。

    `macro_average` 不吞 `None`（它要区分"暂无"与 0），而这里的 `icc`、
    `stable_share` 都可能因为分母为 0 而算不出。把这些 `None` 当成 0 混进
    平均，就会把"算不出"拉成"很差"，结论方向直接反了。
    """
    return macro_average([value for value in values if value is not None])


def print_report(rows: list[dict], problems: list[str]) -> None:
    if not rows:
        print("没有可用报告。先跑一批开放式生成任务，再回来评测。")
    else:
        print(f"{'任务':<24}{'轮':>3}{'标签':>6}{'两两Jaccard':>13}"
              f"{'稳定产出率':>12}{'ICC(2,1)':>10}{'平均std':>9}")
        print("-" * 79)
        for row in rows:
            print(f"{str(row['task_id'])[:23]:<24}{row['runs']:>3}{row['tags']:>6}"
                  f"{_fmt(row['mean_pairwise_jaccard']):>13}"
                  f"{_fmt(row['stable_share']):>12}"
                  f"{_fmt(row['icc']):>10}{_fmt(row['mean_std']):>9}")

        print()
        print(f"报告 {len(rows)} 份　平均轮数 "
              f"{_fmt(_mean([float(r['runs']) for r in rows]), 1)}")
        print(f"两两 Jaccard 宏平均 {_fmt(_mean([r['mean_pairwise_jaccard'] for r in rows]))}"
              f"　稳定产出率宏平均 {_fmt(_mean([r['stable_share'] for r in rows]))}"
              f"　ICC 宏平均 {_fmt(_mean([r['icc'] for r in rows]))}")
        uncomputable = sum(1 for r in rows if r["icc"] is None)
        if uncomputable:
            print(f"其中 {uncomputable} 份报告的全轮标签完全相同，ICC 分母为 0，"
                  "记作「暂无」且不参与宏平均（不是不一致）。")
        print("读法：Jaccard 与稳定产出率看主结论；ICC 在 0/1 矩阵上会被"
              "「多数标签只出现一次」拉低，只作辅助。")
    if problems:
        print(f"\n跳过 / 异常 {len(problems)} 份：")
        for line in problems[:20]:
            print(f"  - {line}")
        if len(problems) > 20:
            print(f"  …… 其余 {len(problems) - 20} 份略")


def build_payload(rows: list[dict], problems: list[str]) -> dict:
    return {
        "reports": len(rows),
        "per_task": rows,
        "aggregate": {
            "mean_pairwise_jaccard": _mean(
                [r["mean_pairwise_jaccard"] for r in rows]),
            "stable_share": _mean([r["stable_share"] for r in rows]),
            "icc": _mean([r["icc"] for r in rows]),
            "mean_std": _mean([r["mean_std"] for r in rows]),
            "runs": _mean([float(r["runs"]) for r in rows]),
            "n_icc_uncomputable": sum(1 for r in rows if r["icc"] is None),
        },
        "problems": problems,
        "note": "ICC 在 0/1 存在矩阵上仅作辅助；主结论看 Jaccard 与稳定产出率。",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开放式生成一致性评测（只读）")
    parser.add_argument("--dir", default=None,
                        help="审计报告目录（默认取 settings.mtg_report_dir）")
    parser.add_argument("--min-runs", type=int, default=2,
                        help="至少几轮有标签记录才纳入评测（默认 2）")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="把结果写成 JSON（供改动前后对比）")
    args = parser.parse_args(argv)

    directory = Path(args.dir or get_settings().mtg_report_dir)
    if not directory.exists():
        print(f"审计目录不存在：{directory}")
        print("提示：跑过开放式生成任务后才会产生 mtg_report_dir/<task_id>.json。")
        return 1

    reports, problems = load_reports(directory, min_runs=args.min_runs)
    rows = [evaluate_report(report) for report in reports]
    print(f"审计目录 {directory}　纳入 {len(reports)} 份报告")
    print_report(rows, problems)

    if args.json_path:
        payload = build_payload(rows, problems)
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())