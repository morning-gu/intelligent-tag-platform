"""开放式生成的提示词：内容渲染、场景与影片生成、judge 审核请求。

移植自 MovieTagGen 的 04 生成 / 06 审核两段思路，但输入换成了任务一产出的
资产 JSON（中文场景描述 + 氛围 + 人物情绪 + 台词）加按场景对齐的关键帧。

两个契约必须与 `llm/mock.py` 咬合（测试与 mock 路径靠它识别分支）：
- 生成 prompt 含标志串 `## 标签生成任务`；
- 审核 prompt 含标志串 `## 审核任务`；
- 场景行统一为 `- {scene_id} [{start_ms}-{end_ms}] {description}`（与
  `services/generation/assemble._render_content_text` 同形，Mock 的 `_SCENE_LINE_RE` 直接可解析）；
- 审核候选行统一为 `- {tag}｜{dimension}｜{reason}`（全角竖线分隔）；
- 目录块标志串 `## 可以放新标签的目录`：只在有可扩展目录时出现，行格式
  `- {dir_id}｜{全路径}｜{粒度}`，模型的 `dir` 字段按这个 id 回填。

图片按场景内联：场景文字块之后紧跟该场景的关键帧（每张前有一行图注），
让模型知道每张图属于哪个场景——这是"场景级视觉证据"能被模型引用的前提。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from tagplatform.schemas import ContentJSON
from tagplatform.schemas.content import Character, Scene
from tagplatform.capabilities.prompts.mtg import (
    CATALOG_ONLY_HINT_TEXT, DIR_FIELD_RULE_TEXT, DIR_HINT_HEAD_TEXT, DIR_HINT_TAIL_TEXT,
    FILM_GOAL_TEXT, GENERATION_ROLE_TEXT, HOW_TO_FILL_TEXT,
    JUDGE_CRITERIA, JUDGE_DIR_BLOCK_HEAD_TEXT, JUDGE_DIR_FIT_TEXT,
    JUDGE_ROLE_BRIEF, LIBRARY_HINT_HEAD_TEXT, SCENE_GOAL_TEXT,
)

# 五个生成维度（键与 MovieTagGen 的 dimensions 对齐，值为中文展示名）
DIMENSIONS: dict[str, str] = {
    "style": "风格",
    "mood": "情绪",
    "theme": "主题",
    "narrative": "叙事",
    "character": "角色",
}

# 目录挂载粒度 → 提示词里的中文说明（只影响措辞，不参与校验）
_SCOPE_LABELS = {"content": "整片", "scene": "单场景", "character": "人物"}


def _scope_label(scope: str | None) -> str:
    """目录 scope 文本 → 提示词粒度标签。

    组合值（"content,scene,character"——治理导入目录的默认写法）逐词取
    标签后以"/"连接；未知词原样保留（与旧 .get 兜底一致）。
    """
    tokens = [t.strip() for t in (scope or "").split(",") if t.strip()]
    if not tokens:
        return scope or ""
    return "/".join(_SCOPE_LABELS.get(token, token) for token in tokens)

GENERATION_MARKER = "## 标签生成任务"
JUDGE_MARKER = "## 审核任务"

# judge 指令文案（JUDGE_ROLE_BRIEF / JUDGE_CRITERIA）自 2026-11-22 文案集中
# 起移驻 tagplatform.capabilities.prompts.mtg（本模块 import 消费）。

# prompt 内明细上限（控制 token；资产本身已按场景裁剪过台词）
_MAX_CAST = 8
_MAX_SCENE_DESC = 240
_MAX_SUMMARY = 600
# 台词不设上限：资产侧已按 asset_dialogue_limit 控量（8 条/场景），这里全用——
# 台词是情绪与主题最直接的证据，按条数筛掉的往往正是关键那句。


def encode_image(path: Path) -> str:
    """图片 → data URL（沿用资产生成链路的编码方式）。"""
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    return f"data:image/{suffix};base64,{base64.b64encode(path.read_bytes()).decode()}"


# 全部模态（默认口径）；`eval/ablation.py` 的 `MODALITIES` 用同一套名字
ALL_MODALITIES: frozenset[str] = frozenset({"video", "script", "dialogue"})


def _active(modalities) -> frozenset[str]:
    """None → 全开；否则只校验、不修正（拼错的名字必须当场报错）。"""
    if modalities is None:
        return ALL_MODALITIES
    names = frozenset(modalities)
    unknown = names - ALL_MODALITIES
    if unknown:
        raise ValueError(
            f"未知模态 {sorted(unknown)}，只支持 {sorted(ALL_MODALITIES)}")
    if not names:
        raise ValueError("至少要开启一种模态")
    return names


def render_content(content: ContentJSON, modalities=None, *, full_cast: bool = False) -> str:
    """资产 → 内容文本；`modalities` 控制 script / dialogue 两段是否出现。

    - 关掉 `script`：整段资产文本都不给（摘要/人物/场景行/氛围/情绪/地点/
      音乐全部属于“文字剧本”通道；只留“台词”没有场景上下文，模型分不清
      哪句属于哪场）；
    - 关掉 `dialogue`：逐行滤掉 `台词：` 开头的行（与 judge 侧
      `_render_judge_content` 的滤法同形）；
    - `video` 不在这里处理——关键帧在 `build_generation_messages` 里内联。

    默认（`modalities=None`）等价全模态，行为与旧版逐字一致。
    """
    active = _active(modalities)
    if "script" not in active:
        return ""
    characters = content.content.characters
    if not full_cast:
        characters = characters[:_MAX_CAST]
    return "\n".join([
        _render_film_context(content, characters),
        *(render_scene(scene, active) for scene in content.content.scenes),
    ])


def _render_film_context(content: ContentJSON, characters: list[Character]) -> str:
    """渲染片名、摘要和选定人物。场景内容由调用方按粒度附加。"""
    lines = [f"电影：《{content.asset.title}》",
             f"摘要：{content.content.summary[:_MAX_SUMMARY]}"]
    cast = [f"{ch.name}（{ch.description}）" if ch.description else ch.name
            for ch in characters]
    if cast:
        lines.append("人物：" + "、".join(cast))
    return "\n".join(lines)


def _scene_cast(content: ContentJSON, scene: Scene) -> list[Character]:
    """用出场关系、说话人及人物情绪定位本场相关人物，兼容名称和别名。"""
    references = set(scene.character_ids)
    for dialogue in scene.dialogues:
        references.update((dialogue.speaker_id, dialogue.speaker_name, dialogue.speaker))
    for emotion in scene.character_emotions:
        references.update((emotion.character_id, emotion.character))
    names = {name.strip().casefold() for name in references if name and name.strip()}
    return [
        character for character in content.content.characters
        if any(name.strip().casefold() in names
               for name in (character.character_id, character.name, *character.aliases))
    ]


def render_scene(scene: Scene, active: frozenset[str]) -> str:
    """只渲染该场材料。无剧本文本时保留场景标识供证据引用。"""
    if "script" in active:
        desc = scene.description[:_MAX_SCENE_DESC]
        lines = [f"- {scene.scene_id}"
                 f" [{scene.time_range.start_ms}-{scene.time_range.end_ms}] {desc}"]
        if scene.emotions:
            lines.append("  氛围：" + "、".join(scene.emotions))
        emotions = [f"{ce.character}（{ce.emotion}）"
                    for ce in scene.character_emotions[:6]]
        if not emotions:
            seen: set[str] = set()
            for dialogue in scene.dialogues:
                tone = dialogue.emotion.text_emotion if dialogue.emotion else None
                speaker = dialogue.speaker_name or dialogue.speaker
                if tone and speaker and speaker not in seen:
                    seen.add(speaker)
                    emotions.append(f"{speaker}（{tone}）")
                if len(emotions) >= 6:
                    break
        if emotions:
            lines.append("  人物情绪：" + "、".join(emotions))
        if scene.location:
            lines.append(f"  地点：{scene.location}")
        if scene.background_music:
            lines.append(f"  音乐：{scene.background_music}")
        if "dialogue" in active:
            for dialogue in scene.dialogues:
                tone = (f"（{dialogue.emotion.text_emotion}）"
                        if dialogue.emotion and dialogue.emotion.text_emotion else "")
                lines.append(f"  台词：{dialogue.speaker}{tone}：{dialogue.text}")
        return "\n".join(lines)
    return f"场景 ID：{scene.scene_id}"


def _generation_instructions(scope: str, max_tags: int | str, with_dir: bool = False) -> str:
    # goal 文案与"如何填写"块自文案集中起为中央常量（capabilities.prompts.mtg）
    dir_rule = DIR_FIELD_RULE_TEXT if with_dir else ""
    dir_field = '"dir":"目录 id",' if with_dir else ""
    goal = SCENE_GOAL_TEXT if scope == "scene" else FILM_GOAL_TEXT
    return (
        f"## 要求\n{goal}\n"
        f"最多 {max_tags} 条，按对当前用途的价值和证据强度降序排列。"
        "一个概念只保留一个说法。证据不足可以少给或返回空 tags。\n"
        f"{HOW_TO_FILL_TEXT}"
        f"{dir_rule}\n## 输出格式\n"
        "直接返回一个 JSON 对象，结构如下：\n"
        '{"tags":[{"tag":"2-8 字中文标签",' + dir_field +
        f'"scope":"{scope}",'
        '"desc":"10-32 字通用释义",'
        '"scenes":["实际场景 ID"],"reason":"可核对的细节及其支撑标签的理由"}]}'
    )


def _dir_hint_block(dirs: list[dict]) -> str:
    """可新增标签的目录清单（生成阶段就把归属交给模型决定）。

    只列可扩充目录（dir_dynamic/dir_asset）的目录：静态目录不允许扩充，
    出现在提示词里只会诱使模型往里面塞新词，然后在校验环节被打回。
    """
    lines = []
    for entry in dirs:
        label = _scope_label(entry.get("scope"))
        description = " ".join((entry.get("description") or "").split())
        line = f"- {entry['dir_id']}｜{entry['path']}｜{label}"
        lines.append(f"{line}｜{description}" if description else line)
    # 头尾说明文案为中央常量（capabilities.prompts.mtg），listing 行就地渲染
    return DIR_HINT_HEAD_TEXT + "\n".join(lines) + "\n" + DIR_HINT_TAIL_TEXT


def build_scene_generation_messages(scene: Scene, frames: tuple[Path, ...], *,
                                    content: ContentJSON, max_tags: int, dir_hint: list[dict],
                                    modalities=None,
                                    catalog_choices: list[str] | None = None) -> list[dict]:
    active = _active(modalities)
    body = render_scene(scene, active)
    if "script" in active:
        context = _render_film_context(content, _scene_cast(content, scene))
        body = ("### 影片背景（用于理解人物和剧情）\n" + context
                + "\n\n### 当前场景（标签证据）\n" + body)
    return _generation_messages(
        body,
        _scene_frame_parts(scene, frames) if "video" in active else [],
        catalog_choices, None if catalog_choices else dir_hint, scope="scene", max_tags=max_tags,
        catalog_only=bool(catalog_choices),
    )


def build_generation_messages(content: ContentJSON,
                              frames_by_scene: dict[str, tuple[Path, ...]], *,
                              max_tags: int = 15,
                              scene_tags: dict[str, list[dict]] | None = None,
                              library_hint: list[str] | None = None,
                              dir_hint: list[dict] | None = None,
                              modalities=None, catalog_only: bool = False) -> list[dict]:
    """影片级生成请求，给出按片序排列的每场候选（包括无候选的场景）。"""
    active = _active(modalities)
    scene_tags = scene_tags or {}
    frame_parts: list[dict] = []
    per_scene = []
    for scene in content.content.scenes:
        per_scene.append({
            "scene_id": scene.scene_id,
            "tags": [{key: item[key] for key in ("tag", "reason", "desc")}
                     for item in scene_tags.get(scene.scene_id, [])],
        })
        if "video" in active:
            frame_parts.extend(_scene_frame_parts(scene, frames_by_scene.get(scene.scene_id, ())))
    body = render_content(content, active, full_cast=True)
    body += "\n\n## 第一轮逐场候选\n" + json.dumps(per_scene, ensure_ascii=False, indent=2)
    return _generation_messages(body, frame_parts, library_hint,
                                None if catalog_only and library_hint else dir_hint,
                                scope="film", max_tags=max_tags, catalog_only=catalog_only)


def _scene_frame_parts(scene: Scene, frames: tuple[Path, ...]) -> list[dict]:
    return [
        part
        for order, frame in enumerate(frames, 1)
        for part in keyframe_parts(scene.scene_id, order, len(frames), encode_image(frame))
    ]


def keyframe_parts(scene_id: str, order: int | str, total: int | str,
                   image_url: str) -> list[dict]:
    return [
        {"type": "text", "text": f"〔关键帧〕{scene_id} 第 {order}/{total} 张"},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]


def _generation_messages(body: str, frame_parts: list[dict],
                         library_hint: list[str] | None,
                         dir_hint: list[dict] | None, *, scope: str,
                         max_tags: int | str, catalog_only: bool = False) -> list[dict]:
    parts: list[dict] = [{
        "type": "text",
        "text": (f"{GENERATION_MARKER}\n"
                 f"{GENERATION_ROLE_TEXT}" + body),
    }, *frame_parts]
    if library_hint:
        parts.append({"type": "text", "text": _library_hint_block(library_hint, catalog_only=catalog_only)})
    if dir_hint:
        parts.append({"type": "text", "text": _dir_hint_block(dir_hint)})
    parts.append({"type": "text",
                  "text": _generation_instructions(scope, max_tags, with_dir=bool(dir_hint))})
    return [{"role": "user", "content": parts}]


def build_generation_template(scope: str) -> dict:
    if scope == "scene":
        body = ("### 影片背景（用于理解人物和剧情）\n"
                "{{影片标题、摘要及当前场景相关人物介绍}}\n\n"
                "### 当前场景（标签证据）\n"
                "{{当前场景 ID、时间、描述、氛围、人物情绪、地点、音乐及台词}}")
        hints = None
    else:
        body = ("{{影片标题、摘要、完整人物表和按片序排列的场景文本及台词}}\n\n"
                "## 第一轮逐场候选\n"
                "{{每场的 scene_id 和 tags，候选含 tag、reason、desc}}")
        hints = ["{{召回的库内标签名，每个词一行}}"]
    frames = keyframe_parts("{{场景 ID}}", "{{帧序号}}", "{{该场景帧数}}",
                          "{{关键帧的完整 data URL}}")
    dirs = [{"dir_id": "{{目录 ID}}", "path": "{{目录全路径}}",
             "scope": "{{目录适用粒度}}", "description": "{{目录定义}}"}]
    budget = "{{本次请求候选上限}}"
    return {
        "messages": _generation_messages(body, frames, hints, dirs, scope=scope, max_tags=budget),
        "instructions_without_directory_hint": _generation_instructions(scope, budget),
    }


def _library_hint_block(names: list[str], *, catalog_only: bool = False) -> str:
    """库内已有标签的提示块（只给名字，不给定义）。

    措辞刻意留出"更精确说法"的口子：库里有"悬疑"时，模型仍可以给出
    "悬疑解谜"这类更细的候选，只是不该原样复述。
    """
    listing = "\n".join(f"- {name}" for name in names)
    heading = CATALOG_ONLY_HINT_TEXT if catalog_only else LIBRARY_HINT_HEAD_TEXT
    return heading + listing


def build_judge_messages(content: ContentJSON, candidates: list[dict], *,
                         include_dialogue: bool = True,
                         dirs: list[dict] | None = None) -> list[dict]:
    """构造 judge 请求：候选清单 + 内容证据，要求逐条判定。

    candidates 每项：{tag, dimension, scope, reason, scenes, dir}
    dirs 非空时追加目录契合段（候选行标出各自的目标目录 id），judge 必须
    逐条给 in_dir——生成侧的目录约束只是提示词，这里才是硬闸。
    """
    lines = []
    for candidate in candidates:
        reason = " ".join((candidate.get("reason") or "").splitlines())
        marks: list[str] = []
        if candidate.get("scope"):
            marks.append(candidate["scope"])
            marks.append("依据场景 " + "、".join(candidate["scenes"]))
        if candidate.get("dir"):
            marks.append(f"目录 {candidate['dir']}")
        if marks:
            reason = f"〔{'｜'.join(marks)}〕{reason}"
        lines.append(candidate_line(candidate["tag"], candidate.get("dimension"), reason))
    modalities = {"script", "dialogue"} if include_dialogue else {"script"}
    return render_judge_messages(render_content(content, modalities), "\n".join(lines),
                                 dir_block=judge_dir_block(dirs or []))


def judge_dir_block(dirs: list[dict]) -> str:
    """judge 侧目录块：目录 id｜全路径｜定义 + 契合判定要求（空清单 → 空串）。"""
    if not dirs:
        return ""
    lines = []
    for entry in dirs:
        description = " ".join((entry.get("description") or "").split())
        line = f"- {entry['dir_id']}｜{entry['path']}"
        lines.append(f"{line}｜{description}" if description else line)
    return JUDGE_DIR_BLOCK_HEAD_TEXT + "\n".join(lines) + "\n" + JUDGE_DIR_FIT_TEXT


def render_judge_messages(body: str, candidate_lines: str, *,
                          dir_block: str = "") -> list[dict]:
    text = (
        f"{JUDGE_MARKER}\n"
        f"{JUDGE_ROLE_BRIEF}"
        "## 候选标签\n" + candidate_lines + "\n\n"
        "## 影片内容\n" + body + "\n\n"
        f"{JUDGE_CRITERIA}" + dir_block
    )
    return [{"role": "user", "content": text}]


def candidate_line(tag: str, dimension: str | None, reason: str | None) -> str:
    """审核候选行格式（Mock 与 judge 提示词共用，改动会同时影响两侧）。"""
    return f"- {tag}｜{dimension or '未分类'}｜{reason or ''}"
