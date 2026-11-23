"""开放评测整片盲评提示词。"""
from tagplatform.capabilities.prompts import quality as quality_prompts

REVIEW_PROMPT_VERSION = "open-review-v7"
REVIEW_PROTOCOL_NOTE = """
## 开放评测补充规则
同时阅读整片产出的标签。只评「待评估标签」中的条目，保留各自的 id。
若还有「该片其他标签（上下文）」，将它和待评列表一起用于比较标签用途和信息重复。
结合名称、释义、用途层级和来源场景比较同组标签。同一用途、信息实质重复时，考虑
哪一条表达更准确具体，说明其余标签是否仍有独立保留价值。不同场景的片段定位用途可以独立成立。

分别返回三维评分（0–4 或 null）和每个维度的理由。相关性达到 3 或 4 时，返回实际支撑结论的
evidence_scene_ids。film 至少两处独立场景，scene 必须来自该标签的 scene_ids。
缺少释义时 semantics 填 null 并说明原因。

另外独立判断这个标签是否值得作为该片的最终标签保留。综合它是否准确反映影片、
有实际展示或检索用途、相对同组标签提供了什么信息。三维分仅供参考，禁止按分数
阈值或总分推导保留结论。pass 为 true 表示保留，false 表示不保留，材料不足以
进行整体判断时填 null。顶层 reason 写清模型对标签的整体评价和决定保留、不保留
或暂无法判断的关键原因。避免复述三条评分理由或列出分数。每个目标 id 返回一次。
"""
REVIEW_OUTPUT_FORMAT = (
    '只输出 JSON 对象，例如 {"reviews":[{"id":"t0",'
    '"pass":true,"reason":"直接概括影片的核心冲突，且提供了同组其他标签没有的区分信息，建议保留",'
    '"relevance":{"score":4,"reason":"理由"},'
    '"semantics":{"score":3,"reason":"理由"},'
    '"value":{"score":3,"reason":"理由"},'
    '"evidence_scene_ids":["场景ID"]}]}。'
)
REVIEW_PROMPT = (quality_prompts._RUBRIC_CRITERIA
                 + REVIEW_PROTOCOL_NOTE + REVIEW_OUTPUT_FORMAT)
REVIEW_RETRY_HINT = (
    "\n\n上一响应缺少可用评审结果。请在 reviews 中按系统协议返回每个目标 id、"
    "三维分及理由、独立保留结论 pass、总评 reason 和引用场景。"
)
