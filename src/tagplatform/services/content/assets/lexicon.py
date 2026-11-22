"""发布组/技术噪音词表（单一来源，dataset 与 asset_build 共用）。

原为两套各自漂移的词表（`assets/dataset.py` 与 `asset_build.py` 各一份
`_NOISE_TOKENS`），2026-11 顺修清单 Task 16 取**并集**统一：两侧原有过滤
行为都保留，更全的词表 = 更激进的噪音过滤（`Memento.1080p.BluRay.x264`
与标题 `Memento` 是同一部片，文件名里的发布信息一律不参与片名比对）。

代价是双侧都可能多剥掉一个词（如数据集侧此前不剥 `蓝光/中字`、资产侧此前
不剥 `hardsub/uncut`）——标题恰好含这些词的极端情形会被过滤，统一口径
优于两侧行为分叉。改动词表时两个消费方（dataset.parse_filename /
asset_build.name_tokens）同时生效。

`YEAR_RE` 是年份匹配的共用正则（两侧原为等价的两种锚定写法，统一成
非锚定模式 + fullmatch）。
"""
from __future__ import annotations

import re

# 文件名里的发布组噪音：出现即从标题里剥离（并集，见模块 docstring）
RELEASE_GROUP_NOISE: frozenset[str] = frozenset({
    "10bit", "1080p", "2160p", "480p", "4k", "720p", "8bit", "8k",
    "aac", "ac3", "atmos", "avc",
    "bdrip", "big5", "blu", "bluray", "brrip",
    "cht", "chs", "cut",
    "dd", "ddp", "directors", "dl", "dual", "dv", "dvd", "dvdrip", "dts",
    "eac3", "eng", "extended",
    "flac",
    "gb",
    "h264", "h265", "hardsub", "hdr", "hdr10", "hd", "hdrip", "hevc", "hdtv", "hq",
    "imax", "internal",
    "jpn",
    "kor",
    "mp3", "multi",
    "proper",
    "remastered", "remux", "repack", "ray",
    "sdr",
    "truehd",
    "uhd", "uncut", "unrated",
    "web", "webdl", "webrip",
    "x264", "x265",
    "国语", "中字", "双语", "字幕", "高清", "粤语", "蓝光",
})

# 年份（19xx/20xx）：配 fullmatch 使用（整词匹配才剥离）
YEAR_RE = re.compile(r"(19|20)\d{2}")
