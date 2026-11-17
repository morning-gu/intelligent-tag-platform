"""closed v1 prompt: candidates grouped as JSON in producer

Revision ID: 0012
Revises: 0011
Create Date: 2026-12-14 10:00:00.000000

closed 模式自 2026-12 起候选标签不再经 `{candidates}` 占位符注入
（runner 不再组装候选文本），改由 ClosedTagProducer 按目录分组、以 JSON
追加在渲染后的正文末尾。因此 closed v1 模板重写：只保留 `{content}`
占位符，正文说明候选分组 JSON 的注入位置与输出契约不变（五键 JSON 数组）。

对既有库做 UPDATE 而非新插入 v2 行：保持 mode+version=(closed,1) 的
身份不变，任务表引用的 prompt_version="1" 不漂移；create_all 路径
（测试 fixture）由 after_create 钩子用同一常量播种，两条路径不会漂移。
downgrade 把旧文案（{candidates} 版）字面还原。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from tagplatform.db.models import _PROMPT_V1_CLOSED_TEXT


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Lightweight table shape for data DML (alembic recommendation for seed rows).
_prompt_template = sa.table(
    'prompt_template',
    sa.column('mode', sa.Text),
    sa.column('version', sa.Integer),
    sa.column('template_text', sa.Text),
    sa.column('is_active', sa.Boolean),
    sa.column('created_at', sa.DateTime),
)

# 0002 版 closed v1 的旧文案（{candidates} 占位符版）。downgrade 还原用；
# 保留字面量而非 import 常量——旧文案在 models.py 中已被替换，历史版本
# 必须自包含（迁移文件是历史快照，不随代码演进）。
_OLD_CLOSED_V1_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签清单（格式：tag_id | 名称 | 分类 | 定义）：",
    "{candidates}",
    "",
    "任务：从候选清单中选出与内容真正相关的标签。对每个选中标签输出：",
    "- tag_id（必须与候选清单一致，不得编造）",
    "- tag_name（与候选一致）",
    "- relevance：标签与内容的相关程度，0 到 1",
    "- confidence：你对该判断的置信度，0 到 1",
    "- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description",
    "",
    "只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。",
])


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        _prompt_template.update()
        .where(sa.and_(_prompt_template.c.mode == 'closed',
                       _prompt_template.c.version == 1))
        .values(template_text=_PROMPT_V1_CLOSED_TEXT))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        _prompt_template.update()
        .where(sa.and_(_prompt_template.c.mode == 'closed',
                       _prompt_template.c.version == 1))
        .values(template_text=_OLD_CLOSED_V1_TEXT))
