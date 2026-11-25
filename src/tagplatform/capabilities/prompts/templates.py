"""Prompt 模板文案常量：本地化的唯一权威源（git 即版本机制）。

初始值 = models.py（四层重构 Task 3 时点）`_PROMPT_V1_CLOSED_TEXT`/
`_PROMPT_V1_OPEN_TEXT` 的逐字复制——0016 迁移播种的就是该常量，线上库
closed/open v1 激活行文案与此逐字一致（test_prompts_local 以迁移文件的
冻结字面量为基准做逐字比对）。改文案 = 改常量 + 全量回归 + commit，
git 历史即版本史。

模板文本不得出现字面花括号——render_prompt 是 str.format，字面花括号
（JSON 示例等）请双写 {{ }} 转义（test_prompts_local 有无残留花括号断言）。
分层红线：本模块不得 import fastapi / sqlalchemy（无 DB 依赖是本地化的
全部意义）。
"""

# closed v1：候选标签不再经 {candidates} 占位符注入（ClosedTagProducer 按
# 目录分组、以 JSON 追加在渲染后的正文末尾，组内含目录名/描述与标签
# id/名称/描述）。模板只保留 {content} 占位符。
# v8（2026-12 多模态融合）：evidence 改为 content/scene/frame 三模态嵌套
# 结构，文案用缩进 bullet 描述。
CLOSED_TEMPLATE_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签按目录分组，以 JSON 格式附在上述正文之后。每组包含所属目录的",
    "名称与描述，组内标签给出 tag_id、tag_name 与 description。",
    "",
    "任务：从候选标签中选出适用于内容的标签。Applicability 和各模态的",
    "Evidence Strength 是独立维度，必须分别评分，不能互相替代。",
    "每个选中标签输出以下字段：",
    "- tag_id（必须与候选标签一致，不得编造）",
    "- applicability：整数 1 到 5。根据标签定义、使用条件和影视内容，判断标签是否适用，",
    "  以及是否构成作品的重要特征。1=完全不适用；2=存在弱关联但不满足主要适用条件；",
    "  3=部分符合标签定义但非主要特征；4=高度符合标签定义且具有明显内容特征；",
    "  5=完全符合标签定义且是作品核心特征。",
    "- evidence：对象，必须同时包含 content、scene、frame 三个模态节，",
    "  分别基于三种输入独立评分与举证：",
    "  - content 模态：基于剧情整体描述（摘要）判断；",
    "  - scene 模态：基于各场景的场景描述判断；",
    "  - frame 模态：基于随消息附上的关键帧图片判断（若无图片则基于正文推断）。",
    "  每个模态节包含两个字段：",
    "  - evidence_strength：整数 1 到 5，该模态下证据的直接性、明确程度和相互支撑程度。",
    "    1=缺乏有效证据，主要依赖推测；2=证据间接、模糊或可信度较低；",
    "    3=存在一定证据但覆盖不足；4=存在多个明确证据支持判断；",
    "    5=证据直接、充分且能够相互印证。",
    "  - evidences：该模态的证据摘要数组，每条包含 scene_id 和 evidence；",
    "    scene_id 必须来自正文已有场景，evidence 用简洁文字总结该场景中",
    "    该模态下的实际证据，不得编造或仅凭推测。",
    "",
    "只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。",
])

# open v1：open 模式自 2026-11 起改由内置流水线 mtg-v1 接管（builds_prompt
# 生产器自拼 prompt），本常量为该 mode 的模板口径存续（get_prompt("open")
# 契约完整），不再被生成任务渲染消费。
OPEN_TEMPLATE_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "任务：为该内容自由生成最贴切的语义标签（不受任何既有标签体系限制）。对每个标签输出：",
    "- tag_name：简洁的标签名（2-8 字）",
    "- tag_type：标签分类（如 genre/mood/theme/narrative/style/character 或自拟）",
    "- description：一句话语义定义",
    "- confidence：你对该标签的置信度，0 到 1",
    "- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description",
    "",
    "只输出 JSON 数组，不要输出其他文字。输出 5 到 15 个标签。",
])

# closed 模式解析重试的格式纠正提示（2026-11-22 文案集中：逐字搬自
# closed/producer.py 的 _RETRY_HINT，重调本组一次）。解析端是
# closed/parse.parse_closed_output——重试描述的输出形状与解析契约
# 两侧必须同改。
CLOSED_RETRY_HINT = (
    "\n\n上一次输出无法解析：顶层必须是 JSON 数组（以 [ 开头、以 ] 结尾），"
    '每个元素必须同时包含 "tag_id"（字符串）、"applicability"（1–5 整数）'
    '与 "evidence"（对象）。evidence 内必须含 "content"、"scene"、"frame"'
    ' 三个模态节，每节含 "evidence_strength"（1–5 整数）与 "evidences"'
    '（数组，元素含 "scene_id" 与 "evidence" 字符串）。不要用对象包装、'
    "不要任何解释文字或 Markdown 代码块。"
)

# 证据核验（closed 置信度 Cver，2026-12）：对最终输出的每个标签，请模型
# 按五级可核验表给 1–5 分。组装方（closed/confidence.py）以
# `spec.prompt_text + "\n\n" + 本常量` 拼接（不走 render_prompt——本常量
# 无占位符，核验材料（标签名 + 证据条目）由组装方注入正文）。输出契约
# 小 JSON：{"score": 1–5 整数}。五级文案与用户口径逐字一致。
CLOSED_VERIFICATION_PROMPT = "\n".join([
    "## 证据核验",
    "",
    "下面给出一个标签及其证据条目。请对照上文的原始内容，判断该标签的",
    "证据是否真实、能否在原始内容中找到对应依据，按下表打分：",
    "",
    "| 分值 | 评价等级 | 评价标准 |",
    "|----|--------|-------|",
    "| 1 | 无法核验 | 找不到原始内容依据，主要来自模型推测 |",
    "| 2 | 弱可核验 | 仅能找到模糊或间接相关内容，无法明确定位 |",
    "| 3 | 部分可核验 | 能找到相关剧情、对白或画面，但定位不准确或仅部分支持 |",
    "| 4 | 高度可核验 | 能定位到明确的剧情片段、对白或关键帧，原始内容基本支持证据描述 |",
    "| 5 | 完全可核验 | 能准确定位到原始内容中的相关片段、对白、时间戳或关键帧，且证据描述与原始内容一致 |",
    "",
    "只输出 JSON 对象：{{\"score\": 1 到 5 的整数}}，不要输出其他文字。",
])
