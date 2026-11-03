"""CLI：数据集影片 → 上游契约格式的资产 JSON（剧情/氛围/人物情绪/台词/事件）。

例：
    # 一部片，3 帧/场景，2 并发
    python scripts/build_assets.py --movienet-root /cache/MovieNet \
        --ids tt0068646 --out data/assets

    # 多部（先到先得，跳过没有 story 分段的）
    python scripts/build_assets.py --movienet-root /cache/MovieNet \
        --limit 5 --out data/assets --workers 3

    # 只验结构不调模型（airplane mode：场景描述留空，用于检查文件形态）
    python scripts/build_assets.py --movienet-root /cache/MovieNet \
        --ids tt0068646 --out /tmp/x --dry-run

成本与恢复：每部片 = 1 次整片摘要 + 每场景 1 次多模态调用（失败重试 1 次）。
已存在的 `movie_<id>.json` 默认跳过，`--force` 重跑；manifest 记 tokens/耗时/
失败场景，便于对账。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tagplatform.assets import AssetBuilder  # noqa: E402
from tagplatform.llm import LLMResponse  # noqa: E402


class DryRunClient:
    """不联网的假客户端：返回结构合法的占位 JSON（验形态用）。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[dict], run_id: str) -> LLMResponse:
        self.calls += 1
        if run_id.startswith("summary_"):
            return LLMResponse(content="（dry-run）整片剧情描述占位。", usage={},
                               raw={}, ok=True)
        return LLMResponse(
            content=json.dumps({
                "description": "（dry-run）场景剧情描述占位。",
                "atmosphere": ["紧张"],
                "events": [], "virtual_time": "", "location": "",
                "dialogues": [],
            }, ensure_ascii=False),
            usage={}, raw={}, ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="数据集影片 → 精简资产 JSON")
    parser.add_argument("--movienet-root", required=True)
    parser.add_argument("--out", default="data/assets")
    parser.add_argument("--ids", nargs="*", default=None, help="指定 IMDb id")
    parser.add_argument("--limit", type=int, default=None,
                        help="成功生成多少部（跳过无 story 分段的）")
    parser.add_argument("--frames", type=int, default=3, help="每场景抽帧数")
    parser.add_argument("--workers", type=int, default=2, help="场景级并发")
    parser.add_argument("--dialogue-limit", type=int, default=5,
                        help="每场景最多带几行字幕（0=不带台词）")
    parser.add_argument("--no-infer", dest="infer", action="store_false",
                        default=True,
                        help="只填数据集确有的字段，不调模型（对齐校验用）")
    parser.add_argument("--force", action="store_true", help="覆盖已有输出")
    parser.add_argument("--dry-run", action="store_true", help="不调模型，验结构")
    parser.add_argument("--max-scan", type=int, default=200,
                        help="最多检查多少部片（--limit 生效时）")
    args = parser.parse_args()

    root = Path(args.movienet_root)
    for required in ("meta_data", "annotation_data"):
        if not (root / required).exists():
            print(f"缺少 {root / required}", file=sys.stderr)
            return 1

    if args.dry_run:
        client = DryRunClient()
    else:
        from tagplatform.llm import make_llm_client
        from tagplatform.settings import get_settings
        client = make_llm_client(get_settings())

    listing = [ln.strip() for ln in (root / "list.v1.txt").read_text(
        encoding="utf-8").splitlines() if ln.strip()]

    builder = AssetBuilder(root=root, out_dir=Path(args.out), client=client,
                           k=args.frames, workers=args.workers,
                           dialogue_limit=args.dialogue_limit,
                           infer=args.infer)

    if args.ids:
        results = builder.build_many(args.ids, force=args.force)
    else:
        done: list[dict] = []
        scanned = 0
        out_dir = Path(args.out)
        for movie_id in listing:
            if args.limit is not None and len(done) >= args.limit:
                break
            if scanned >= args.max_scan:
                break
            scanned += 1
            if not (root / "annotation_data" / "annotation" / f"{movie_id}.json").exists():
                continue
            annotation = json.loads(
                (root / "annotation_data" / "annotation" / f"{movie_id}.json")
                .read_text(encoding="utf-8"))
            if not (annotation.get("story") or []):
                continue
            stats = builder.build_movie(movie_id, force=args.force)
            done.append(stats.as_dict())
            print(json.dumps(stats.as_dict(), ensure_ascii=False))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "_manifest.json").write_text(
            json.dumps({"total": len(done), "movies": done,
                        "prompt_tokens": sum(r["prompt_tokens"] for r in done),
                        "completion_tokens": sum(r["completion_tokens"] for r in done)},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        results = done

    total_fail = sum(len(r["failed_scenes"]) for r in results)
    print(json.dumps({"movies": len(results),
                      "prompt_tokens": sum(r["prompt_tokens"] for r in results),
                      "completion_tokens": sum(r["completion_tokens"] for r in results),
                      "failed_scenes": total_fail}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
