"""asset_tag.level（标签层级：film/scene）

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04 18:10:00.000000

标签层级从"只在生成阶段内部区分"改为落库：每条标签都带 film（影片级）/
scene（场景级）。历史行按下列口径回填（不重跑生成也能看对）：
- 证据只涉及单个场景 → scene（场景级特征只需一个场景支撑）；
- 其余（无证据 / 多场景证据 / 目录标签）→ film。

新增行由生成流水线显式写入（MTG 合并结果的 scope；闭环与目录恒为 film），
本迁移只负责加列与历史回填。

加列/删列都先查 `PRAGMA table_info` 再执行：0006 曾以 "asset_tag.level"
身份在测试库（8101）落过库，重编号后同一库的升级会重复加列——幂等写法让
两种历史状态都能升到 0007。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set[str]:
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if "level" not in _columns(bind, "asset_tag"):
        op.add_column(
            'asset_tag',
            sa.Column('level', sa.Text(), nullable=False, server_default='film'),
        )

    # 历史回填：单场景证据 → scene。用 Python 侧解析 JSON，避免依赖
    # SQLite JSON1 扩展（生产库/测试库都开，但没必要多一个前提）。
    import json

    rows = bind.execute(
        sa.text("SELECT id, evidence_json FROM asset_tag WHERE evidence_json IS NOT NULL")
    ).fetchall()
    for row_id, raw in rows:
        try:
            evidence = json.loads(raw)
        except (TypeError, ValueError):
            continue
        scene_ids = {
            item.get("scene_id")
            for item in evidence
            if isinstance(item, dict) and item.get("scene_id")
        }
        if len(scene_ids) == 1:
            bind.execute(
                sa.text("UPDATE asset_tag SET level = 'scene' WHERE id = :id"),
                {"id": row_id},
            )


def downgrade() -> None:
    """Downgrade schema."""
    if "level" in _columns(op.get_bind(), "asset_tag"):
        with op.batch_alter_table('asset_tag') as batch_op:
            batch_op.drop_column('level')
