"""开放评测整片盲评提示词。"""
from tagplatform.capabilities.prompts import quality as quality_prompts

REVIEW_PROMPT_VERSION = "open-review-v6"
REVIEW_PROTOCOL_NOTE = """
## 开放评测补充规则
同时阅读整片产出的标签。只给「待评估标签」中的条目打分，保持各自的 id。
若还有「该片其他标签（上下文）」，将它和待评列表一起用于判断跨标签重复。
结合名称、释义、用途层级和来源场景判断是否重复。同一组标签如果传达相同信息且
用途相同，保留最具体、最准确的一条，质量相同时保留 id 序号最小的一条。
其余条目的 value 至多 1 分，并指出与哪条重复。不同来源场景的片段定位用途可以独立成立。
相关性达到 3 或 4 时，返回实际支撑结论的 evidence_scene_ids。film 至少两处独立场景，
scene 必须来自该标签的 scene_ids。缺少释义时 semantics 填 null 并说明原因。
下列输出格式覆盖前文示例。每个目标 id 恰好返回一次。
"""
REVIEW_OUTPUT_FORMAT = (
    '只输出 JSON 对象，例如 {"reviews":[{"id":"t0",'
    '"relevance":{"score":4,"reason":"理由"},'
    '"semantics":{"score":3,"reason":"理由"},'
    '"value":{"score":2,"reason":"理由"},'
    '"evidence_scene_ids":["场景ID"]}]}。'
)
REVIEW_PROMPT = quality_prompts.RUBRIC_TEXT + REVIEW_PROTOCOL_NOTE + REVIEW_OUTPUT_FORMAT
REVIEW_RETRY_HINT = "\n\n上一响应缺少可用评分。请按系统输出协议在 reviews 中返回全部目标 id 及三维分。"
