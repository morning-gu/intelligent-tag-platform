"""开放评测整片盲评提示词（与 Judge 共用统一三维评分标准）。

三维评价标准正文由 `capabilities/prompts/_evaluation.py` 统一维护，
Judge（`mtg.py`）与本模块共用同一份标准，消除两环节评价倾向分歧。
改 rubric 措辞应 bump REVIEW_PROMPT_VERSION。

分层红线：本模块纯常量，不得 import fastapi / sqlalchemy /
tagplatform.services。
"""

from tagplatform.capabilities.prompts._evaluation import (
    UNIFIED_EVALUATION_CRITERIA as _UNIFIED_EVALUATION_CRITERIA,
)

REVIEW_PROMPT_VERSION = "open-review-v13"

_REVIEW_HEADER = """## 标签质量评估
你是影视标签评估员。只对「待评估标签」列表中的标签逐一评分，原样返回每个 id。
输入字段说明：
- level 表示取证层级：film 评全片，scene 评 scene_ids 指定的来源场景；
- target_type 表示标签种类：content（内容级）、scene（场景级）、character（人物级）；
- dir_path 与 dir_description 表示该标签所属的目标目录路径与定义（供语义准确性评估目录匹配度）。

"""

_REVIEW_PROTOCOL_NOTE = """

## 开放评测补充规则
先逐条完成单标签评分。相关性理由写出能定位到实际场景的具体细节，语义理由指出名称、通用释义或目录定义的具体依据，价值理由逐方向写出词名本身给用户的信息。
随后结合整片标签给出保留结论。若提供「该片其他标签（上下文）」，在这一步一起比较。同一用途存在实质重叠时，说明与哪条重叠、另一条为何更准确或更具体；不同场景的片段定位用途可各自独立成立。
同组比较只写在顶层保留理由中，沿用第一步的各维分数。

输出字段要求：
- relevance 与 semantics：返回 {"score": 0–4 整数或 null, "reason": "简短理由"}；
  缺少释义且未观察到明显名称或目录缺陷时，semantics 填 null 并说明原因。
- value：返回三个方向的独立评分对象，形如
  {"presentation": {"score": 0–4 整数, "reason": "理由"},
   "search": {"score": 0–4 整数, "reason": "理由"},
   "recommendation": {"score": 0–4 整数, "reason": "理由"}}。
- evidence_scene_ids：每条均须返回，无引用时为空列表；相关性达到 3 或 4 分时填写实际支撑结论的完整场景 ID（例如 scene_001），film 至少两处独立场景，scene 取自该标签的 scene_ids。
- pass 与 reason：结合标签对影片的准确反映程度、与所属目录及释义的契合度、呈现/检索/推荐价值以及相对同组标签的独立信息贡献，独立判断该标签是否值得作为最终标签保留。pass 为 true 表示保留，false 表示不保留，材料不足以进行整体判断时填 null；顶层 reason 简明概括最终保留或不保留的核心依据。每个目标 id 返回一次。
"""

_REVIEW_OUTPUT_FORMAT = (
    '只输出 JSON 对象，例如 {"reviews":[{"id":"t0",'
    '"pass":true,"reason":"直接概括影片的核心冲突，且提供了同组其他标签没有的区分信息，建议保留",'
    '"relevance":{"score":4,"reason":"理由"},'
    '"semantics":{"score":3,"reason":"理由"},'
    '"value":{"presentation":{"score":4,"reason":"理由"},'
    '"search":{"score":3,"reason":"理由"},'
    '"recommendation":{"score":2,"reason":"理由"}},'
    '"evidence_scene_ids":["场景ID"]}]}。'
)

REVIEW_PROMPT = (
    _REVIEW_HEADER
    + _UNIFIED_EVALUATION_CRITERIA
    + _REVIEW_PROTOCOL_NOTE
    + _REVIEW_OUTPUT_FORMAT
)

REVIEW_RETRY_HINT = (
    "\n\n上一响应缺少可用评审结果。请在 reviews 中按系统协议返回每个目标 id、"
    "relevance/semantics 评分及理由、value 下 presentation/search/recommendation 三个方向的评分及理由、"
    "独立保留结论 pass、总评 reason 和引用场景。"
)
