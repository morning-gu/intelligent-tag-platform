"""standardized scoring dimensions for closed v1 prompt

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-18 15:00:00.000000

Update the closed v1 prompt to emit standardized applicability and evidence
strength scores with per-scene evidence summaries.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from tagplatform.db.models import _PROMPT_V1_CLOSED_TEXT


revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_prompt_template = sa.table(
    "prompt_template",
    sa.column("mode", sa.Text),
    sa.column("version", sa.Integer),
    sa.column("template_text", sa.Text),
)

_PREVIOUS_CLOSED_V1_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签按目录分组，以 JSON 格式附在上述正文之后。每组包含所属目录的",
    "名称与描述，组内标签给出 tag_id、tag_name 与 description。",
    "",
    "任务：从候选标签中选出与内容真正相关的标签。对每个选中标签输出：",
    "- tag_id（必须与候选标签一致，不得编造）",
    "- tag_name（与候选一致）",
    "- relevance：标签与内容的相关程度，0 到 1",
    "- confidence：你对该判断的置信度，0 到 1",
    "- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description",
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
