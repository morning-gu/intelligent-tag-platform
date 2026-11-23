"""全量 Prompt 文案只读清单：capabilities/prompts 各模块的静态聚合。

2026-11-22 文案集中（prompt-copy-centralization Task 2）：给
GET /api/v1/prompts/registry 供数据——一处看全部文案（含 closed/open
mode 模板，形成唯一全量清单）。纯读、无 DB、无状态：条目文本即各模块
常量本体（单一来源，无第二份拷贝）。新增/改名常量时同步本清单与
对应测试（test_prompt_registry 的组覆盖断言）。

分层红线：本模块只 import 本包（.templates/.mtg/.assets/.quality/
.open_review），不得 import fastapi / sqlalchemy / tagplatform.services。
"""

from tagplatform.capabilities.prompts.assets import (
    SCENE_ANALYSIS_OUTPUT_TEXT, SCENE_ANALYSIS_ROLE_TEXT,
    SUMMARY_OUTPUT_TEXT, SUMMARY_REQUIREMENTS_TEXT, SUMMARY_TASK_TEXT,
)
from tagplatform.capabilities.prompts.mtg import (
    DESCRIPTION_INSTRUCTIONS, DESCRIPTION_RETRY_HINT, DIR_FIELD_RULE_TEXT,
    DIR_HINT_HEAD_TEXT, DIR_HINT_TAIL_TEXT, FILM_GOAL_TEXT,
    GENERATION_ROLE_TEXT, HOW_TO_FILL_TEXT, JUDGE_CRITERIA,
    JUDGE_RETRY_HINT, JUDGE_ROLE_BRIEF, LIBRARY_HINT_HEAD_TEXT,
    SCENE_GOAL_TEXT, VERIFY_INSTRUCTIONS, VERIFY_OUTPUT_FORMAT,
    VERIFY_RETRY_HINT,
)
from tagplatform.capabilities.prompts.open_review import (
    REVIEW_OUTPUT_FORMAT, REVIEW_PROMPT, REVIEW_PROMPT_VERSION,
    REVIEW_PROTOCOL_NOTE, REVIEW_RETRY_HINT,
)
from tagplatform.capabilities.prompts.quality import PROMPT_VERSION, RUBRIC_TEXT
from tagplatform.capabilities.prompts.templates import (
    CLOSED_RETRY_HINT, CLOSED_TEMPLATE_TEXT, OPEN_TEMPLATE_TEXT,
)


def list_prompt_registry() -> list[dict]:
    """返回五组只读清单（JSON 形状：module/title/items[name/text]）。

    组序 = closed → mtg → assets → quality → open_review（与文案集中
    设计的流水线分组一致，开放评审为 2026-11-22 open-eval 追加）。
    """
    return [
        {
            "module": "closed",
            "title": "闭环生成",
            "items": [
                {"name": "CLOSED_TEMPLATE_TEXT", "text": CLOSED_TEMPLATE_TEXT},
                {"name": "OPEN_TEMPLATE_TEXT", "text": OPEN_TEMPLATE_TEXT},
                {"name": "CLOSED_RETRY_HINT", "text": CLOSED_RETRY_HINT},
            ],
        },
        {
            "module": "mtg",
            "title": "开放流水线",
            "items": [
                {"name": "GENERATION_ROLE_TEXT", "text": GENERATION_ROLE_TEXT},
                {"name": "SCENE_GOAL_TEXT", "text": SCENE_GOAL_TEXT},
                {"name": "FILM_GOAL_TEXT", "text": FILM_GOAL_TEXT},
                {"name": "HOW_TO_FILL_TEXT", "text": HOW_TO_FILL_TEXT},
                {"name": "DIR_FIELD_RULE_TEXT", "text": DIR_FIELD_RULE_TEXT},
                {"name": "DIR_HINT_HEAD_TEXT", "text": DIR_HINT_HEAD_TEXT},
                {"name": "DIR_HINT_TAIL_TEXT", "text": DIR_HINT_TAIL_TEXT},
                {"name": "LIBRARY_HINT_HEAD_TEXT",
                 "text": LIBRARY_HINT_HEAD_TEXT},
                {"name": "JUDGE_ROLE_BRIEF", "text": JUDGE_ROLE_BRIEF},
                {"name": "JUDGE_CRITERIA", "text": JUDGE_CRITERIA},
                {"name": "JUDGE_RETRY_HINT", "text": JUDGE_RETRY_HINT},
                {"name": "DESCRIPTION_INSTRUCTIONS",
                 "text": DESCRIPTION_INSTRUCTIONS},
                {"name": "DESCRIPTION_RETRY_HINT",
                 "text": DESCRIPTION_RETRY_HINT},
                {"name": "VERIFY_INSTRUCTIONS", "text": VERIFY_INSTRUCTIONS},
                {"name": "VERIFY_OUTPUT_FORMAT", "text": VERIFY_OUTPUT_FORMAT},
                {"name": "VERIFY_RETRY_HINT", "text": VERIFY_RETRY_HINT},
            ],
        },
        {
            "module": "assets",
            "title": "内容资产构建",
            "items": [
                {"name": "SCENE_ANALYSIS_ROLE_TEXT",
                 "text": SCENE_ANALYSIS_ROLE_TEXT},
                {"name": "SCENE_ANALYSIS_OUTPUT_TEXT",
                 "text": SCENE_ANALYSIS_OUTPUT_TEXT},
                {"name": "SUMMARY_TASK_TEXT", "text": SUMMARY_TASK_TEXT},
                {"name": "SUMMARY_REQUIREMENTS_TEXT",
                 "text": SUMMARY_REQUIREMENTS_TEXT},
                {"name": "SUMMARY_OUTPUT_TEXT", "text": SUMMARY_OUTPUT_TEXT},
            ],
        },
        {
            "module": "quality",
            "title": "标签质量评估",
            "items": [
                {"name": "RUBRIC_TEXT", "text": RUBRIC_TEXT},
                {"name": "PROMPT_VERSION", "text": PROMPT_VERSION},
            ],
        },
        {
            "module": "open_review",
            "title": "开放评审",
            "items": [
                {"name": "REVIEW_PROMPT", "text": REVIEW_PROMPT},
                {"name": "REVIEW_PROTOCOL_NOTE", "text": REVIEW_PROTOCOL_NOTE},
                {"name": "REVIEW_OUTPUT_FORMAT", "text": REVIEW_OUTPUT_FORMAT},
                {"name": "REVIEW_RETRY_HINT", "text": REVIEW_RETRY_HINT},
                {"name": "REVIEW_PROMPT_VERSION", "text": REVIEW_PROMPT_VERSION},
            ],
        },
    ]
