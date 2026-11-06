"""asset build task: infer flag

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05

上传时可选「是否用大模型补充资产字段」。这个选择决定两件事：
1. 生成时是否调模型（False 只填数据集事实，快且免费）；
2. 内容口径（asset_builder:v5-infer / v5-facts）——两种口径不互相复用，
   换选之后重新上传同一部片会重生成，符合预期。

存量任务行按 True 回填（此前只有推断一种口径）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set[str]:
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    # SQLite 的 ADD COLUMN 带常量默认值可直接执行；重复执行时跳过
    # （与 0007 同样的幂等写法，容忍历史库状态）。
    if 'infer' not in _columns(bind, 'asset_build_task'):
        op.add_column(
            'asset_build_task',
            sa.Column('infer', sa.Boolean(), nullable=False, server_default='1'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if 'infer' in _columns(bind, 'asset_build_task'):
        with op.batch_alter_table('asset_build_task') as batch:
            batch.drop_column('infer')
