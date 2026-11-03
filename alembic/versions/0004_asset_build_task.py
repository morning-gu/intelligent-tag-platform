"""asset build task

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03

上传视频 → 自动生成资产的后台任务表。上传文件名命中数据集影片时建任务，
后台线程逐场景生成并回写进度，前端轮询 GET /media/build-tasks/{id}。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'asset_build_task',
        sa.Column('task_id', sa.Text(), nullable=False),
        sa.Column('asset_id', sa.Text(), nullable=False),
        sa.Column('movie_id', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('total_scenes', sa.Integer(), nullable=False),
        sa.Column('done_scenes', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['asset_id'], ['media_asset.asset_id']),
        sa.PrimaryKeyConstraint('task_id'),
    )
    op.create_index('ix_asset_build_task_asset_id', 'asset_build_task',
                    ['asset_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_asset_build_task_asset_id', table_name='asset_build_task')
    op.drop_table('asset_build_task')
