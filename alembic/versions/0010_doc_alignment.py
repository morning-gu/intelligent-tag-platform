"""对齐设计文档：media_asset / asset_build_task 的约束、索引与默认值。

文档（2026-11 版 §3.1 / §3.10）与旧实现的三处差异在此补齐：

- `media_asset.asset_type` 增加 CHECK（movie|series|scene）；
- `asset_build_task.status` 增加 CHECK（queued|running|succeeded|failed）；
- 索引按文档命名：`idx_media_asset_parent`、`idx_media_asset_type`、
  `idx_build_task_asset`（旧名 `ix_asset_build_task_asset_id` 一并换掉）、
  `idx_build_task_status`；
- `created_at` 补 `DEFAULT CURRENT_TIMESTAMP`，`total_scenes/done_scenes`
  补 `DEFAULT 0`（SQLite 对应写法；代码仍显式赋值，默认值只是兜底）。

SQLite 不支持 `ALTER TABLE ADD CONSTRAINT`，CHECK 与默认值都通过 batch
重建表完成（`render_as_batch` 已在 env.py 打开，重建前显式关闭外键检查，
否则带子行的父表无法 drop）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_ASSET_TYPES = "asset_type IN ('movie', 'series', 'scene')"
_TASK_STATUSES = "status IN ('queued', 'running', 'succeeded', 'failed')"


def upgrade() -> None:
    # --- media_asset -----------------------------------------------------
    with op.batch_alter_table("media_asset") as batch:
        batch.create_check_constraint("ck_media_asset_asset_type", _ASSET_TYPES)
        batch.alter_column(
            "created_at", server_default=sa.text("CURRENT_TIMESTAMP"),
            existing_type=sa.DateTime(), existing_nullable=True,
        )
    op.create_index("idx_media_asset_parent", "media_asset", ["parent_id"])
    op.create_index("idx_media_asset_type", "media_asset", ["asset_type"])

    # --- asset_build_task ------------------------------------------------
    with op.batch_alter_table("asset_build_task") as batch:
        batch.create_check_constraint("ck_asset_build_task_status", _TASK_STATUSES)
        batch.alter_column(
            "created_at", server_default=sa.text("CURRENT_TIMESTAMP"),
            existing_type=sa.DateTime(), existing_nullable=True,
        )
        batch.alter_column(
            "total_scenes", server_default=sa.text("0"),
            existing_type=sa.Integer(), existing_nullable=False,
        )
        batch.alter_column(
            "done_scenes", server_default=sa.text("0"),
            existing_type=sa.Integer(), existing_nullable=False,
        )
    op.drop_index("ix_asset_build_task_asset_id", table_name="asset_build_task")
    op.create_index("idx_build_task_asset", "asset_build_task", ["asset_id"])
    op.create_index("idx_build_task_status", "asset_build_task", ["status"])


def downgrade() -> None:
    op.drop_index("idx_build_task_status", table_name="asset_build_task")
    op.drop_index("idx_build_task_asset", table_name="asset_build_task")
    op.create_index("ix_asset_build_task_asset_id", "asset_build_task", ["asset_id"])
    with op.batch_alter_table("asset_build_task") as batch:
        batch.alter_column("done_scenes", server_default=None,
                           existing_type=sa.Integer(), existing_nullable=False)
        batch.alter_column("total_scenes", server_default=None,
                           existing_type=sa.Integer(), existing_nullable=False)
        batch.alter_column("created_at", server_default=None,
                           existing_type=sa.DateTime(), existing_nullable=True)
        batch.drop_constraint("ck_asset_build_task_status", type_="check")

    op.drop_index("idx_media_asset_type", table_name="media_asset")
    op.drop_index("idx_media_asset_parent", table_name="media_asset")
    with op.batch_alter_table("media_asset") as batch:
        batch.alter_column("created_at", server_default=None,
                           existing_type=sa.DateTime(), existing_nullable=True)
        batch.drop_constraint("ck_media_asset_asset_type", type_="check")
