"""multimodal evidence for closed v1 prompt

Revision ID: 0016
Revises: 0015
Create Date: 2026-12-06 10:00:00.000000

Update the closed v1 prompt to emit the nested three-modality evidence
structure (content/scene/frame), each section carrying its own 1-5
evidence_strength and per-scene evidence summaries for fused Sevi scoring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_prompt_template = sa.table(
    "prompt_template",
    sa.column("mode", sa.Text),
    sa.column("version", sa.Integer),
    sa.column("template_text", sa.Text),
)

# upgrade 用新文案：本迁移时点 models.py 中 `_PROMPT_V1_CLOSED_TEXT` 的逐字
# 快照。迁移文件是历史快照，必须自包含——不随 models.py 演进（或删除）而失效。
_PROMPT_V1_CLOSED_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签按目录分组，以 JSON 格式附在上述正文之后。每组包含所属目录的",
    "名称与描述，组内标签给出 tag_id、tag_name 与 description。",
    "",
    "任务：从候选标签中选出适用于内容的标签。Applicability 和各模态的",
    "Evidence Strength 是独立维度，必须分别评分，不能互相替代。",
    "每个选中标签输出以下字段：",
    "- tag_id（必须与候选标签一致，不得编造）",
    "- applicability：整数 1 到 5。根据标签定义、使用条件和影视内容，判断标签是否适用，",
    "  以及是否构成作品的重要特征。1=完全不适用；2=存在弱关联但不满足主要适用条件；",
    "  3=部分符合标签定义但非主要特征；4=高度符合标签定义且具有明显内容特征；",
    "  5=完全符合标签定义且是作品核心特征。",
    "- evidence：对象，必须同时包含 content、scene、frame 三个模态节，",
    "  分别基于三种输入独立评分与举证：",
    "  - content 模态：基于剧情整体描述（摘要）判断；",
    "  - scene 模态：基于各场景的场景描述判断；",
    "  - frame 模态：基于随消息附上的关键帧图片判断（若无图片则基于正文推断）。",
    "  每个模态节包含两个字段：",
    "  - evidence_strength：整数 1 到 5，该模态下证据的直接性、明确程度和相互支撑程度。",
    "    1=缺乏有效证据，主要依赖推测；2=证据间接、模糊或可信度较低；",
    "    3=存在一定证据但覆盖不足；4=存在多个明确证据支持判断；",
    "    5=证据直接、充分且能够相互印证。",
    "  - evidences：该模态的证据摘要数组，每条包含 scene_id 和 evidence；",
    "    scene_id 必须来自正文已有场景，evidence 用简洁文字总结该场景中",
    "    该模态下的实际证据，不得编造或仅凭推测。",
    "",
    "只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。",
])

_PREVIOUS_CLOSED_V1_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签按目录分组，以 JSON 格式附在上述正文之后。每组包含所属目录的",
    "名称与描述，组内标签给出 tag_id、tag_name 与 description。",
    "",
    "任务：从候选标签中选出适用于内容的标签。Applicability 和 Evidence Strength",
    "是两个独立维度，必须分别评分，不能互相替代。每个选中标签输出：",
    "- tag_id（必须与候选标签一致，不得编造）",
    "- applicability：整数 1 到 5。根据标签定义、使用条件和影视内容，判断标签是否适用，",
    "  以及是否构成作品的重要特征。1=完全不适用；2=存在弱关联但不满足主要适用条件；",
    "  3=部分符合标签定义但非主要特征；4=高度符合标签定义且具有明显内容特征；",
    "  5=完全符合标签定义且是作品核心特征。",
    "- evidence_strength：整数 1 到 5。根据实际内容证据的直接性、明确程度和相互支撑程度评分。",
    "  1=缺乏有效证据，主要依赖推测；2=证据间接、模糊或可信度较低；",
    "  3=存在一定证据但覆盖不足；4=存在多个明确证据支持判断；",
    "  5=证据直接、充分且能够相互印证。",
    "- evidences：证据摘要数组，每条包含 scene_id 和 evidence；scene_id 必须来自正文已有场景，",
    "  evidence 用简洁文字总结该场景中的实际证据，不得编造或仅凭推测。",
    "",
    "只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。",
])


def upgrade() -> None:
    op.execute(
        _prompt_template.update()
        .where(sa.and_(_prompt_template.c.mode == "closed",
                       _prompt_template.c.version == 1))
        .values(template_text=_PROMPT_V1_CLOSED_TEXT)
    )


def downgrade() -> None:
    op.execute(
        _prompt_template.update()
        .where(sa.and_(_prompt_template.c.mode == "closed",
                       _prompt_template.c.version == 1))
        .values(template_text=_PREVIOUS_CLOSED_V1_TEXT)
    )
