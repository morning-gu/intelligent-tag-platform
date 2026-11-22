"""内容资产构建（builder）的指令文案常量：唯一权威源（git 即版本机制）。

2026-11-22 文案集中（prompt-copy-centralization Task 2）：逐字搬自
services/content/assets/builder.py 的 scene_messages / summary_messages 的
无参数文案段。含参数槽的行（电影头/场景时间/已知角色/剧情/字幕行、嵌
MOODS/FEELINGS 词表的要求块）留在 builder 组装函数内——本模块只收
无参数槽的完整文案块。改文案 = 改常量 + 全量回归 + commit。

分层红线：本模块纯常量，不得 import fastapi / sqlalchemy /
tagplatform.services。
"""

# 场景分析请求的角色头（自报家门段，位于消息最前）
SCENE_ANALYSIS_ROLE_TEXT = (
    "你是影视内容分析助手。请结合这场戏的原始剧情、字幕行与关键帧，"
    "输出中文结构化分析。\n\n"
)

# 场景分析请求的输出格式行（消息最后一段；解析端是 builder._extract_json_object
# / parse_scene_result——输出形状两侧同改）
SCENE_ANALYSIS_OUTPUT_TEXT = (
    '只输出 JSON：{"description":"...","atmosphere":["悬疑神秘"],'
    '"events":[{"description":"...","participants":[{"name":"...",'
    '"emotions":["愤怒"]}]}],"virtual_time":"","location":"",'
    '"dialogues":[{"line":1,"speaker":"..."}]}'
)

# 整片摘要请求的任务头
SUMMARY_TASK_TEXT = (
    "把下面这部影片的英文剧情简介改写成中文整片剧情描述。\n"
)

# 整片摘要请求的要求段
SUMMARY_REQUIREMENTS_TEXT = (
    "要求：200–300 字，交代主要人物、核心冲突与结局走向；"
    "人名保留影片原名（英文），不要音译。\n"
)

# 整片摘要请求的输出格式行（解析端是 builder._extract_json_object /
# parse_summary——输出形状两侧同改）
SUMMARY_OUTPUT_TEXT = (
    '只输出 JSON：{"summary": "中文剧情描述"}'
)
