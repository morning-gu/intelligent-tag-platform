"""开放评测整片盲评提示词（与 Judge 共用统一三维评分标准与判决输出协议）。

三维评价标准正文与融合输出协议（VERDICT_OUTPUT_SPEC 字段要求 + 输出示例、
VERDICT_RETRY_HINT 重试提示）由 `capabilities/prompts/_evaluation.py` 统一维护，
Judge（`mtg.py`）与本模块共用同一份标准与输出协议，消除两环节评价倾向与输出形状分歧：
模型只输出评分与证据，是否保留由程序按分数复算（本模块不再要求 pass/总评）。
改 rubric 或输出协议措辞应 bump REVIEW_PROMPT_VERSION。

分层红线：本模块纯常量，不得 import fastapi / sqlalchemy /
tagplatform.services。
"""

from tagplatform.capabilities.prompts._evaluation import (
    UNIFIED_EVALUATION_CRITERIA as _UNIFIED_EVALUATION_CRITERIA,
    VERDICT_OUTPUT_SPEC as _VERDICT_OUTPUT_SPEC,
    VERDICT_RETRY_HINT as _VERDICT_RETRY_HINT,
)

REVIEW_PROMPT_VERSION = "open-review-v14"

_REVIEW_HEADER = """## 标签质量评估
你是影视标签评估员。只对「待评估标签」列表中的标签逐一评分，原样返回每个 id。
输入字段说明：
- level 表示取证层级：film 评全片，scene 评 scene_ids 指定的来源场景；
- target_type 表示标签种类：content（内容级）、scene（场景级）、character（人物级）；
- dir_path 与 dir_description 表示该标签所属的目标目录路径与定义（供语义准确性评估目录匹配度）。

"""

# 协议附注（v14 瘦身）：两步评分顺序、组内比较与 pass/总评段退役——只保留
# 「只对待评估标签输出、原样返回 id」与由共享块承载的字段要求；节标题
# 「## 开放评测补充规则」是 mock 分流标志串，不得改名。
_REVIEW_PROTOCOL_NOTE = """

## 开放评测补充规则
只对「待评估标签」列表中的标签输出判决，原样返回每个 id，每个 id 恰好一条；是否保留由程序按各维分数复算，无需输出保留结论。输出字段要求与示例如下。

""" + _VERDICT_OUTPUT_SPEC

REVIEW_PROMPT = (
    _REVIEW_HEADER
    + _UNIFIED_EVALUATION_CRITERIA
    + _REVIEW_PROTOCOL_NOTE
)

REVIEW_RETRY_HINT = _VERDICT_RETRY_HINT
