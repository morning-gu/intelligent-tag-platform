"""v2 表结构落地（第一批）：tag_generation_task + pre_asset_tag。

本轮只动这两张表，其余表（tag_catalog / asset_tag / review_record /
media_context）物理保持 v1 形状——文档里对它们的改造（目录/标签拆分、
入库复制、流水表删除）留待后续。

变更点：
1. `generation_task` → `tag_generation_task`：外键从 `context_id`（→media_context）
   改为 `asset_id`（→media_asset，NOT NULL），补 mode/status 的 CHECK 与
   `idx_gen_task_asset` / `idx_gen_task_status` 两个索引。存量行按
   `media_context.asset_id` 回填，回填不到的旧行删除（无资产的任务无意义）。
2. 新建 `pre_asset_tag`：生成结果 + 审核队列（asset×target×tag 去重）。
   `tag_id` / `candidate_dir_id` 本期暂指 `tag_catalog.tag_id`——v2 的目标是
   `tag_define` / `tag_catalog.dir_id`，但标签库拆分不在本轮。
3. 存量审核行从 `asset_tag` 迁入 `pre_asset_tag`（origin 取任务 mode，
   merged → approved），迁完清空 `asset_tag` / `review_record`：v2 起写入侧
   只落 pre 表，asset_tag 作为「审核通过后的正式挂载」待入库逻辑实现后再填。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_TASK_STATUSES = "status IN ('queued', 'running', 'succeeded', 'failed')"

_MIGRATE_PRE_ROWS = """
INSERT INTO pre_asset_tag (
    asset_id, target_type, target_id, tag_id, candidate_name, candidate_dir_id,
    candidate_description, origin, relevance_score, confidence, evidence_json,
    task_id, review_status, reviewer_id, review_comment, created_at, updated_at
)
SELECT
    t.asset_id,
    'content',
    t.asset_id,
    t.tag_id,
    t.candidate_name,
    NULL,
    t.candidate_description,
    COALESCE(gt.mode, 'open'),
    t.relevance_score,
    t.confidence,
    t.evidence_json,
    t.task_id,
    CASE t.review_status WHEN 'merged' THEN 'approved' ELSE t.review_status END,
    NULL,
    NULL,
    t.created_at,
    t.updated_at
FROM asset_tag t
LEFT JOIN tag_generation_task gt ON gt.task_id = t.task_id
"""


def upgrade() -> None:
    # --- 1. generation_task → tag_generation_task -----------------------
    # 先改名：SQLite 会把引用它的外键（asset_tag.task_id）一起改到新名字，
    # 后面批重建同表名时不会留下悬空外键。
    op.execute("ALTER TABLE generation_task RENAME TO tag_generation_task")
    op.execute("ALTER TABLE tag_generation_task ADD COLUMN asset_id TEXT")
    op.execute(
        "UPDATE tag_generation_task SET asset_id = ("
        "  SELECT m.asset_id FROM media_context m"
        "  WHERE m.context_id = tag_generation_task.context_id)"
    )
    op.execute("DELETE FROM tag_generation_task WHERE asset_id IS NULL")

    with op.batch_alter_table("tag_generation_task") as batch:
        batch.alter_column(
            "asset_id", existing_type=sa.Text(), nullable=False,
        )
        batch.drop_column("context_id")
        batch.create_foreign_key(
            "fk_tag_gen_task_asset", "media_asset", ["asset_id"], ["asset_id"]
        )
        batch.create_check_constraint(
            "ck_tag_gen_task_mode", "mode IN ('closed', 'open')"
        )
        batch.create_check_constraint(
            "ck_tag_gen_task_status", _TASK_STATUSES
        )
        batch.create_index("idx_gen_task_asset", ["asset_id"])
        batch.create_index("idx_gen_task_status", ["status"])

    # --- 2. pre_asset_tag ------------------------------------------------
    op.create_table(
        "pre_asset_tag",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Text(),
                  sa.ForeignKey("media_asset.asset_id"), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False,
                  server_default="content"),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("tag_id", sa.Text(),
                  sa.ForeignKey("tag_catalog.tag_id"), nullable=True),
        sa.Column("candidate_name", sa.Text(), nullable=True),
        sa.Column("candidate_dir_id", sa.Text(),
                  sa.ForeignKey("tag_catalog.tag_id"), nullable=True),
        sa.Column("candidate_description", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Text(),
                  sa.ForeignKey("tag_generation_task.task_id"), nullable=True),
        sa.Column("review_status", sa.Text(), nullable=False,
                  server_default="pending"),
        sa.Column("reviewer_id", sa.Text(), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('content', 'scene', 'character')",
            name="ck_pre_tag_target_type",
        ),
        sa.CheckConstraint("origin IN ('closed', 'open')", name="ck_pre_tag_origin"),
        sa.CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_pre_tag_review_status",
        ),
    )
    op.create_index("ux_pre_tag_catalog", "pre_asset_tag",
                    ["asset_id", "target_type", "target_id", "tag_id"],
                    unique=True, sqlite_where=sa.text("tag_id IS NOT NULL"))
    op.create_index(
        "ux_pre_tag_candidate", "pre_asset_tag",
        ["asset_id", "target_type", "target_id", "candidate_name"],
        unique=True,
        sqlite_where=sa.text("tag_id IS NULL AND candidate_name IS NOT NULL"),
    )
    op.create_index("idx_pre_tag_asset", "pre_asset_tag", ["asset_id"])
    op.create_index("idx_pre_tag_task", "pre_asset_tag", ["task_id"])
    op.create_index("idx_pre_tag_review", "pre_asset_tag", ["review_status"])

    # --- 3. 存量行迁移 ---------------------------------------------------
    op.execute(_MIGRATE_PRE_ROWS)
    op.execute("DELETE FROM review_record")
    op.execute("DELETE FROM asset_tag")


def downgrade() -> None:
    op.execute("DELETE FROM pre_asset_tag")
    op.drop_table("pre_asset_tag")

    with op.batch_alter_table("tag_generation_task") as batch:
        batch.drop_index("idx_gen_task_status")
        batch.drop_index("idx_gen_task_asset")
        batch.drop_constraint("ck_tag_gen_task_status", type_="check")
        batch.drop_constraint("ck_tag_gen_task_mode", type_="check")
        batch.drop_constraint("fk_tag_gen_task_asset", type_="foreignkey")
        batch.add_column(sa.Column("context_id", sa.Text(), nullable=True))
        batch.drop_column("asset_id")
    op.execute("ALTER TABLE tag_generation_task RENAME TO generation_task")
