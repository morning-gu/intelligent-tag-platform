"""开放式生成的提示词：内容渲染、多轮生成请求、judge 审核请求。

移植自 MovieTagGen 的 04 生成 / 06 审核两段思路，但输入换成了任务一产出的
资产 JSON（中文场景描述 + 氛围 + 人物情绪 + 台词）加按场景对齐的关键帧。

两个契约必须与 `llm/mock.py` 咬合（测试与 mock 路径靠它识别分支）：
- 生成 prompt 含标志串 `## 标签生成任务`；
- 审核 prompt 含标志串 `## 审核任务`；
- 场景行统一为 `- {scene_id} [{start_ms}-{end_ms}] {description}`（与
  `tasks/runner._render_content_text` 同形，Mock 的 `_SCENE_LINE_RE` 直接可解析）；
- 审核候选行统一为 `- {tag}｜{dimension}｜{reason}`（全角竖线分隔）；
- 目录块标志串 `## 可以放新标签的目录`：只在有可扩展目录时出现，行格式
  `- {dir_id}｜{全路径}｜{粒度}`，模型的 `dir` 字段按这个 id 回填。

图片按场景内联：场景文字块之后紧跟该场景的关键帧（每张前有一行图注），
让模型知道每张图属于哪个场景——这是"场景级视觉证据"能被模型引用的前提。
"""
from __future__ import annotations

import base64
from pathlib import Path

from tagplatform.schemas import ContentJSON

# 五个生成维度（键与 MovieTagGen 的 dimensions 对齐，值为中文展示名）
DIMENSIONS: dict[str, str] = {
    "style": "风格",
    "mood": "情绪",
    "theme": "主题",
    "narrative": "叙事",
    "character": "角色",
}

