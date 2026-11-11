"""精简资产构建：数据集影片 → 对齐《长视频理解数据》契约的资产 JSON。

输出严格按上游模板（`new_json.txt`，2026-11 第二版）：

```jsonc
{
  "schema_version": "1.0",
  "asset": {"asset_id","asset_type","title","duration_ms","video_uri","language"},
  "content": {
    "description": "整片剧情（中文）",
    "characters": [{"character_id","name","aliases","description"}],
    "scenes": [{
      "scene_id","start_ms","end_ms","description",
      "virtual_time","location","character_ids",
      "events":[{"event_id","description",
                 "participant_ids":[{"character_id","emotion":[{"name","code"}]}]}],
      "emotion":[{"name","code"}],                 // 场景氛围
      "dialogues":[{"dialogue_id","speaker_id","speaker_name","text",
                    "start_ms","end_ms"}],          // 台词行没有情绪字段
      "audio_events":[]                            // 数据集无音轨，恒为空
    }]
  }
}
```

数据来源（MovieNet）：
- 剧情：meta `synopsis/plot/overview` + `annotation.story[].description`
- 台词：`subtitle1K.v1.zip` 的 SRT，按场景时间窗截取、等距抽样
- 画面：`frames/frames/<tt>/shot_XXXX_img_Y.jpg`，按 story 的 `shot` 区间取帧
- 氛围/人物情绪/事件/说话人：多模态模型看关键帧 + 读剧情 + 读字幕行后给出

本模块不依赖网络：LLM 客户端从外部注入（`complete(messages, run_id)`），
测试用 FakeClient；CLI 在 `scripts/build_assets.py`。
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from tagplatform.assets.dataset import normalize
from tagplatform.assets.emotions import FEELINGS, MOODS, prompt_catalog, to_emotions
from tagplatform.assets.subtitles import SubtitleLine, lines_for_scene, load_subtitles

FRAME_NAME_RE = re.compile(r"shot_(\d+)_img_(\d+)\.jpg$")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 场景分析必须返回的键（缺任一视为解析失败，宁可重试也不写脏数据）
_SCENE_KEYS = ("description", "atmosphere")

# 生成口径版本：写进 media_asset.source（asset_builder:v5-infer /
# asset_builder:v5-facts）。改了提示词或输出字段就 bump，重新生成的内容
# 能看出是哪一版产的。
ASSET_BUILDER_VERSION = "v5"


def source_tag(infer: bool) -> str:
    """本口径的 source 标识（编码与 main 分支一致，双方内容可互认复用）。

    main：`asset_builder:<版本>-<模式>`；本地历史为 `<版本>-<模式>`（已弃用，
    存量内容会因口径不符被判为「需要重生成」一次）。
    """
    return f"asset_builder:{ASSET_BUILDER_VERSION}-{'infer' if infer else 'facts'}"


# 整片描述最短长度：低于此判为残片（实测模型偶尔只回 "[1945]"）
MIN_SUMMARY_CHARS = 60


@dataclass
class ScenePlan:
    """一个场景的输入事实（尚未经过模型）。"""

    scene_id: str
    start_ms: int
    end_ms: int
    source_description: str
    frames: list[Path] = field(default_factory=list)
    dialogues: list[SubtitleLine] = field(default_factory=list)


@dataclass
class MovieStats:
    """一部片的调用与失败统计（写进 manifest，便于对账成本）。"""

    movie_id: str
    scenes: int = 0
    scenes_with_frames: int = 0
    scenes_with_dialogues: int = 0
    failed_scenes: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0
    output: str | None = None

    def as_dict(self) -> dict:
        return {
            "movie_id": self.movie_id, "scenes": self.scenes,
            "scenes_with_frames": self.scenes_with_frames,
            "scenes_with_dialogues": self.scenes_with_dialogues,
            "failed_scenes": self.failed_scenes,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed_s": round(self.elapsed_s, 1), "output": self.output,
        }


# ---------------------------------------------------------------- 关键帧

def pick_keyframes(frames_dir: Path, shot_range: list[int], k: int = 3) -> list[Path]:
    """在 story 的 shot 区间内等距取 k 张关键帧（无帧返回空列表）。"""
    if not shot_range or k <= 0 or not frames_dir.is_dir():
        return []
    lo, hi = min(shot_range), max(shot_range)
    files: list[Path] = []
    for path in sorted(frames_dir.iterdir()):
        m = FRAME_NAME_RE.search(path.name)
        if m and lo <= int(m.group(1)) <= hi:
            files.append(path)
    if len(files) <= k:
        return files
    if k == 1:
        return [files[len(files) // 2]]
    idx = sorted({round(i * (len(files) - 1) / (k - 1)) for i in range(k)})
    return [files[i] for i in idx]


def encode_image(path: Path) -> str:
    """图片 → data URL（模型侧按 image_url 收 base64）。"""
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    return f"data:image/{suffix};base64,{base64.b64encode(path.read_bytes()).decode()}"


# ---------------------------------------------------------------- 提示词

def _mmss(ms: int) -> str:
    total = ms // 1000
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def scene_messages(title: str, year: str, genres: list[str],
                   cast_names: list[str], plan: ScenePlan,
                   image_urls: list[str]) -> list[dict]:
    """单场景分析请求：剧情原文 + 字幕行 + 关键帧 → 模板字段。"""
    cast = "、".join(cast_names[:25]) or "（未提供，可按剧情推断但不要编造姓名）"
    lines = plan.dialogues
    if lines:
        dialogue_block = "\n".join(
            f"{i}. [{_mmss(ln.start_ms)}] {ln.text}" for i, ln in enumerate(lines, 1))
    else:
        dialogue_block = "（本场景无字幕行）"
    text = (
        "你是影视内容分析助手。请结合这场戏的原始剧情、字幕行与关键帧，"
        "输出中文结构化分析。\n\n"
        f"电影：《{title}》({year})，类型：{'、'.join(genres) or '未知'}\n"
        f"场景时间：{_mmss(plan.start_ms)} – {_mmss(plan.end_ms)}\n"
        f"已知角色（人物情绪与说话人必须从这里选，不要编造）：{cast}\n"
        f"该场景的原始剧情（英文）：\n{plan.source_description.strip()}\n\n"
        f"该场景的字幕行（英文，编号即行号）：\n{dialogue_block}\n\n"
        "要求：\n"
        "1. description：本场景的中文剧情描述，120–220 字，写清谁在哪里做了什么、"
        "冲突或转折是什么；不要复述整片剧情。\n"
        f"2. atmosphere：场景整体氛围，从这张表里选 1–3 个：{prompt_catalog(MOODS)}。\n"
        "3. events：本场景发生的事件概述，0–2 条；participants 为参与角色，"
        f"每人给 0–2 个情绪，只能从这张表里选：{prompt_catalog(FEELINGS)}。\n"
        "4. dialogues：对上面每一行字幕给出 speaker（说话人，推不出就填空串）。line 是行号。\n"
        "5. virtual_time（剧情时间，如“1945年夏末 / 现代，深夜23:40，暴雨”）"
        "与 location（地点），不确定就填空串。\n"
        "6. 人名一律保留影片原名（英文），不要音译。\n\n"
        '只输出 JSON：{"description":"...","atmosphere":["悬疑神秘"],'
        '"events":[{"description":"...","participants":[{"name":"...",'
        '"emotions":["愤怒"]}]}],"virtual_time":"","location":"",'
        '"dialogues":[{"line":1,"speaker":"..."}]}'
    )
    content: list[dict] = [{"type": "text", "text": text}]
    content.extend({"type": "image_url", "image_url": {"url": url}}
                   for url in image_urls)
    return [{"role": "user", "content": content}]


def summary_messages(title: str, year: str, genres: list[str],
                     synopsis: str) -> list[dict]:
    """整片剧情摘要请求：英文简介 → 200–300 字中文（JSON 模式，键 summary）。"""
    text = (
        "把下面这部影片的英文剧情简介改写成中文整片剧情描述。\n"
        f"电影：《{title}》({year})，类型：{'、'.join(genres) or '未知'}\n"
        f"英文简介：\n{synopsis.strip()}\n\n"
        "要求：200–300 字，交代主要人物、核心冲突与结局走向；"
        "人名保留影片原名（英文），不要音译。\n"
        '只输出 JSON：{"summary": "中文剧情描述"}'
    )
    return [{"role": "user", "content": text}]


# ---------------------------------------------------------------- 解析

def _extract_json_object(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象（容忍 ``` 围栏与前后废话）。"""
    if not text:
        return None
    fenced = _FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _clean_list(value, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def parse_scene_result(text: str) -> dict | None:
    """校验场景分析结果：description 必填，其余按类型清洗；坏结构返回 None。"""
    data = _extract_json_object(text)
    if data is None or any(k not in data for k in _SCENE_KEYS):
        return None
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    atmosphere = data.get("atmosphere")
    if not isinstance(atmosphere, list):
        return None

    events: list[dict] = []
    for item in data.get("events") or []:
        if isinstance(item, dict):
            desc = str(item.get("description", "")).strip()
            if desc:
                participants: list[dict] = []
                for person in item.get("participants") or []:
                    if not isinstance(person, dict):
                        continue
                    name = str(person.get("name", "")).strip()
                    if not name:
                        continue
                    participants.append({
                        "name": name,
                        "emotions": _clean_list(person.get("emotions"), 3),
                    })
                events.append({"description": desc, "participants": participants})
    dialogue_rows: list[dict] = []
    for item in data.get("dialogues") or []:
        if not isinstance(item, dict):
            continue
        try:
            line = int(item.get("line"))
        except (TypeError, ValueError):
            continue
        dialogue_rows.append({
            "line": line,
            "speaker": str(item.get("speaker", "")).strip(),
        })
    return {
        "description": description.strip(),
        "atmosphere": _clean_list(atmosphere, 5),
        "events": events[:2],
        "virtual_time": str(data.get("virtual_time") or "").strip(),
        "location": str(data.get("location") or "").strip(),
        "dialogues": dialogue_rows,
    }


def parse_summary_result(text: str) -> str:
    """从模型输出取整片描述：优先 {"summary"/"description"}，其次裸文本。"""
    data = _extract_json_object(text)
    candidate = ""
    if isinstance(data, dict):
        candidate = str(data.get("summary") or data.get("description") or "")
    if not candidate:
        candidate = (text or "").strip()
    candidate = candidate.strip()
    return candidate if len(candidate) >= MIN_SUMMARY_CHARS else ""


# ---------------------------------------------------------------- 角色表

class CastIndex:
    """角色名 → character_id 的映射（大小写/标点不敏感）。

    入参放宽：可传角色名（演员未知），也可传 `(角色名, 演员名)`。
    模板没有演员字段，演员写进 `description`（"由 Marlon Brando 饰演"），
    输出字段集与契约示例 JSON 逐字一致。
    """

    def __init__(self, entries: list[str | tuple[str, str | None]]) -> None:
        self.characters: list[dict] = []
        self._by_norm: dict[str, str] = {}
        for index, item in enumerate(entries, start=1):
            if isinstance(item, str):
                name, actor = item, None
            else:
                name, actor = item[0], item[1]
            cid = f"character_{index:03d}"
            # 模板没有演员字段：演员写进 description，字段集与示例 JSON 逐字一致
            self.characters.append({
                "character_id": cid, "name": name, "aliases": [],
                "description": f"由 {actor} 饰演" if actor else None,
            })
            self._by_norm.setdefault(normalize(name), cid)

    def cid(self, name: str) -> str | None:
        return self._by_norm.get(normalize(name))


# ---------------------------------------------------------------- 组装

def _scene_to_template(plan: ScenePlan, parsed: dict, cast: CastIndex,
                       scene_index: int) -> dict:
    """模型结果 + 字幕行 → 上游模板里的一个场景对象。

    字段集与 new_json.txt 逐字一致：人物情绪挂在事件的参与人上
    （`events[].participant_ids[].emotion`），台词行本身没有情绪字段。
    """
    character_ids: list[str] = []
    events: list[dict] = []
    for item in parsed.get("events") or []:
        participants: list[dict] = []
        seen: list[str] = []
        for person in item.get("participants") or []:
            cid = cast.cid(person["name"])
            if not cid or cid in seen:
                continue
            seen.append(cid)
            if cid not in character_ids:
                character_ids.append(cid)
            participants.append({
                "character_id": cid,
                "emotion": to_emotions(person.get("emotions"), FEELINGS),
            })
        if not participants:
            continue
        events.append({
            "event_id": "",  # 组装完统一编号（event_001 起）
            "description": item["description"],
            "participant_ids": participants,
        })

    # 字幕行 → 模板台词；模型给的 speaker 按行号贴回去
    by_line = {row["line"]: row for row in parsed.get("dialogues") or []}
    dialogues: list[dict] = []
    for index, line in enumerate(plan.dialogues, start=1):
        row = by_line.get(index, {})
        speaker = (row.get("speaker") or "").strip()
        cid = cast.cid(speaker) if speaker else None
        if cid and cid not in character_ids:
            character_ids.append(cid)
        dialogues.append({
            "dialogue_id": "",  # 组装完统一编号（dialogue_001 起）
            "speaker_id": cid,
            "speaker_name": speaker or None,
            "text": line.text,
            "start_ms": line.start_ms,
            "end_ms": line.end_ms,
        })

    scene = {
        "scene_id": "",  # 组装完统一编号（scene_001 起）
        "start_ms": plan.start_ms,
        "end_ms": plan.end_ms,
        "description": parsed["description"],
        "virtual_time": parsed.get("virtual_time") or None,
        "location": parsed.get("location") or None,
        "character_ids": character_ids,
        "events": events,
        "emotion": to_emotions(parsed.get("atmosphere"), MOODS),
        "dialogues": dialogues,
        # 数据集无音轨：字段保留、恒为空（与“不用音频”的口径一致）
        "audio_events": [],
    }
    return scene


def renumber_ids(scenes: list[dict]) -> None:
    """按契约风格统一编号：scene_001 / event_001 / dialogue_001（全片连续）。"""
    event_no = 0
    dialogue_no = 0
    for index, scene in enumerate(scenes, start=1):
        scene["scene_id"] = f"scene_{index:03d}"
        for event in scene["events"]:
            event_no += 1
            event["event_id"] = f"event_{event_no:03d}"
        for dialogue in scene["dialogues"]:
            dialogue_no += 1
            dialogue["dialogue_id"] = f"dialogue_{dialogue_no:03d}"


def assemble_asset(movie_id: str, title: str, language: str, duration_ms: int,
                   summary: str, cast: CastIndex, scenes: list[dict]) -> dict:
    """拼成上游模板格式的资产 JSON（字段顺序固定，便于 diff）。"""
    return {
        "schema_version": "1.0",
        "asset": {
            "asset_id": f"movie_{movie_id}",
            "asset_type": "movie",
            "title": title,
            "duration_ms": duration_ms,
            "video_uri": None,
            "language": language,
        },
        "content": {
            "description": summary,
            "characters": cast.characters,
            "scenes": scenes,
        },
    }


# ---------------------------------------------------------------- 编排

def plan_scenes(root: Path, movie_id: str, annotation: dict, k: int = 3,
                subtitles: list[SubtitleLine] | None = None,
                dialogue_limit: int = 5) -> list[ScenePlan]:
    """annotation.story → ScenePlan 列表（含帧路径与该场景的字幕行）。"""
    frames_dir = root / "frames" / "frames" / movie_id
    plans: list[ScenePlan] = []
    for index, seg in enumerate(annotation.get("story") or []):
        duration = seg.get("duration") or [0, 0]
        start_ms = max(0, int(round(float(duration[0]) * 1000)))
        end_ms = max(start_ms, int(round(float(duration[1]) * 1000)))
        scene_dialogues = (lines_for_scene(subtitles, start_ms, end_ms,
                                           limit=dialogue_limit)
                           if subtitles else [])
        plans.append(ScenePlan(
            scene_id=f"{movie_id}_story_{index:03d}",
            start_ms=start_ms, end_ms=end_ms,
            source_description=(seg.get("description") or "").strip(),
            frames=pick_keyframes(frames_dir, seg.get("shot") or [], k),
            dialogues=scene_dialogues,
        ))
    return plans


def _meta_facts(meta: dict, movie_id: str) -> tuple[str, str, list[str]]:
    """meta → (标题, 年份, 类型)。"""
    raw_title = str(meta.get("title") or movie_id)
    year = ""
    m = re.search(r"\((\d{4})\)\s*$", raw_title)
    if m:
        year = m.group(1)
        raw_title = raw_title[: m.start()].strip() or raw_title
    genres = [str(g) for g in (meta.get("genres") or [])]
    return raw_title, year, genres


def _synopsis(meta: dict) -> str:
    for key in ("synopsis", "plot", "overview", "storyline"):
        value = (meta.get(key) or "").strip()
        if value:
            return value
    return ""


def _cast_entries(meta: dict, limit: int = 25) -> list[tuple[str, str | None]]:
    """meta.cast → [(角色名, 演员名)]（角色名缺失时退回演员名）。"""
    entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for member in meta.get("cast") or []:
        actor = (member.get("name") or "").strip() or None
        character = (member.get("character") or "").strip() or actor
        if character and character not in seen:
            seen.add(character)
            entries.append((character, actor))
        if len(entries) >= limit:
            break
    return entries


def _add_usage(a: dict, b: dict) -> dict:
    """两次调用的 usage 累加（重试场景）。"""
    keys = ("prompt_tokens", "completion_tokens")
    return {**a, **{k: int(a.get(k, 0) or 0) + int(b.get(k, 0) or 0) for k in keys}}


class AssetBuilder:
    """单部影片 → 上游模板格式的资产 JSON。客户端注入，便于离线测试。"""

    def __init__(self, root: Path, out_dir: Path, client, k: int = 3,
                 workers: int = 2, sleep: float = 0.0, dialogue_limit: int = 5,
                 subtitles_zip: Path | None = None, progress_cb=None,
                 infer: bool = True):
        self.root = Path(root)
        self.out_dir = Path(out_dir)
        self.client = client
        self.k = k
        self.workers = max(1, workers)
        self.sleep = sleep
        self.dialogue_limit = dialogue_limit
        self.progress_cb = progress_cb
        # True = 让模型推断数据集没有的字段（描述/氛围/事件/说话人/时间地点）
        # False = 只填数据集事实，一次模型调用都不发
        self.infer = infer
        self.subtitles_zip = Path(subtitles_zip) if subtitles_zip else \
            self.root / "subtitle1K.v1.zip"

    # -- 单次调用 --

    def _call(self, messages: list[dict], run_id: str) -> tuple[str, dict]:
        """一次补全 → (文本, usage)。失败返回空串（由调用方判定降级）。"""
        resp = self.client.complete(messages, run_id)
        usage = getattr(resp, "usage", {}) or {}
        text = getattr(resp, "content", "") or ""
        if not getattr(resp, "ok", False):
            return "", usage
        if self.sleep:
            time.sleep(self.sleep)
        return text, usage

    # -- 单场景 --

    def analyze_scene(self, plan: ScenePlan, title: str, year: str,
                      genres: list[str], cast_names: list[str],
                      cast: CastIndex) -> tuple[dict, dict]:
        """一个场景 → (模板场景对象, usage)；两次都解析失败则退回原始英文描述。

        self.infer=False 时不调模型：只填数据集确有的字段（英文场景描述、
        字幕原文与时间），氛围/事件/剧情时间/地点/说话人一律留空。
        """
        if not self.infer:
            parsed = {"description": plan.source_description, "atmosphere": [],
                      "events": [], "virtual_time": "", "location": "",
                      "dialogues": []}
            return _scene_to_template(plan, parsed, cast, 0), {}
        image_urls = [encode_image(p) for p in plan.frames]
        messages = scene_messages(title, year, genres, cast_names, plan, image_urls)
        text, usage = self._call(messages, plan.scene_id)
        parsed = parse_scene_result(text)
        if parsed is None:
            text2, usage2 = self._call(messages, plan.scene_id + "#retry")
            usage = _add_usage(usage, usage2)
            parsed = parse_scene_result(text2)
        if parsed is None:
            parsed = {"description": plan.source_description, "atmosphere": [],
                      "events": [], "virtual_time": "", "location": "",
                      "dialogues": []}
        return _scene_to_template(plan, parsed, cast, 0), usage

    # -- 整片 --

    def _summarize(self, movie_id: str, title: str, year: str,
                   genres: list[str], synopsis: str,
                   fallback: str) -> tuple[str, dict]:
        """整片中文描述：一次调用 + 残片重试，仍失败退回场景描述拼接。"""
        if not self.infer:
            # 不让模型写：数据集只有英文简介/剧情，直接用原文
            return (synopsis.strip() or fallback), {}
        messages = summary_messages(title, year, genres, synopsis)
        text, usage = self._call(messages, f"summary_{movie_id}")
        summary = parse_summary_result(text)
        if not summary:
            text2, usage2 = self._call(messages, f"summary_{movie_id}#retry")
            usage = _add_usage(usage, usage2)
            summary = parse_summary_result(text2)
        return (summary or fallback), usage

    def build_movie(self, movie_id: str, force: bool = False) -> MovieStats:
        stats = MovieStats(movie_id=movie_id)
        started = time.time()
        out_path = self.out_dir / f"movie_{movie_id}.json"
        if out_path.exists() and not force:
            stats.output = str(out_path)
            stats.elapsed_s = time.time() - started
            return stats

        meta = json.loads((self.root / "meta_data" / "meta" / f"{movie_id}.json")
                          .read_text(encoding="utf-8"))
        annotation = json.loads(
            (self.root / "annotation_data" / "annotation" / f"{movie_id}.json")
            .read_text(encoding="utf-8"))
        title, year, genres = _meta_facts(meta, movie_id)
        cast_entries = _cast_entries(meta)
        cast_names = [name for name, _ in cast_entries]
        cast = CastIndex(cast_entries)
        subtitles = load_subtitles(self.subtitles_zip, movie_id)
        plans = plan_scenes(self.root, movie_id, annotation, self.k,
                            subtitles=subtitles, dialogue_limit=self.dialogue_limit)
        stats.scenes = len(plans)
        stats.scenes_with_frames = sum(1 for p in plans if p.frames)
        stats.scenes_with_dialogues = sum(1 for p in plans if p.dialogues)
        if self.progress_cb is not None:
            # 先报总数：前端一开始就能显示「0/6」，不必等第一个场景跑完
            self.progress_cb(0, len(plans))

        if self.workers == 1:
            pairs = [self.analyze_scene(p, title, year, genres, cast_names, cast)
                     for p in plans]
        else:
            import threading
            from concurrent.futures import ThreadPoolExecutor

            lock = threading.Lock()
            total = len(plans)
            done = [0]

            def _run(plan: ScenePlan):
                pair = self.analyze_scene(plan, title, year, genres,
                                          cast_names, cast)
                if self.progress_cb is not None:
                    with lock:
                        done[0] += 1
                        current = done[0]
                    self.progress_cb(current, total)
                return pair

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                pairs = list(pool.map(_run, plans))

        scenes: list[dict] = []
        for plan, (scene, usage) in zip(plans, pairs):
            stats.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            stats.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
            # 纯事实模式（infer=False）下氛围本来就该为空，不算降级
            if self.infer and (not scene["description"] or not scene["emotion"]):
                stats.failed_scenes.append(plan.scene_id)
            scenes.append(scene)

        fallback = "；".join(s["description"] for s in scenes[:3])[:400]
        summary, usage = self._summarize(movie_id, title, year, genres,
                                         _synopsis(meta), fallback)
        stats.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        stats.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        if not summary:
            stats.failed_scenes.append("__summary__")
        elif summary == fallback:
            stats.failed_scenes.append("__summary_fallback__")

        renumber_ids(scenes)
        duration_ms = plans[-1].end_ms if plans else 0
        # language 只在数据集确实给了才填，不替它猜
        asset = assemble_asset(movie_id, title, meta.get("language"),
                               duration_ms, summary, cast, scenes)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(asset, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        stats.output = str(out_path)
        stats.elapsed_s = time.time() - started
        return stats

    def build_many(self, movie_ids: list[str], force: bool = False) -> list[dict]:
        results = []
        for movie_id in movie_ids:
            stats = self.build_movie(movie_id, force=force)
            results.append(stats.as_dict())
            print(json.dumps(stats.as_dict(), ensure_ascii=False))
        manifest = {"total": len(results), "movies": results,
                    "prompt_tokens": sum(r["prompt_tokens"] for r in results),
                    "completion_tokens": sum(r["completion_tokens"] for r in results)}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return results
