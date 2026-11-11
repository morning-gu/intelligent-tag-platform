"""消融实验评测：只改输入模态，其余全固定，量化"每种模态各值多少"（只读）。

用法（仓库根目录）：

    # 1) 逐变体跑同一批资产（每次只改模态，其余别动）
    TAGPLT_MTG_MODALITIES=video,script,dialogue python run_server.py   # full
    TAGPLT_MTG_MODALITIES=video           python run_server.py         # video-only
    TAGPLT_MTG_MODALITIES=script          python run_server.py         # script-only
    TAGPLT_MTG_MODALITIES=dialogue        python run_server.py         # dialogue-only
    TAGPLT_MTG_MODALITIES=video,script    python run_server.py         # video-script
    # 2) 对照
    python scripts/eval_ablation.py --full full --variant video-only
    python scripts/eval_ablation.py --full full --variant all --json out.json

变体名与上游 `configs/ablation/` 一一对应：`full / video-only /
script-only / dialogue-only / video-script`（定义见
`tagplatform/eval/ablation.py` 的 `ABLATION_VARIANTS`）。

为什么要先跑再评（而不是一个脚本全干完）
========================================

上游的做法也是分开的两步：`04_run_annotation.py --config <cfg>` 逐配置跑，
`06_evaluate.py --extra ...` 再对照。分开的理由：

1. 跑一轮要花真金白银，中途失败不该让评测代码跟着重来；
2. 审计 JSON 是**留痕**，评测只读它，结论可复核（谁都能拿同一批报告重算）；
3. 消融的纪律是"只改一个字段"，而"只改一个字段"必须在**启动配置**这一层
   保证——脚本内改配置容易顺手改到别处。

配对方式与指标
==============

同一部片在不同变体下的报告按 `asset_id` **配对**（`--variant` 与 `--full`
的资产集必须一致，缺项直接跳过并报出来，绝不"少评几条把平均拉高"）。

逐资产指标都取自审计报告，不需要模型：

- `accepted`：最终接受条数（`report["accepted"]` 长度）；
- `judge_pass`：judge 通过率 = 接受 / 合并候选（衡量"内容够不够支撑"）；
- `new_ratio`：接受结果里新词占比（库里没有的词，衡量"是否只是复述库"）；
- `frames`：这一变体实际喂了多少帧（关掉 video 应恒为 0，用来验证配置真的
  生效，而不是脚本以为自己改了）。

差异显著性用**配对 bootstrap**（`eval/ablation.py`）：同一批片子上跑两个
配置本来就是配对的，配对比独立检验灵敏得多，且给出置信区间而不只是 p 值。

安全设计：本脚本**只读**审计目录，不连数据库、不调模型。

分层红线：脚本可用平台模块，但指标全在 `tagplatform/eval`（纯标准库）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tagplatform.eval import (
    ABLATION_VARIANTS,
    VariantResult,
    compare,
    variant_by_name,
)
from tagplatform.settings import get_settings

# 报告里判"新词"的分档名（与 `catalog_align` 的 BAND_NEW 一致）
_NEW_BANDS = {None, "new"}


def load_reports(directory: Path) -> tuple[dict[str, dict[str, dict]], list[str]]:
    """审计目录 → `{变体名: {asset_id: 报告}}`，外加问题清单。

    变体名取报告里的 `params.modalities`（排序后按 `+` 拼起来再映射回
    `ABLATION_VARIANTS` 的名字）。旧报告没有这个字段时归为 `full`——
    那时只存在全模态一种口径。
    """
    by_variant: dict[str, dict[str, dict]] = {}
    problems: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: 读不出来（{exc}）")
            continue
        if data.get("error"):
            problems.append(f"{path.name}: 生成本身失败（{str(data['error'])[:60]}）")
            continue
        asset_id = data.get("asset_id")
        if not asset_id:
            problems.append(f"{path.name}: 没有 asset_id，无法配对")
            continue
        name = _variant_name(data)
        if name is None:
            problems.append(
                f"{path.name}: 模态 {data.get('params', {}).get('modalities')} "
                f"不对应任何已知变体")
            continue
        by_variant.setdefault(name, {})[asset_id] = data
    return by_variant, problems


def _variant_name(report: dict) -> str | None:
    """报告 → 变体名（`params.modalities` 缺失按 full 处理）。"""
    modalities = (report.get("params") or {}).get("modalities")
    if modalities is None:
        return "full"
    wanted = frozenset(modalities)
    for variant in ABLATION_VARIANTS:
        if variant.modalities == wanted:
            return variant.name
    return None


def item_metrics(report: dict) -> dict[str, float]:
    """单份报告 → 逐资产指标（全部从留痕字段算，不依赖模型）。"""
    accepted = report.get("accepted") or []
    merged = report.get("merged") or []
    new_count = sum(1 for row in accepted if row.get("band") in _NEW_BANDS)
    return {
        "accepted": float(len(accepted)),
        "judge_pass": (len(accepted) / len(merged)) if merged else 0.0,
        "new_ratio": (new_count / len(accepted)) if accepted else 0.0,
        "frames": float(report.get("frames") or 0),
    }


def build_result(name: str, reports: dict[str, dict],
                 ordered_assets: list[str]) -> VariantResult:
    """按统一的资产顺序组装 `VariantResult`（缺项按 0 补齐会误导，故不补）。"""
    result = VariantResult(variant=variant_by_name(name))
    for asset_id in ordered_assets:
        report = reports.get(asset_id)
        if report is None:
            continue
        result.add(item_metrics(report), item=asset_id)
    return result.finalize()


def _fmt(value: float | None, digits: int = 3) -> str:
    return "暂无" if value is None else f"{value:.{digits}f}"


def print_comparison(full: VariantResult, with_others: list[VariantResult],
                     comparisons: dict[str, dict[str, dict]],
                     skipped_assets: list[str]) -> None:
    print(f"对照组 {full.variant.name}（{sorted(full.variant.modalities)}）"
          f"　资产 {full.n_items} 部")
    print()
    header = f"{'指标':<12}{'full':>9}"
    for result in with_others:
        header += f"{result.variant.name:>16}"
    print(header)
    print("-" * len(header))
    for name, value in full.metrics.items():
        line = f"{name:<12}{_fmt(value, 2):>9}"
        for result in with_others:
            delta = comparisons[result.variant.name].get(name)
            if not isinstance(delta, dict):
                line += f"{'—':>16}"
                continue
            mark = "*" if delta["p"] < 0.05 else " "
            line += f"{_fmt(result.metrics.get(name), 2) + mark:>16}"
        print(line)

    print()
    print("差值 = full − 变体（**正数 = 全模态更好**；* 表示 p < 0.05，"
          "配对 bootstrap）：")
    for result in with_others:
        print(f"\n  【{result.variant.name}】关掉 {list(result.variant.dropped)}")
        for name, delta in comparisons[result.variant.name].items():
            if name == "_skipped":
                continue
            low, high = delta["ci"]
            verdict = "全模态更好" if delta["delta"] > 0 else "变体更好"
            if delta["p"] >= 0.05:
                verdict = "分不开"
            print(f"    {name:<12} Δ={delta['delta']:+.3f}  "
                  f"p={delta['p']:.3f}  95%CI [{low:+.3f}, {high:+.3f}]  {verdict}")
        skipped = comparisons[result.variant.name].get("_skipped")
        if skipped:
            print(f"    （跳过缺项指标：{skipped}）")
    if skipped_assets:
        print(f"\n只在部分变体出现的资产 {len(skipped_assets)} 部（已排除配对）：")
        for asset_id in skipped_assets[:10]:
            print(f"  - {asset_id}")
        if len(skipped_assets) > 10:
            print(f"  …… 其余 {len(skipped_assets) - 10} 部略")


def build_payload(full: VariantResult, with_others: list[VariantResult],
                  comparisons: dict[str, dict[str, dict]],
                  skipped_assets: list[str]) -> dict:
    return {
        "full": {"name": full.variant.name, "n_items": full.n_items,
                 "metrics": full.metrics},
        "variants": [
            {"name": r.variant.name, "dropped": list(r.variant.dropped),
             "n_items": r.n_items, "metrics": r.metrics,
             "vs_full": comparisons[r.variant.name]}
            for r in with_others
        ],
        "skipped_assets": skipped_assets,
        "note": "差值 = full − 变体（正数 = 全模态更好）；配对 bootstrap；"
                "只在同一批资产的交集上比较。",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="输入模态消融评测（只读）")
    parser.add_argument("--dir", default=None,
                        help="审计报告目录（默认取 settings.mtg_report_dir）")
    parser.add_argument("--full", default="full",
                        help="对照组变体名（默认 full，必须是全模态）")
    parser.add_argument("--variant", nargs="+", default=None,
                        help="要对照的变体名（默认全部非 full 变体）")
    parser.add_argument("--resamples", type=int, default=5000,
                        help="配对 bootstrap 重采样次数（默认 5000）")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="把结果写成 JSON（供改动前后对比）")
    args = parser.parse_args(argv)

    directory = Path(args.dir or get_settings().mtg_report_dir)
    if not directory.exists():
        print(f"审计目录不存在：{directory}")
        print("提示：跑过开放式生成任务后才会产生 mtg_report_dir/<task_id>.json。")
        return 1

    by_variant, problems = load_reports(directory)
    if args.full not in by_variant:
        print(f"审计目录里没有对照组 {args.full} 的报告。"
              f"已发现：{', '.join(sorted(by_variant)) or '（无）'}")
        return 1

    names = args.variant or [v.name for v in ABLATION_VARIANTS if v.name != args.full]
    # 显式指定却不存在的变体是笔误，必须报错；默认口径下只跑"已发现"的变体，
    # 否则只要少跑一种模态，整张表就出不来。
    unknown = [n for n in names if n not in by_variant]
    if args.variant and unknown:
        print(f"这些变体没有报告：{', '.join(unknown)}")
        print(f"已发现：{', '.join(sorted(by_variant))}")
        return 1
    names = [n for n in names if n in by_variant]
    if not names:
        print(f"审计目录里只有 {args.full} 的报告，没有可比对的消融变体。")
        print("先换 TAGPLT_MTG_MODALITIES 跑一批任务，再回来对照。")
        return 1
    if unknown:
        print(f"（跳过没有报告的变体：{', '.join(unknown)}）")

    # 配对基准：所有变体共有的资产（差集不参与比较，单独报出）
    common = set(by_variant[args.full])
    for name in names:
        common &= set(by_variant[name])
    universe = set(by_variant[args.full])
    for name in names:
        universe |= set(by_variant[name])
    skipped_assets = sorted(universe - common)
    ordered = sorted(common)

    if not ordered:
        print("对照组与变体没有共同资产，无法配对比较。")
        return 1

    full_result = build_result(args.full, by_variant[args.full], ordered)
    results = [build_result(name, by_variant[name], ordered) for name in names]
    comparisons = {
        result.variant.name: compare(full_result, result,
                                     n_resamples=args.resamples)
        for result in results
    }

    print(f"审计目录 {directory}　配对资产 {len(ordered)} 部")
    print_comparison(full_result, results, comparisons, skipped_assets)
    if problems:
        print(f"\n跳过 / 异常报告 {len(problems)} 份：")
        for line in problems[:10]:
            print(f"  - {line}")
        if len(problems) > 10:
            print(f"  …… 其余 {len(problems) - 10} 份略")

    if args.json_path:
        payload = build_payload(full_result, results, comparisons, skipped_assets)
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())