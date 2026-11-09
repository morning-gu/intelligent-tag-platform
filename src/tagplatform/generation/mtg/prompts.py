"""开放式生成的提示词：内容渲染、多轮生成请求、judge 审核请求。

移植自 MovieTagGen 的 04 生成 / 06 审核两段思路，但输入换成了任务一产出的
资产 JSON（中文场景描述 + 氛围 + 人物情绪 + 台词）加按场景对齐的关键帧。

两个契约必须与 `llm/mock.py` 咬合（测试与 mock 路径靠它识别分支）：
- 生成 prompt 含标志串 `## 标签生成任务`；
- 审核 prompt 含标志串 `## 审核任务`；
- 场景行统一为 `- {scene_id} [{start_ms}-{end_ms}] {description}`（与
  `tasks/runner._render_content_text` 同形，Mock 的 `_SCENE_LINE_RE` 直接可解析）；
- 审核候选行统一为 `- {tag}｜{dimension}｜{reason}`（全角竖线分隔）。

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

GENERATION_MARKER = "## 标签生成任务"
JUDGE_MARKER = "## 审核任务"

# prompt 内明细上限（控制 token；资产本身已按场景裁剪过台词）
_MAX_CAST = 8
_MAX_SCENE_DESC = 240
_MAX_SUMMARY = 600
# 台词不设上限：资产侧已按 asset_dialogue_limit 控量（8 条/场景），这里全用——
# 台词是情绪与主题最直接的证据，按条数筛掉的往往正是关键那句。


def _encode_image(path: Path) -> str:
    """图片 → data URL（沿用资产生成链路的编码方式）。"""
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    return f"data:image/{suffix};base64,{base64.b64encode(path.read_bytes()).decode()}"


def render_content(content: ContentJSON) -> str:
    """资产 → 生成/审核共用的内容文本（场景行与 runner 渲染同形）。"""
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
        for dialogue in scene.dialogues:
            tone = (f"（{dialogue.emotion.text_emotion}）"
                    if dialogue.emotion and dialogue.emotion.text_emotion else "")
            lines.append(f"  台词：{dialogue.speaker}{tone}：{dialogue.text}")
    return "\n".join(lines)


def _generation_instructions() -> str:
    dims = "\n".join(f"- {name}：{_DIMENSION_HINTS[name]}" for name in DIMENSIONS.values())
    return (
        "## 要求\n"
        "围绕下面五个维度，为这部影片生成 15-20 个中文标签（不受任何既有词表限制，"
        "自由表达这部片子的特点）：\n"
        f"{dims}\n"
        "## 两个层级（先在内部按层级想清楚，再输出）\n"
        "先在心里把全片场景扫一遍（不要输出扫描过程），然后区分两种标签：\n"
        "- 全片级（scope=film）：全片共有的特征，必须能由**至少两个场景**共同支撑；\n"
        "  用 2-8 字的概括性短语（如“阶级与偏见”“家族群像”“道德堕落”），\n"
        "  不要写成剧情描述或某一场戏的画面；\n"
        "- 场景级（scope=scene）：只属于个别场景的特征，允许只有一个场景支撑，\n"
        "  但也要提炼成 2-8 字的特征短语，并带上全片基调的取向：\n"
        "  写“狼狈闯入”而不是“泥泞跋涉的狼狈闯入”，写“激烈对峙”而不是\n"
        "  “暴雨中的激烈冲突”；不要复述场景情节，也不要用“全片/整体/贯穿”的口径；\n"
        "  每个场景最多贡献一条场景级标签（同一场戏不要拆成“雨中交锋”“雨中表白”\n"
        "  两条），场景级标签合计最多 5 条。\n"
        "所有标签都不超过 8 个字。\n"
        "配额：全片级不少于 10 条，场景级最多 5 条。\n"
        "凑不出第二个场景支撑的特征，就老实标成 scene 级——不要为了凑全片级编造场景。\n"
        "硬性约束：\n"
        "1. 每个标签必须能从上面的内容（含关键帧）找到依据，不要凭片名或常识编造；\n"
        "2. 标签要具体、可判别，避免空泛（如“好看”“精彩”）；同一含义只出现一次；\n"
        "3. scenes 只能填上面出现过的场景 id；无法对应到具体场景时给空数组；\n"
        "4. 只输出一个 JSON 对象，形如 "
        '{"tags":[{"tag":"2-8 字中文标签","scope":"film|scene",'
        '"dimension":"风格|情绪|主题|叙事|角色",'
        '"scenes":["场景 id"],"reason":"20 字以内的内容依据"}]}；'
        "不要解释、不要 Markdown 代码块。"
    )


def _library_hint_block(names: list[str]) -> str:
    """库内已有标签的提示块（只给名字，不给定义）。

    措辞刻意留出"更精确说法"的口子：库里有"悬疑"时，模型仍可以给出
    "悬疑解谜"这类更细的候选，只是不该原样复述。
    """
    listing = "\n".join(f"- {name}" for name in names)
    return (
        "## 库里已有的标签\n"
        "下面这些标签我们的标签库里已经有了，**不要原样重复输出**；"
        "如果你认为其中某条对这部片不够准确、不够具体，可以给出更精确的说法：\n"
        f"{listing}"
    )


def build_generation_messages(content: ContentJSON,
                              frames_by_scene: dict[str, tuple[Path, ...]],
                              run_index: int = 0,
                              library_hint: list[str] | None = None) -> list[dict]:
    """构造一轮生成请求：内容 + 场景内联关键帧 +（可选）库内已有标签 + 要求。"""
    parts: list[dict] = [{
        "type": "text",
        "text": (f"{GENERATION_MARKER}\n"
                 "你是影视内容分析专家，任务是从影片内容中提炼语义标签。\n\n"
                 "## 内容\n" + render_content(content)),
    }]
    for scene in content.content.scenes:
        frames = frames_by_scene.get(scene.scene_id) or ()
        for order, frame in enumerate(frames, 1):
            parts.append({"type": "text",
                          "text": f"〔关键帧〕{scene.scene_id} 第 {order}/{len(frames)} 张"})
            parts.append({"type": "image_url",
                          "image_url": {"url": _encode_image(frame)}})
    if library_hint:
        parts.append({"type": "text", "text": _library_hint_block(library_hint)})
    parts.append({"type": "text", "text": _generation_instructions()})
    return [{"role": "user", "content": parts}]


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
    lines = [f"- {c['tag']}｜{c.get('dimension') or '未分类'}｜"
             f"{(c.get('reason') or '')[:40]}" for c in candidates]
    text = (
        f"{JUDGE_MARKER}\n"
        "你是严格的影视内容标签审核员。下面是候选标签与影片内容，"
        "请逐个判定候选标签是否被内容真实支持。\n\n"
        "## 候选标签\n" + "\n".join(lines) + "\n\n"
        "## 影片内容\n" + _render_judge_content(content) + "\n\n"
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
