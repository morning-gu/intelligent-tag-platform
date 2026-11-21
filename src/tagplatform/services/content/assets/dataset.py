"""数据集索引与「上传文件名 → 数据集影片」匹配。

用户上传任意 mp4，文件名里通常带片名（`The.Godfather.1972.1080p.mkv`）。
本模块把这串文件名归一化后，与 MovieNet 里**可生成资产**的影片比对，命中则
返回影片 id，交给资产生成；没命中就明确报错，不静默失败。

匹配优先级（先严后宽，命中即返回）：
1. 文件名里带 IMDb id（`tt0068646`）—— 100% 精确；
2. 归一化后与片名完全相同；
3. 归一化后互相包含（文件名常有前后缀噪声）；
4. `difflib` 相似度 ≥ 0.86，取最高分（并列时评分高者优先）。

归一化：去扩展名、去年份/分辨率/编码/来源等发布组噪声、去音轨标记、
小写、去标点、压缩空格、去掉开头冠词 the/a/an。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

_IMDB_RE = re.compile(r"(tt\d{7,8})", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")
# 发布组/技术标记：出现即从标题里剥离
_NOISE_TOKENS = {
    "1080p", "720p", "2160p", "480p", "4k", "uhd", "hdr", "hdr10", "dv",
    "bluray", "blu", "ray", "bdrip", "brrip", "webrip", "web", "dl", "webdl",
    "hdtv", "dvdrip", "dvd", "remux", "x264", "x265", "h264", "h265", "hevc",
    "avc", "aac", "ac3", "dts", "ddp", "atmos", "truehd", "10bit", "8bit",
    "extended", "remastered", "proper", "repack", "internal", "uncut",
    "multi", "chs", "cht", "gb", "big5", "hardsub", "hdr", "hq", "hd",
}
_ARTICLES = ("the ", "a ", "an ")
MIN_SCORE = 0.86


@dataclass(frozen=True)
class MovieEntry:
    """数据集里一部可生成资产的影片。"""

    movie_id: str
    title: str
    year: str
    normalized: str
    rating: float
    scenes: int
    has_frames: bool

    def as_dict(self) -> dict:
        return {"movie_id": self.movie_id, "title": self.title, "year": self.year,
                "rating": self.rating, "scenes": self.scenes}


@dataclass(frozen=True)
class MatchResult:
    """匹配结果：影片 + 命中方式（便于前端/日志说明依据）。"""

    movie: MovieEntry
    how: str
    score: float


def normalize(text: str) -> str:
    """归一化标题：小写、去标点、去冠词、压缩空格。"""
    lowered = text.lower().replace("&", " and ")
    cleaned = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", lowered)
    tokens = [t for t in cleaned.split() if t]
    # 去掉开头冠词（The Godfather 与 Godfather 视为同一部）
    while tokens and tokens[0] in ("the", "a", "an"):
        tokens = tokens[1:]
    return " ".join(tokens)


def parse_filename(filename: str) -> tuple[str, str]:
    """文件名 → (候选片名, IMDb id 或空串)。"""
    stem = Path(filename).stem
    imdb = _IMDB_RE.search(stem)
    if imdb:
        return "", imdb.group(1).lower()
    # 常见分隔符统一成空格，方括号/圆括号内容先剥离
    stem = re.sub(r"[\[\(].*?[\]\)]", " ", stem)
    stem = re.sub(r"[._\-]+", " ", stem)
    tokens = stem.split()
    kept: list[str] = []
    for token in tokens:
        low = token.lower()
        if _YEAR_RE.fullmatch(low) or low in _NOISE_TOKENS:
            continue
        if re.fullmatch(r"\d{3,4}p", low) or re.fullmatch(r"[0-9a-f]{8,}", low):
            continue
        kept.append(token)
    return " ".join(kept).strip(), ""


def build_index(root: Path) -> list[MovieEntry]:
    """扫描数据集，返回可生成资产的影片（有 meta + story 分段）。"""
    entries: list[MovieEntry] = []
    meta_dir = root / "meta_data" / "meta"
    ann_dir = root / "annotation_data" / "annotation"
    for meta_path in sorted(meta_dir.glob("*.json")):
        movie_id = meta_path.stem
        ann_path = ann_dir / f"{movie_id}.json"
        if not ann_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ann = json.loads(ann_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        story = ann.get("story") or []
        if not story:
            continue
        raw_title = str(meta.get("title") or movie_id)
        year = ""
        year_match = re.search(r"\((\d{4})\)\s*$", raw_title)
        if year_match:
            year = year_match.group(1)
            raw_title = raw_title[: year_match.start()].strip()
        try:
            rating = float(meta.get("imdb_rating") or 0)
        except (TypeError, ValueError):
            rating = 0.0
        entries.append(MovieEntry(
            movie_id=movie_id, title=raw_title, year=year,
            normalized=normalize(raw_title), rating=rating,
            scenes=len(story),
            has_frames=(root / "frames" / "frames" / movie_id).is_dir(),
        ))
    return entries


def match(filename: str, index: list[MovieEntry]) -> MatchResult | None:
    """文件名 → 最可能的影片；匹配不到返回 None。"""
    if not index:
        return None
    title, imdb = parse_filename(filename)
    if imdb:
        for entry in index:
            if entry.movie_id.lower() == imdb:
                return MatchResult(entry, "imdb_id", 1.0)
    candidate = normalize(title or filename)
    if not candidate:
        return None

    exact = [e for e in index if e.normalized == candidate]
    if exact:
        return MatchResult(_best(exact), "exact", 1.0)

    contained = [e for e in index
                 if e.normalized and (e.normalized in candidate
                                      or candidate in e.normalized)]
    if contained:
        return MatchResult(_best(contained), "contains", 0.95)

    best_entry, best_score = None, 0.0
    for entry in index:
        if not entry.normalized:
            continue
        score = SequenceMatcher(None, candidate, entry.normalized).ratio()
        if score > best_score:
            best_entry, best_score = entry, score
    if best_entry is not None and best_score >= MIN_SCORE:
        return MatchResult(best_entry, "fuzzy", round(best_score, 3))
    return None


def _best(entries: list[MovieEntry]) -> MovieEntry:
    """同分时取评分高、场景多的那部（Star Wars 这类同名系列）。"""
    return sorted(entries, key=lambda e: (-e.rating, -e.scenes, e.movie_id))[0]
