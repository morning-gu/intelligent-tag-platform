"""字幕（SRT）解析与按场景截取。

数据源：`/cache/MovieNet/subtitle1K.v1.zip` → `subtitle/<imdb_id>.srt`
（815 部，覆盖全部 297 部可生成影片）。SRT 是标准格式：

```
1
00:02:06,167 --> 00:02:08,001
She isn't coming yet, Toto.

2
...
```

用途：给每个场景补台词。台词是**英文原文**（数据集只有英文字幕），只取
场景时间窗内的行，并按上限等距抽样——全量字幕一部片上千行、会让资产 JSON
从 22 KB 涨到 172 KB（台词占 85%），抽样是体积与证据价值的折中。
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
# 字幕里的样式/定位标记：<i> </i> <font ...> {\an8}
_TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]*\}")
# 听障字幕的音效标注：[door slams] (laughing) （音乐）
_SDH_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|（[^）]*）")
_MULTI_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SubtitleLine:
    """一行字幕：时间窗 + 文本（已清洗）。"""

    start_ms: int
    end_ms: int
    text: str


def _to_ms(match: re.Match) -> int:
    hours, minutes, seconds, millis = match.groups()
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 \
        + int(millis.ljust(3, "0")[:3])


def clean_text(raw: str) -> str:
    """去标签与音效标注、合并空白、去掉成对台词的前导连字符。

    `[Thud]`、`(laughing)` 这类听障字幕标注对「人物情绪」没有价值，直接去掉；
    去掉后若整行不剩字（纯音效行），退回原文，避免产出空台词。
    """
    text = _TAG_RE.sub("", raw)
    text = text.replace("\u200b", " ").replace("&nbsp;", " ")
    stripped = _SDH_RE.sub(" ", text)
    if stripped.strip(" .,-"):
        text = stripped
    # 成对字幕里的对话者分隔符："- Yes. - No." → "Yes. No."；行尾的 " -" 也去掉
    text = re.sub(r"\s+-\s*", " ", text)
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    if text.startswith("-"):
        text = text.lstrip("-").strip()
    return text


def parse_srt(raw: str) -> list[SubtitleLine]:
    """SRT 文本 → 行列表（跳过坏块，不抛异常）。"""
    lines: list[SubtitleLine] = []
    for block in re.split(r"\r?\n\s*\r?\n", raw):
        rows = [r for r in block.splitlines() if r.strip()]
        if len(rows) < 2:
            continue
        # 可选序号行在前；带 "-->" 的那行才是时间轴
        time_row = next((i for i, r in enumerate(rows) if "-->" in r), None)
        if time_row is None:
            continue
        stamps = list(_TIME_RE.finditer(rows[time_row]))
        if len(stamps) < 2:
            continue
        start_ms, end_ms = _to_ms(stamps[0]), _to_ms(stamps[1])
        text = clean_text(" ".join(rows[time_row + 1:]))
        if text and end_ms >= start_ms:
            lines.append(SubtitleLine(start_ms=start_ms, end_ms=end_ms, text=text))
    return lines


@lru_cache(maxsize=8)
def _read_srt(zip_path: str, movie_id: str) -> str | None:
    """从 zip 读一部片的 SRT 文本（lru_cache 避免同片反复解压）。"""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            return archive.read(f"subtitle/{movie_id}.srt").decode("utf-8", "replace")
    except (KeyError, FileNotFoundError, OSError):
        return None


def load_subtitles(zip_path: Path, movie_id: str) -> list[SubtitleLine]:
    """加载一部片的全部字幕行；没有字幕返回空列表。"""
    raw = _read_srt(str(zip_path), movie_id)
    return parse_srt(raw) if raw else []


def lines_for_scene(lines: list[SubtitleLine], start_ms: int, end_ms: int,
                    limit: int = 5) -> list[SubtitleLine]:
    """取与场景时间窗相交的字幕行，超过 limit 时等距抽样（保持时间序）。"""
    if not lines or limit <= 0:
        return []
    picked = [ln for ln in lines if ln.end_ms >= start_ms and ln.start_ms <= end_ms]
    if len(picked) <= limit:
        return picked
    if limit == 1:
        return [picked[len(picked) // 2]]
    step = (len(picked) - 1) / (limit - 1)
    idx = sorted({round(i * step) for i in range(limit)})
    return [picked[i] for i in idx]
