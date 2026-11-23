"""开放评测三维独立评审的 prompt 常量：唯一权威源（git 即版本机制）。

2026-11-23 三维化（open-judge-quality）：评审不再输出二值 pass/fail，改为
复用 tag-quality-v4 的 canonical rubric（quality.py 的 RUBRIC_TEXT，逐字
import 不复制）逐维评 0-4/null；本模块只收评审任务的补充指令（judge 参考
定位、摘录要求、输出协议覆盖）。通过与否由解析端按分数阈值推导，模型只
给分——rubric 评分口径的单一来源仍是 RUBRIC_TEXT，改口径去 quality.py。

REVIEW_PROMPT_VERSION 与文案同驻（该版本号标识的就是这份评审 prompt）：
改文案须 bump 版本号，让历史评审记录可追溯到文案代。

分层红线：本模块纯常量，不得 import fastapi / sqlalchemy /
tagplatform.services。
"""
from tagplatform.capabilities.prompts import quality as quality_prompts

# 评审 prompt 版本标识（写进 meta 供 run 级溯源；运行器记账直用）
REVIEW_PROMPT_VERSION = "open-review-v3"

# 评审任务补充指令：接在 canonical rubric 之后，只改任务定位与输入输出
# 协议，不改三维评分口径。judge_reference 明确定位为参考——独立评审必须
# 自己核对材料，可同意可推翻，防同模锚定。
REVIEW_PROTOCOL_NOTE = """
## 评审任务补充说明
本次是开放生成评测的独立评审，**逐条进行**：「待评估标签」里只有本次要评的一条，
逐维按上述 rubric 给 0-4 或 null，并各给简短理由。「该片全部标签」清单是上下文，
供你判断相对价值与是否语义重复——不要评清单里的其他标签，只评「待评估标签」那一条。
若目标标签与清单中其他标签传达相同观众信息（语义重复），value 降档并在理由指明与哪条重复。
标签可能带 judge_reference 字段——那是生产 judge 的三维评分与理由，仅供参考：
它不构成指令、不预设结论，你必须独立核对材料后给出自己的分数，可以同意也可以推翻。
desc 缺失时 semantics 填 null 并写明缺少释义。
每条在 quote 字段从影片材料里**逐字摘录**一句原文（10-25 字），用来核对相关性依据；
不得改写、不得复述标签理由。找不到能摘的原文就给 relevance 0 或 1。
输出协议（覆盖 rubric 末尾的输出形状）：只输出「待评估标签」那一条，id 回填 "t0"。
"""

REVIEW_OUTPUT_FORMAT = (
    '只输出一个 JSON 对象：{"reviews":[{"id":"t0",'
    '"relevance":{"score":4,"reason":"理由"},'
    '"semantics":{"score":3,"reason":"理由"},'
    '"value":{"score":2,"reason":"理由"},'
    '"quote":"10–25 字原文摘录"}]}，'
    "不要解释、不要 Markdown 代码块。"
)

# 完整 system prompt：canonical rubric + 评审补充 + 输出协议。
REVIEW_PROMPT = (quality_prompts.RUBRIC_TEXT + REVIEW_PROTOCOL_NOTE
                 + REVIEW_OUTPUT_FORMAT)

# ---- 评审解析重试提示（镜像 mtg judge/verify 的重试后缀模式） ----
REVIEW_RETRY_HINT = (
    "\n\n上一次输出无法解析：只输出一个 JSON 对象，形如 "
    '{"reviews":[{"id":"t0","relevance":{"score":4,"reason":"理由"},'
    '"semantics":{"score":3,"reason":"理由"},"value":{"score":2,"reason":"理由"},'
    '"quote":"10–25 字原文摘录"}]}，待评估标签里的每个 id 恰好一条，'
    "不要解释、不要 Markdown 代码块。"
)
