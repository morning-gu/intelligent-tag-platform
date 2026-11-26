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

REVIEW_PROMPT_VERSION = "open-review-v11"

_REVIEW_HEADER = """## 标签质量评估
你是影视标签评估员。只对「待评估标签」列表中的标签逐一评分，原样返回每个 id。
输入字段说明：
- level 表示取证层级：film 评全片，scene 评 scene_ids 指定的来源场景；
- target_type 表示标签种类：content（内容级）、scene（场景级）、character（人物级）；
- dir_path 与 dir_description 表示该标签所属的目标目录路径与定义（供语义准确性评估目录匹配度）。

"""

_REVIEW_PROTOCOL_NOTE = """

## 开放评测补充规则
同时阅读整片产出的标签。只评「待评估标签」中的条目，保留各自的 id。
若还有「该片其他标签（上下文）」，将它和待评列表一起用于比较标签用途和信息重复。
结合名称、释义、目标目录、用途层级和来源场景比较同组标签。同一用途、信息实质重复时，考虑
哪一条表达更准确具体，说明其余标签是否仍有独立保留价值。不同场景的片段定位用途可以独立成立。

分别返回三维评分与理由：
- relevance 与 semantics 返回 {"score": 0–4 整数或 null, "reason": "简短理由"}；
  缺少释义且未观察到明显名称或目录缺陷时 semantics 填 null 并说明原因。
- value 返回三个方向的独立评分对象：
  {"presentation": {"score": 0–4 或 null, "reason": "理由"},
   "search": {"score": 0–4 或 null, "reason": "理由"},
   "recommendation": {"score": 0–4 或 null, "reason": "理由"}}，
  不需要模型自行加权汇总，系统将按 target_type（内容级/场景级/人物级）对应权重自动聚合总分。
- 相关性达到 3 或 4 时，返回实际支撑结论的 evidence_scene_ids。每条都必须包含该字段，
  无引用时返回空列表。film 至少两处独立场景，scene 必须来自该标签的 scene_ids。
  引用时写完整 ID，例如 scene_001。

另外独立判断这个标签是否值得作为该片的最终标签保留。综合它是否准确反映影片、
是否契合所属目录与释义、有实际呈现/检索/推荐价值、相对同组标签提供了什么信息。
三维分仅供参考，禁止按分数阈值或总分推导保留结论。pass 为 true 表示保留，false 表示不保留，
材料不足以进行整体判断时填 null。顶层 reason 写清模型对标签的整体评价和决定保留、不保留
或暂无法判断的关键原因。避免复述三条评分理由或列出分数。每个目标 id 返回一次。
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
