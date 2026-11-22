"""开放流水线（mtg-v1）指令文案常量：唯一权威源（git 即版本机制）。

2026-11-22 文案集中（prompt-copy-centralization Task 1）：全部逐字搬自
services 层五模块（prompts.py / descriptions.py / verify.py / judge.py 的
指令段与重试提示）。搬运为纯位移——改文案 = 改常量 + 全量回归 + commit。

协议耦合提醒：本模块文案与其解析端互为协议——JUDGE_CRITERIA / *_RETRY_HINT
描述的输出 JSON 形状就是 parse.parse_judge_items / parse_verify_results /
_parse_items / parse_closed_output 的输入契约，DESCRIPTION_INSTRUCTIONS 的
"通用性"规则就是 descriptions.is_general 的校验口径。改字段名、取值集或
输出 JSON 形状时两侧必须同改，改动只落提示词一侧 = 靠解析容错兜住的
静默漂移。

分层红线：本模块纯常量，不得 import fastapi / sqlalchemy /
tagplatform.services。含参数槽的行（输出格式 JSON 行、builder 数据头）
留在服务层组装函数内——本模块只收无参数槽的完整文案块。
"""

# 生成请求的角色行（## 标签生成任务 marker 之后、## 内容 之前的自报家门段）
GENERATION_ROLE_TEXT = (
    "你是影视内容分析专家，任务是从影片内容中提炼语义标签。\n\n"
    "## 内容\n"
)

# 第一轮（场景粒度）的目标段：scope=scene 时由 _generation_instructions 注入
SCENE_GOAL_TEXT = (
    "第一轮：为场景搜索提炼标签，scope 固定为 scene。"
    "想象用户记得这个片段的一个细节，或者想找同类片段时，会搜索什么。"
    "选择本场明确出现且有区分度的细节，用自然、可直接搜索的词组命名，"
    "比如「无声对峙」「拆除定时炸弹」。局部细节本身就可以有检索价值。"
    "以本场的描述、台词或画面为证据。scenes 只填写这一场的 ID。"
    "如有目录清单，选择能归入单场景目录的标签。"
)

# 第二轮（整片粒度）的目标段：scope=film 时由 _generation_instructions 注入
FILM_GOAL_TEXT = (
    "第二轮：结合影片材料和逐场候选，为观众提炼可独立展示的全片标签，scope 固定为 film。"
    "从反复出现的意象或情绪、人物变化、前后呼应和段落对照中找全片特征。"
    "逐场候选是检索线索，证据来自影片材料。允许发现候选里没有的全片特征。"
    "避免照抄局部标签、机械拼接场景标签或仅凭重复次数决定主题。"
    "每条标签至少引用两个不同场景，在 reason 中说清它们的共性或联系，"
    "以及这些细节为何足以概括全片。"
)

# 字段填写说明块（两轮共用；介于"最多 N 条"行与 dir 规则行之间）
HOW_TO_FILL_TEXT = (
    "\n## 如何填写\n"
    "- tag：用简洁自然的中文词组命名，通常 2-8 字。\n"
    "- desc：用 10-32 字解释标签本身，写成可跨影片复用的词典式释义。\n"
    "- scenes：填写实际提供、且用于支撑标签的场景 ID。\n"
    "- reason：用一到两句完整的话指出可核对的细节，并说明这些细节如何支持标签。\n"
)

# dir 字段规则行（有可扩展目录清单时插入"如何填写"与"输出格式"之间）
DIR_FIELD_RULE_TEXT = (
    "- dir：从「可以放新标签的目录」中选择语义和适用粒度匹配的一项，填写其目录 id；"
    "只输出有合适目录的标签。\n"
)

# 目录清单块的头部说明（listing 行由 _dir_hint_block 按目录渲染）
DIR_HINT_HEAD_TEXT = (
    "## 可以放新标签的目录\n"
    "每个标签都要落到下面某个目录里，用**目录 id** 填 `dir` 字段。"
    "只能从这张表里选，不要自造 id（竖线后是目录全路径与挂载粒度）：\n"
)

# 目录清单块的尾部说明（紧接 listing 行之后）
DIR_HINT_TAIL_TEXT = (
    "按标签本身的含义挑目录，不要看哪个目录空着就往哪塞；"
    "一条标签实在没有合适的目录，就**不要输出它**——宁可少一条，"
    "也不要放进不合适的目录。"
)

# 库内已有标签提示块的头部说明（词表行由 _library_hint_block 渲染）
LIBRARY_HINT_HEAD_TEXT = (
    "## 库里已有的标签\n"
    "以下是已收录的表述，供你对照。请输出尚未收录的表述，优先补充清单未覆盖的特征，"
    "或用片中证据提炼出比现有词更精确的标签：\n"
)