_DIMENSION_HINTS = {
    "风格": "视觉风格、镜头语言、画面质感（如 黑色电影、长镜头、冷色调）",
    "情绪": "观影情绪与整体基调（如 压抑、荒诞、温情、肃杀）",
    "主题": "叙事主题与母题（如 家族伦理、复仇、成长、阶级）",
    "叙事": "叙事结构或手法（如 多线叙事、倒叙、闭环结构）",
    "角色": "人物类型或人物关系特征（如 反英雄、群像、父子关系）",
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


def render_content(content: ContentJSON,
                   modalities=None) -> str:
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
    lines: list[str] = [f"电影：《{content.asset.title}》",
                        f"摘要：{content.content.summary[:_MAX_SUMMARY]}"]
    cast = [f"{ch.name}（{ch.description}）" if ch.description else ch.name
            for ch in content.content.characters[:_MAX_CAST]]
    if cast:
        lines.append("人物：" + "、".join(cast))
    for scene in content.content.scenes:
        desc = scene.description[:_MAX_SCENE_DESC]
        lines.append(
            f"- {scene.scene_id}"
            f" [{scene.time_range.start_ms}-{scene.time_range.end_ms}] {desc}")
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
        if "dialogue" not in active:
            continue
        for dialogue in scene.dialogues:
            tone = (f"（{dialogue.emotion.text_emotion}）"
                    if dialogue.emotion and dialogue.emotion.text_emotion else "")
            lines.append(f"  台词：{dialogue.speaker}{tone}：{dialogue.text}")
    return "\n".join(lines)


def _generation_instructions(with_dir: bool = False) -> str:
    dims = "\n".join(f"- {name}：{_DIMENSION_HINTS[name]}" for name in DIMENSIONS.values())
    dir_rule = (
        "- dir：从「可以放新标签的目录」中选择语义和适用粒度匹配的一项，填写其目录 id；"
        "只输出有合适目录的标签。\n" if with_dir else "")
    dir_field = '"dir":"目录 id",' if with_dir else ""
    return (
        "## 要求\n"
        "结合给出的影片材料（含关键帧），从以下五个维度自由提炼具体、可辨识的中文标签。"
        "每个概念选一个最贴切的说法，优先选择能体现本片特色、帮助观众形成观影预期的特征。\n"
        f"{dims}\n"
        "\n## 先找证据，再定层级\n"
        "逐场识别材料呈现的特征，再按证据覆盖的范围归类。\n"
        "- 全片级（scope=film）：概括贯穿或塑造全片的特征，如「阶级与偏见」「家族群像」。"
        "选取至少两个不同场景，说明它们如何共同体现这一特征。\n"
        "- 场景级（scope=scene）：提炼个别场景中鲜明的表现，如「激烈对峙」。"
        "只有单场证据时按场景级处理，每场选一条最有代表性的标签，合计最多 5 条。\n"
        "- 数量：证据充足时，目标为 15-20 条，其中全片级至少 10 条。证据不足时，"
        "按实际可支撑的数量输出。\n"
        "\n## 如何填写\n"
        "- tag：用 2-8 字概括特征；具体情节和视听细节留给 reason。\n"
        "- desc：用 10-32 字写成可跨影片复用的词典式释义，说明概念是什么、具备什么特征。"
        "例如「幽闭压迫」指「由封闭狭窄的空间引发的压抑与窒息感」。\n"
        "- scenes：填写实际提供、且在 reason 中用作依据的场景 id。\n"
        "- reason：用一到两句完整的话，先指出对应场景里可核对的细节（如镜头、台词、"
        "人物行为），再说明这些细节为什么支持该标签。全片级说明跨场景的共性或联系；"
        "场景级说明该场景如何具体体现这一特征。\n"
        "  例如，若两个场景都有相应画面，「幽闭压迫」的理由可写："
        "「一处用近景把人物挤在狭窄走廊中，另一处用门框遮住大半画面。"
        "两处构图都压缩了可见空间，营造出逼仄的压迫感。」\n"
        f"{dir_rule}\n## 输出格式\n"
        "直接返回一个 JSON 对象，结构如下：\n"
        '{"tags":[{"tag":"2-8 字中文标签",' + dir_field +
        '"scope":"film|scene",'
        '"dimension":"风格|情绪|主题|叙事|角色",'
        '"desc":"10-32 字通用释义",'
        '"scenes":["实际场景 id"],"reason":"可核对的细节及其支撑标签的理由"}]}'
    )


def _dir_hint_block(dirs: list[dict]) -> str:
    """可新增标签的目录清单（生成阶段就把归属交给模型决定）。

    只列 `dir_dynamic` 的目录：静态目录不允许扩充，出现在提示词里只会诱使
    模型往里面塞新词，然后在校验环节被打回。
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


def build_generation_messages(content: ContentJSON,
                              frames_by_scene: dict[str, tuple[Path, ...]],
                              run_index: int = 0,
                              library_hint: list[str] | None = None,
                              dir_hint: list[dict] | None = None,
                              modalities=None) -> list[dict]:
    """构造一轮生成请求：内容 + 场景内联关键帧 +（可选）库内标签/目录 + 要求。

    `modalities` 是消融实验的唯一旋钮（见 `eval/ablation.py`）：
    - 关掉 `video` → 即使调用方传了 `frames_by_scene` 也不内联任何关键帧
      （不能指望调用方自己传空表，否则“传了但忘了清”会默默地变成 full）；
    - 关掉 `script` / `dialogue` → 交给 `render_content` 裁剪。

    关掉 script 时**不报错、不提示模型缺内容**：模型只知道“就给这些”，
    这正是消融要测的“只有台词/只有画面够不够”。
    """
    active = _active(modalities)
    body = render_content(content, active)
    frame_parts: list[dict] = []
    if "video" not in active:
        frames_by_scene = {}
    for scene in content.content.scenes:
        frames = frames_by_scene.get(scene.scene_id) or ()
        for order, frame in enumerate(frames, 1):
            frame_parts.extend(keyframe_parts(scene.scene_id, order, len(frames),
                                             encode_image(frame)))
    return _generation_messages(body, frame_parts, library_hint, dir_hint)


def keyframe_parts(scene_id: str, order: int | str, total: int | str,
                   image_url: str) -> list[dict]:
    return [
        {"type": "text", "text": f"〔关键帧〕{scene_id} 第 {order}/{total} 张"},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]


def _generation_messages(body: str, frame_parts: list[dict],
                         library_hint: list[str] | None,
                         dir_hint: list[dict] | None) -> list[dict]:
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
                  "text": _generation_instructions(with_dir=bool(dir_hint))})
    return [{"role": "user", "content": parts}]


def build_generation_template() -> dict:
    body = "{{影片标题、摘要、人物、场景描述与台词等按模态渲染的文本}}"
    frames = keyframe_parts("{{场景 ID}}", "{{帧序号}}", "{{该场景帧数}}",
                          "{{关键帧的完整 data URL}}")
    hints = ["{{召回的库内标签名，每个词一行}}"]
    dirs = [{"dir_id": "{{目录 ID}}", "path": "{{目录全路径}}",
             "scope": "{{目录适用粒度}}"}]
    return {
        "messages": _generation_messages(body, frames, hints, dirs),
        "instructions_without_directory_hint": _generation_instructions(),
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


def _render_judge_content(content: ContentJSON) -> str:
    """judge 看到的内容文本：与生成同形，但**不含台词**。

    A/B 实测（同一批候选连判两次，只差台词）：角斗士 31 条、傲慢与偏见 32 条，
    判定结果 0 处变化，而 judge prompt 分别少 24% / 36%。原因是资产里的场景
    描述本来就是拿字幕一起生成出来的，台词对「这个标签有没有依据」不再提供
    新证据；它只在生成阶段值钱（直接语料、语气、只可能出现在台词里的概念）。
    """
    text = render_content(content)
    return "\n".join(line for line in text.splitlines()
                     if not line.startswith("  台词："))


def build_judge_messages(content: ContentJSON, candidates: list[dict]) -> list[dict]:
    """构造 judge 请求：候选清单 + 内容证据，要求逐条判定。

    candidates 每项：{tag, dimension, reason, scenes}
    """
    lines = [candidate_line(c["tag"], c.get("dimension"),
                            " ".join((c.get("reason") or "").splitlines()))
             for c in candidates]
    return render_judge_messages(_render_judge_content(content), "\n".join(lines))


def render_judge_messages(body: str, candidate_lines: str) -> list[dict]:
    text = (
        f"{JUDGE_MARKER}\n"
        "你是严格的影视内容标签审核员。下面是候选标签与影片内容，"
        "请逐个判定候选标签是否被内容真实支持。\n\n"
        "## 候选标签\n" + candidate_lines + "\n\n"
        "## 影片内容\n" + body + "\n\n"
        "## 判定标准\n"
        "- supported：内容中有明确依据（场景描述、氛围、人物情绪至少一处）；\n"
        "- insufficient：内容不足以判断（不要臆测）；\n"
        "- contradicted：内容明显与标签矛盾；\n"
        "- importance：core（贯穿全片的主要特征）/ secondary（局部但清晰）/ "
        "incidental（一笔带过）；\n"
        "- confidence：0-1，你对判定结果的把握；\n"
        "- scenes：引用的场景 id，必须是上面出现过的 id；没有就空数组；\n"
        "- quote：从上面内容里**逐字摘录**的一句原文（10-25 字，可以是场景描述、"
        "氛围或情绪词），用来让人核对你的依据。**不得改写、不得复述候选标签的"
        "理由**；找不到能摘的原文就判 insufficient。\n"
        "- reason：一句话说明这句原文怎么支撑（或不支撑）该标签，不要照抄候选理由。\n"
        "另外注意作用域：若候选是面向全片的概括（措辞含“全片/整体/贯穿/始终”或"
        "明显在描述全片共性），而你只能找到**单个场景**的依据，判 insufficient；"
        "单场景细节若措辞本身就是局部的（如“开场旁白”），不受此限。\n"
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
