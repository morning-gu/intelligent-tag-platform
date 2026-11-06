"""media asset: 内容落库（content_json / source / updated_at）

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05

资产内容从「media_context 每次导入新建一行」改为「media_asset 一行覆盖」：
- content_json：标准资产 JSON（结构对齐 new_json.txt），仅影片行有值；
- source：内容口径（v5-infer / v5-facts），仅影片行有值；
- updated_at：最近一次写入内容的时间（没内容时等于上传时间）。

本轮为过渡期：media_context 仍照常写入，标签生成侧继续读它；等标签生成
切到 content_json 后再删表。存量行按 created_at 回填 updated_at。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set[str]:
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    existing = _columns(bind, 'media_asset')
    # SQLite 的 ADD COLUMN 可增量执行；逐列判断，容忍重复执行
    if 'content_json' not in existing:
        op.add_column('media_asset', sa.Column('content_json', sa.Text(), nullable=True))
    if 'source' not in existing:
        op.add_column('media_asset', sa.Column('source', sa.Text(), nullable=True))
    if 'updated_at' not in existing:
        op.add_column('media_asset', sa.Column('updated_at', sa.DateTime(), nullable=True))
    # 存量行没有「内容更新时间」概念：按创建时间回填，排序/展示才不至于空
    bind.exec_driver_sql(
        "UPDATE media_asset SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    existing = _columns(bind, 'media_asset')
    for column in ('updated_at', 'source', 'content_json'):
        if column in existing:
            with op.batch_alter_table('media_asset') as batch:
                batch.drop_column(column)
