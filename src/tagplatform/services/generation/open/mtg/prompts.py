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

GENERATION_MARKER = "## 标签生成任务"
JUDGE_MARKER = "## 审核任务"

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
    dir_rule = (
        "- dir：从「可以放新标签的目录」中选择语义和适用粒度匹配的一项，填写其目录 id；"
        "只输出有合适目录的标签。\n" if with_dir else "")
    dir_field = '"dir":"目录 id",' if with_dir else ""
    if scope == "scene":
        goal = (
            "第一轮：为场景搜索提炼标签，scope 固定为 scene。"
            "想象用户记得这个片段的一个细节，或者想找同类片段时，会搜索什么。"
            "选择本场明确出现且有区分度的细节，用自然、可直接搜索的词组命名，"
            "比如「无声对峙」「拆除定时炸弹」。局部细节本身就可以有检索价值。"
            "以本场的描述、台词或画面为证据。scenes 只填写这一场的 ID。"
            "如有目录清单，选择能归入单场景目录的标签。"
        )
    else:
        goal = (
            "第二轮：结合影片材料和逐场候选，为观众提炼可独立展示的全片标签，scope 固定为 film。"
            "从反复出现的意象或情绪、人物变化、前后呼应和段落对照中找全片特征。"
            "逐场候选是检索线索，证据来自影片材料。允许发现候选里没有的全片特征。"
            "避免照抄局部标签、机械拼接场景标签或仅凭重复次数决定主题。"
            "每条标签至少引用两个不同场景，在 reason 中说清它们的共性或联系，"
            "以及这些细节为何足以概括全片。"
        )
    return (
        f"## 要求\n{goal}\n"
        f"最多 {max_tags} 条，按对当前用途的价值和证据强度降序排列。"
        "一个概念只保留一个说法。证据不足可以少给或返回空 tags。\n"
        "\n## 如何填写\n"
        "- tag：用简洁自然的中文词组命名，通常 2-8 字。\n"
        "- desc：用 10-32 字解释标签本身，写成可跨影片复用的词典式释义。\n"
        "- scenes：填写实际提供、且用于支撑标签的场景 ID。\n"
        "- reason：用一到两句完整的话指出可核对的细节，并说明这些细节如何支持标签。\n"
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
        label = _SCOPE_LABELS.get(entry.get("scope") or "", entry.get("scope") or "")
        lines.append(f"- {entry['dir_id']}｜{entry['path']}｜{label}")
    return (
        "## 可以放新标签的目录\n"
        "每个标签都要落到下面某个目录里，用**目录 id** 填 `dir` 字段。"
        "只能从这张表里选，不要自造 id（竖线后是目录全路径与挂载粒度）：\n"
        + "\n".join(lines) + "\n"
        "按标签本身的含义挑目录，不要看哪个目录空着就往哪塞；"
        "一条标签实在没有合适的目录，就**不要输出它**——宁可少一条，"
        "也不要放进不合适的目录。"
    )


def build_scene_generation_messages(scene: Scene, frames: tuple[Path, ...], *,
                                    content: ContentJSON, max_tags: int, dir_hint: list[dict],
                                    modalities=None) -> list[dict]:
    active = _active(modalities)
    body = render_scene(scene, active)
    if "script" in active:
        context = _render_film_context(content, _scene_cast(content, scene))
        body = ("### 影片背景（用于理解人物和剧情）\n" + context
                + "\n\n### 当前场景（标签证据）\n" + body)
    return _generation_messages(
        body,
        _scene_frame_parts(scene, frames) if "video" in active else [],
        None, dir_hint, scope="scene", max_tags=max_tags,
    )


def build_generation_messages(content: ContentJSON,
                              frames_by_scene: dict[str, tuple[Path, ...]], *,
                              max_tags: int = 15,
                              scene_tags: dict[str, list[dict]] | None = None,
                              library_hint: list[str] | None = None,
                              dir_hint: list[dict] | None = None,
                              modalities=None) -> list[dict]:
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
    return _generation_messages(body, frame_parts, library_hint, dir_hint,
                                scope="film", max_tags=max_tags)


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
                         max_tags: int | str) -> list[dict]:
    parts: list[dict] = [{
        "type": "text",
        "text": (f"{GENERATION_MARKER}\n"
                 "你是影视内容分析专家，任务是从影片内容中提炼语义标签。\n\n"
                 "## 内容\n" + body),
    }, *frame_parts]
    if library_hint:
        parts.append({"type": "text", "text": _library_hint_block(library_hint)})
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
             "scope": "{{目录适用粒度}}"}]
    budget = "{{本次请求候选上限}}"
    return {
        "messages": _generation_messages(body, frames, hints, dirs, scope=scope, max_tags=budget),
        "instructions_without_directory_hint": _generation_instructions(scope, budget),
    }


