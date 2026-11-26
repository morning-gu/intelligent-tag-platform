"""全量 Prompt 文案只读清单：capabilities/prompts 各模块的静态聚合。

给 `GET /api/v1/prompts/registry` 供数据——一处查看全部提示词模板。
纯读、无 DB、无状态：条目文本即各模块常量本体（单一来源，无第二份拷贝）。
新增或改名公开常量时同步本清单与 `tests/test_prompt_registry.py`。

分层红线：本模块只 import 本包（.templates/.mtg/.assets/.open_review），
不得 import fastapi / sqlalchemy / tagplatform.services。
"""

from tagplatform.capabilities.prompts.assets import (
    SCENE_ANALYSIS_OUTPUT_TEXT, SCENE_ANALYSIS_ROLE_TEXT,
    SUMMARY_OUTPUT_TEXT, SUMMARY_REQUIREMENTS_TEXT, SUMMARY_TASK_TEXT,
)
from tagplatform.capabilities.prompts.mtg import (
    DESCRIPTION_RETRY_HINT, DESCRIPTION_TEMPLATE,
    FILM_GENERATION_TEMPLATE, JUDGE_RETRY_HINT, JUDGE_TEMPLATE,
    SCENE_GENERATION_TEMPLATE, VERIFY_RETRY_HINT, VERIFY_TEMPLATE,
)
from tagplatform.capabilities.prompts.open_review import (
    REVIEW_PROMPT, REVIEW_PROMPT_VERSION, REVIEW_RETRY_HINT,
)
from tagplatform.capabilities.prompts.templates import (
    CLOSED_ASSEMBLY_NOTE, CLOSED_CONFIDENCE_ASSETS_NOTE,
    CLOSED_CONFIDENCE_CCONS_NOTE,
    CLOSED_CONFIDENCE_CMOD_NOTE, CLOSED_CONFIDENCE_CVER_NOTE,
    CLOSED_RETRY_HINT, CLOSED_TEMPLATE_TEXT, CLOSED_VERIFICATION_PROMPT,
)


def list_prompt_registry() -> list[dict]:
    """返回四组只读清单（JSON 形状：module/title/items[name/text]）。

    组序 = closed → mtg → assets → open_review。
    """
    return [
        {
            "module": "closed",
            "title": "闭环生成",
            "items": [
                {"name": "CLOSED_TEMPLATE_TEXT", "text": CLOSED_TEMPLATE_TEXT},
                {"name": "CLOSED_RETRY_HINT", "text": CLOSED_RETRY_HINT},
                {"name": "CLOSED_VERIFICATION_PROMPT",
                 "text": CLOSED_VERIFICATION_PROMPT},
                {"name": "CLOSED_ASSEMBLY_NOTE", "text": CLOSED_ASSEMBLY_NOTE},
                {"name": "CLOSED_CONFIDENCE_ASSETS_NOTE",
                 "text": CLOSED_CONFIDENCE_ASSETS_NOTE},
                {"name": "CLOSED_CONFIDENCE_CCONS_NOTE",
                 "text": CLOSED_CONFIDENCE_CCONS_NOTE},
                {"name": "CLOSED_CONFIDENCE_CVER_NOTE",
                 "text": CLOSED_CONFIDENCE_CVER_NOTE},
                {"name": "CLOSED_CONFIDENCE_CMOD_NOTE",
                 "text": CLOSED_CONFIDENCE_CMOD_NOTE},
            ],
        },
        {
            "module": "mtg",
            "title": "开放流水线",
            "items": [
                {"name": "SCENE_GENERATION_TEMPLATE",
                 "text": SCENE_GENERATION_TEMPLATE},
                {"name": "FILM_GENERATION_TEMPLATE",
                 "text": FILM_GENERATION_TEMPLATE},
                {"name": "JUDGE_TEMPLATE", "text": JUDGE_TEMPLATE},
                {"name": "JUDGE_RETRY_HINT", "text": JUDGE_RETRY_HINT},
                {"name": "DESCRIPTION_TEMPLATE", "text": DESCRIPTION_TEMPLATE},
                {"name": "DESCRIPTION_RETRY_HINT",
                 "text": DESCRIPTION_RETRY_HINT},
                {"name": "VERIFY_TEMPLATE", "text": VERIFY_TEMPLATE},
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
            "module": "open_review",
            "title": "开放评审",
            "items": [
                {"name": "REVIEW_PROMPT", "text": REVIEW_PROMPT},
                {"name": "REVIEW_RETRY_HINT", "text": REVIEW_RETRY_HINT},
                {"name": "REVIEW_PROMPT_VERSION",
                 "text": REVIEW_PROMPT_VERSION},
            ],
        },
    ]
