"""MovieNet 官方分发 → 上游长视频理解 JSON（`schemas/upstream.py` 格式）。

用途：在视频理解部门接入之前，用开源数据集造出**格式正确、内容真实**的
影视资产，打通「资产导入 → 生成式标签 → 待审核」链路。

输入（MovieNet 官方分发布局，只读）：
    <root>/list.v1.txt                        1,100 部 movie1K 的 IMDb id
    <root>/meta_data/meta/<tt>.json           title/genres/runtime/cast/plot…
    <root>/annotation_data/annotation/<tt>.json
                                              story[]（分段剧情描述 + 秒级时间）
    <root>/subtitle1K.v1.zip                  字幕（<tt>.srt，可选）
    <root>/frames/frames/<tt>/*.jpg           关键帧（本脚本不读，留给后续 VLM）

输出：`--out` 目录下每部片一个 `<asset_id>.json`（上游格式），外加
`_manifest.json`（成功/跳过清单与计数，便于复跑与对账）。

转换要点：
- `content.description` ← meta 的 storyline/overview/plot/synopsis 首个非空；
- `scenes[]` ← annotation.story 分段：`duration:[start_sec, end_sec]` → 毫秒，
  `description` 为英文剧情段落；
- `characters[]` ← meta.cast（角色名 + 饰演演员名）；
- `dialogues[]` ← SRT 条目，按起始时间落入覆盖它的场景（缺字幕则空）；
- `emotion` / `audio_events` 留空——这正是上游视频理解能力要补的两块，
  平台侧渲染已支持，字段到位即生效。

只处理**有 story 分段**的电影：没有分段描述的片子只能给出标题与整片简介，
场景证据为空，无法支撑标签生成，直接跳过并计入 manifest。

用法：
    python scripts/ingest_movienet.py --movienet-root /cache/MovieNet \
        --out data/ingest/movienet --limit 20
    # 直接入库（服务层，不经 HTTP）：
    python scripts/ingest_movienet.py --movienet-root /cache/MovieNet \
        --out data/ingest/movienet --limit 20 --import
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})?\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})?")
_YEAR_TAIL = re.compile(r"\(([^()]*)\)\s*$")
_YEAR = re.compile(r"(\d{4})")
_RUNTIME_H = re.compile(r"(\d+)\s*h", re.IGNORECASE)
_RUNTIME_M = re.compile(r"(\d+)\s*m", re.IGNORECASE)


def _title_year(raw_title: str) -> tuple[str, int | None]:
    """`"绿野仙踪 (1939)"` → ("绿野仙踪", 1939)；无括号年份时原样返回。"""
    m = _YEAR_TAIL.search(raw_title)
    if not m:
        return raw_title.strip(), None
    y = _YEAR.search(m.group(1))
    return raw_title[: m.start()].strip() or raw_title.strip(), (
        int(y.group(1)) if y else None)


def _runtime_ms(meta: dict) -> int | None:
    """meta.version[0].runtime（如 "1 min" / "2h 5min"）→ 毫秒；解析不出返回 None。"""
    versions = meta.get("version") or []
    if not versions:
        return None
    text = str(versions[0].get("runtime") or "")
    hours = _RUNTIME_H.search(text)
    minutes = _RUNTIME_M.search(text)
    if not hours and not minutes:
        return None
    return ((int(hours.group(1)) * 60 if hours else 0)
            + (int(minutes.group(1)) if minutes else 0)) * 60_000


def _story_description(meta: dict) -> str:
    """整片剧情描述：storyline > overview > plot > synopsis 首个非空。"""
    for key in ("storyline", "overview", "plot", "synopsis"):
        value = (meta.get(key) or "").strip()
        if value:
            return value
    return ""


def _characters(meta: dict) -> list[dict]:
    """meta.cast → 上游角色表（角色名优先，description 记饰演演员）。"""
    rows: list[dict] = []
    seen: set[str] = set()
    for index, member in enumerate(meta.get("cast") or [], start=1):
        character = (member.get("character") or "").strip()
        actor = (member.get("name") or "").strip()
        name = character or actor
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append({
            "character_id": f"character_{index:03d}",
            "name": name,
            "aliases": [],
            "description": f"饰演：{actor}" if actor and character else None,
        })
    return rows


def _parse_srt(text: str) -> list[tuple[int, int, str]]:
    """SRT → [(start_ms, end_ms, text)]；容错缺毫秒/多余行。"""
    entries: list[tuple[int, int, str]] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        m = _SRT_TIME.search(block)
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        start = ((int(h1) * 3600 + int(m1) * 60 + int(s1)) * 1000
                 + int(ms1 or 0))
        end = ((int(h2) * 3600 + int(m2) * 60 + int(s2)) * 1000
               + int(ms2 or 0))
        seed = m.end() - block.find(m.group(0))
        body = block[seed:].strip()
        # 首行若还是序号（"12"），去掉
        parts = [ln for ln in body.splitlines() if not ln.isdigit()]
        text_line = " ".join(parts).strip()
        if text_line:
            entries.append((start, end, text_line))
    return entries


def _load_srt(zip_path: Path, movie_id: str) -> list[tuple[int, int, str]] | None:
    """从 subtitle1K.v1.zip 读某片字幕；不存在返回 None。"""
    if not zip_path.exists():
        return None
    member = f"subtitle/{movie_id}.srt"
    with zipfile.ZipFile(zip_path) as zf:
        try:
            raw = zf.read(member)
        except KeyError:
            return None
    return _parse_srt(raw.decode("utf-8", errors="replace"))


def build_payload(movie_id: str, meta: dict, annotation: dict,
                  subtitles: list[tuple[int, int, str]] | None) -> dict | None:
    """单部电影 → 上游 JSON dict；无 story 分段时返回 None（跳过）。"""
    story = annotation.get("story") or []
    if not story:
        return None

    title, year = _title_year(str(meta.get("title") or movie_id))
    description = _story_description(meta)
    scenes: list[dict] = []
    for index, seg in enumerate(story):
        duration = seg.get("duration") or [0, 0]
        start_ms = max(0, int(round(float(duration[0]) * 1000)))
        end_ms = max(start_ms, int(round(float(duration[1]) * 1000)))
        scenes.append({
            "scene_id": f"{movie_id}_story_{index:03d}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "description": (seg.get("description") or "").strip(),
            "character_ids": [],
            "events": [],
            "emotion": [],
            "dialogues": [],
            "audio_events": [],
        })

    # 字幕按起始时间落入覆盖它的场景（区间重叠，优先第一个命中）
    if subtitles and scenes:
        for start, end, text in subtitles:
            for scene in scenes:
                if start < scene["end_ms"] and end > scene["start_ms"]:
                    scene["dialogues"].append({
                        "speaker_id": None,
                        "speaker_name": None,
                        "text": text,
                        "start_ms": start,
                        "end_ms": end,
                    })
                    break

    total_ms = scenes[-1]["end_ms"] if scenes else _runtime_ms(meta)
    return {
        "schema_version": "1.0",
        "asset": {
            "asset_id": f"movie_{movie_id}",
            "asset_type": "movie",
            "title": title,
            "duration_ms": total_ms,
            "video_uri": None,
            "language": "en",
        },
        "content": {
            "description": description,
            "characters": _characters(meta),
            "scenes": scenes,
        },
    }


def _select_ids(root: Path, ids: list[str] | None) -> list[str]:
    """候选 id 全序列：显式 --ids 优先，否则读完整 list.v1.txt。

    limit 的语义是「最多成功多少部」，不是「只看前多少个 id」——MovieNet
    只有约 1/3 的片子有 story 分段，按前 N 个 id 截断会浪费大量配额，
    因此扫描顺序由调用方在 ingest() 里按成功数提前终止。
    """
    if ids:
        return ids
    listing = root / "list.v1.txt"
    return [ln.strip() for ln in listing.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def ingest(root: Path, out: Path, limit: int | None, ids: list[str] | None) -> dict:
    """遍历候选 id → 生成上游 JSON 落盘；返回统计 manifest。

    limit 为「成功生成数」上限（None = 全量扫描）：先到先得，跳过没有
    story 分段的片子继续往后找。
    """
    out.mkdir(parents=True, exist_ok=True)
    subtitle_zip = root / "subtitle1K.v1.zip"
    done: list[str] = []
    skipped: dict[str, str] = {}

    for movie_id in _select_ids(root, ids):
        if limit is not None and len(done) >= limit:
            break
        meta_path = root / "meta_data" / "meta" / f"{movie_id}.json"
        ann_path = root / "annotation_data" / "annotation" / f"{movie_id}.json"
        if not meta_path.exists() or not ann_path.exists():
            skipped[movie_id] = "meta/annotation 缺失"
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        annotation = json.loads(ann_path.read_text(encoding="utf-8"))
        payload = build_payload(movie_id, meta, annotation,
                                _load_srt(subtitle_zip, movie_id))
        if payload is None:
            skipped[movie_id] = "无 story 分段"
            continue
        (out / f"{payload['asset']['asset_id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        done.append(payload["asset"]["asset_id"])

    manifest = {"total": len(done) + len(skipped), "done": len(done),
                "skipped": len(skipped), "assets": done,
                "skip_reasons": skipped}
    (out / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _import_payloads(out: Path, video_root: str | None = None) -> dict:
    """把 out 目录下的上游 JSON 经服务层导入平台库（不 commit 业务外的内容）。"""
    from sqlalchemy.orm import Session  # noqa: F401  (类型提示用)

    from tagplatform.adapters.vision import to_content_json
    from tagplatform.content_import import import_content
    from tagplatform.db import create_engine_for, make_session_factory
    from tagplatform.schemas.upstream import parse_upstream
    from tagplatform.settings import get_settings

    settings = get_settings()
    engine = create_engine_for(settings.database_path)
    factory = make_session_factory(engine)
    ok, failed = 0, {}
    with factory() as session:
        for path in sorted(out.glob("movie_*.json")):
            try:
                payload = parse_upstream(json.loads(path.read_text(encoding="utf-8")))
                content = to_content_json(payload)
                import_content(content, session, video_root)
                ok += 1
            except Exception as exc:  # noqa: BLE001 —— 单部失败不阻断整批
                failed[path.stem] = f"{type(exc).__name__}: {exc}"
        session.commit()
    engine.dispose()
    return {"imported": ok, "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MovieNet → 上游长视频理解 JSON（并可入库）")
    parser.add_argument("--movienet-root", required=True, help="如 /cache/MovieNet")
    parser.add_argument("--out", default="data/ingest/movienet")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", nargs="*", default=None)
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="生成后直接经服务层导入平台库")
    args = parser.parse_args()

    root = Path(args.movienet_root)
    for required in ("list.v1.txt", "meta_data", "annotation_data"):
        if not (root / required).exists():
            print(f"[ingest] 缺少 {root / required}", file=sys.stderr)
            return 1

    manifest = ingest(root, Path(args.out), args.limit, args.ids)
    print("[ingest] " + json.dumps({k: v for k, v in manifest.items()
                                    if k != "assets"}, ensure_ascii=False))
    if args.do_import:
        result = _import_payloads(Path(args.out))
        print("[ingest] import: " + json.dumps(
            {"imported": result["imported"], "failed": len(result["failed"])},
            ensure_ascii=False))
        if result["failed"]:
            print(json.dumps(result["failed"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