def _library_hint_block(names: list[str]) -> str:
    """库内已有标签的提示块（只给名字，不给定义）。

    措辞刻意留出"更精确说法"的口子：库里有"悬疑"时，模型仍可以给出
    "悬疑解谜"这类更细的候选，只是不该原样复述。
    """
    listing = "\n".join(f"- {name}" for name in names)
    return (
        "## 库里已有的标签\n"
        "以下是已收录的表述，供你对照。请输出尚未收录的表述，优先补充清单未覆盖的特征，"
        "或用片中证据提炼出比现有词更精确的标签：\n"
        f"{listing}"
    )


def build_judge_messages(content: ContentJSON, candidates: list[dict], *,
                         include_dialogue: bool = True) -> list[dict]:
    """构造 judge 请求：候选清单 + 内容证据，要求逐条判定。

    candidates 每项：{tag, dimension, scope, reason, scenes}
    """
    lines = []
    for candidate in candidates:
        reason = " ".join((candidate.get("reason") or "").splitlines())
        if candidate.get("scope"):
            ids = "、".join(candidate["scenes"])
            reason = f"〔{candidate['scope']}｜依据场景 {ids}〕{reason}"
        lines.append(candidate_line(candidate["tag"], candidate.get("dimension"), reason))
    modalities = {"script", "dialogue"} if include_dialogue else {"script"}
    return render_judge_messages(render_content(content, modalities), "\n".join(lines))


def render_judge_messages(body: str, candidate_lines: str) -> list[dict]:
    text = (
        f"{JUDGE_MARKER}\n"
        "你是严格的影视内容标签审核员。下面是候选标签与影片内容，"
        "请逐个判定候选标签是否被内容真实支持。\n\n"
        "## 候选标签\n" + candidate_lines + "\n\n"
        "## 影片内容\n" + body + "\n\n"
        "## 判定标准\n"
        "- supported：内容中有明确依据（如场景描述、台词、氛围或人物情绪）；\n"
        "- insufficient：内容不足以判断（不要臆测）；\n"
        "- contradicted：内容明显与标签矛盾；\n"
        "- importance：film 按全片展示价值评 core / secondary / incidental。"
        "scene 按本场检索价值评 core（精确且有区分度）/ secondary（相关但较泛）/ "
        "incidental（难以帮助定位片段），无需衡量在全片中的重要性。\n"
        "- confidence：0-1，你对判定结果的把握；\n"
        "- scenes：引用的场景 id，必须是上面出现过的 id；没有就空数组；\n"
        "- quote：从上面内容里**逐字摘录**的一句原文（10-25 字，可以是场景描述、"
        "台词、氛围或情绪词），用来让人核对你的依据。**不得改写、不得复述候选标签的"
        "理由**；找不到能摘的原文就判 insufficient。\n"
        "- reason：一句话说明这句原文怎么支撑（或不支撑）该标签，不要照抄候选理由。\n"
        "按候选标明的作用域审核。film 判 supported 时，scenes 必须列出至少两个共同支撑它的真实场景；"
        "只有一处依据时判 insufficient。scene 只在候选标明的依据场景内核验并引用原文。\n"
        "只有 quote 能在内容里逐字找到的判定才算数，没有原文依据一律 insufficient。\n"
        "宁可判 insufficient，也不要放过没有依据的标签。\n\n"
        "只输出一个 JSON 对象，不要解释、不要 Markdown 代码块：\n"
        '{"verdicts":[{"tag":"候选标签原文","verdict":"supported|insufficient|contradicted",'
        '"importance":"core|secondary|incidental","confidence":0.0,'
        '"scenes":["场景 id"],"quote":"原文逐字摘录","reason":"30 字以内"}]}'
    )
    return [{"role": "user", "content": text}]


def candidate_line(tag: str, dimension: str | None, reason: str | None) -> str:
    """审核候选行格式（Mock 与 judge 提示词共用，改动会同时影响两侧）。"""
    return f"- {tag}｜{dimension or '未分类'}｜{reason or ''}"