# ---- judge 指令文案（与解析器互为协议）----
# judge 输出的解析端是 parse.parse_judge_items（verdict/importance 取值集、
# confidence 夹取、quote/reason 截断都在那里），JUDGE_RETRY_HINT 是同一输出
# 协议的第三次出现——改字段名、取值集或输出 JSON 形状时三处必须同改。
JUDGE_ROLE_BRIEF = (
    "你是严格的影视内容标签审核员。下面是候选标签与影片内容，"
    "请逐个判定候选标签是否被内容真实支持。\n\n"
)
JUDGE_CRITERIA = (
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

# ---- 标签通用释义补写指令（descriptions）----
# 解析端是 descriptions._parse_items / clean_description / is_general：条目
# 形状 {"tag","desc"}、长度带（DESC_MIN/DESC_MAX）与"通用性"规则两侧同改。
DESCRIPTION_INSTRUCTIONS = (
    "## 通用释义（标签描述补写）\n"
    "你在维护一个影视标签库。下面是待新增的标签（名称 + 归属目录），"
    "请为每个标签写一句**通用释义**。\n"
    "这句释义以后会脱离任何影片单独使用（标签库检索、审核界面展示），"
    "必须只解释**这个词本身**指什么、具备什么特征。\n"
    "硬性要求：\n"
    "1. 长度 10-32 字，一句话；\n"
    "2. 像词典条目，不叙述任何影片的剧情；\n"
    "3. 禁止出现片名、人名、地名、具体情节、具体镜头或时间点，"
    "禁止用“本片/这部片/影片中/主角/结尾”等指代；\n"
    "4. 句式可参考现有标签库：\n"
    "   - 宏大史诗：整体氛围宏大壮阔，具有史诗般的叙事格局\n"
    "   - 黑色电影：以愤世嫉俗、道德模糊、阴暗氛围为特征的犯罪题材类型\n"
    "   - 怀旧：以追忆过往和怀旧情感为主要观看需求\n"
    "5. 只输出 JSON：{\"items\":[{\"tag\":\"标签名\",\"desc\":\"通用释义\"}]}，"
    "不要解释、不要 Markdown 代码块。\n\n"
    "标签清单：\n"
)
DESCRIPTION_RETRY_HINT = (
    "\n\n上一版不合格：出现了具体影片信息、专名或长度越界。"
    "请严格照要求重写，只输出 JSON。"
)

# ---- 相似度复核指令（verify）----
# 解析端是 verify.parse_verify_results：候选行格式与输出 JSON 形状
# {"results":[{"candidate","same"}]} 两侧同改。
VERIFY_INSTRUCTIONS = (
    "下面每行是一组「候选标签｜已有标签｜相似度」。\n"
    "判断标准只有一条：如果这两个词都进了标签库，审核的人会不会认为它们是"
    "重复条目、只该留一条？会 → same=true，否则 false。\n"
    "答 true 的典型情况（同一含义的详略/近义说法，合并后信息不丢，"
    "原措辞会作为别名保留）：\n"
    "- 压抑沉重 / 压抑\n"
    "- 古典史诗 / 宏大史诗\n"
    "- 悬疑神秘 / 悬疑\n"
    "答 false 的三种情况：\n"
    "1. 意思相反（非线性叙事 / 线性叙事）；\n"
    "2. 字面像但指不同事物（友情与背叛 / 权力与背叛；超能力觉醒 / 绝境觉醒）；\n"
    "3. 候选是另一个独立概念，只是共享了词（暴君多疑 / 多疑 视情况，"
    "共享词但主体不同的判 false）。\n"
)
VERIFY_OUTPUT_FORMAT = (
    '只输出一个 JSON 对象：{"results":[{"candidate":"候选原文","same":true}]}，'
    "每行都要给一条，不要解释、不要 Markdown 代码块。"
)
VERIFY_RETRY_HINT = (
    "\n\n上一次输出无法解析：只输出一个 JSON 对象，形如 "
    '{"results":[{"candidate":"候选原文","same":true}]}，'
    "不要解释、不要 Markdown 代码块。"
)

# ---- judge 解析重试提示 ----
JUDGE_RETRY_HINT = (
    "\n\n上一次输出无法解析：顶层必须是 JSON 数组（以 [ 开头、以 ] 结尾），"
    '每个元素形如 {"tag":"标签","verdict":"supported","importance":"core",'
    '"confidence":0.9,"scenes":["scene_id"],"reason":"依据"}。'
    "不要用对象包装、不要任何解释文字或 Markdown 代码块。"
)
