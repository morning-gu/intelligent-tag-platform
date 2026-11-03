"""open prompt deprecated marker

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 17:20:00.000000

open 模式自 2026-11 起改由内置流水线（generation/mtg, mtg-v1）接管，不再
消费 prompt_template 里 open 的模板。按"不删但标注过时"的口径：v1 行原样
保留，本迁移追加一条 inactive 的 v2 作为过时标记（提示词管理页可见）。

种子行与 `db/models.PROMPT_DEPRECATED_OPEN_SEED` 共用，create_all 路径
（测试 fixture）由 after_create 钩子插入同一行，两条路径不会漂移。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from tagplatform.db.models import PROMPT_DEPRECATED_OPEN_SEED


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Lightweight table shape for data DML（alembic 推荐的播种写法）
_prompt_template = sa.table(
    'prompt_template',
    sa.column('mode', sa.Text),
    sa.column('version', sa.Integer),
    sa.column('template_text', sa.Text),
    sa.column('is_active', sa.Boolean),
    sa.column('created_at', sa.DateTime),
)


def upgrade() -> None:
    """Upgrade schema."""
    op.bulk_insert(_prompt_template,
                   [dict(row) for row in PROMPT_DEPRECATED_OPEN_SEED])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        _prompt_template.delete().where(
            sa.and_(_prompt_template.c.mode == 'open',
                    _prompt_template.c.version == 2)))
