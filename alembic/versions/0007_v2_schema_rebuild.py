"""v2 表结构重建（目录/标签拆分、pre/正式分离、media 合并）

doc table_design.md §8 迁移策略：服务器业务数据已清零（2026-11-05），0007
对 v2 变更所及的 7 张 v1 业务表 drop 重建（数据不迁移）；media_asset 按
doc §3.1 定稿 DDL 重建——v1 七列 + content_json / source（v1 + media_context
合并，识别状态 = content_json IS NULL）+ asset_type CHECK +
idx_media_asset_parent / idx_media_asset_type 索引，数据行经 INSERT SELECT
保留（服务器业务数据已清零，零数据成本）；user_account / prompt_template /
asset_build_task 三表物理不动。升级库（迁移路径）与 create_all 库（测试
路径）由此同构。

v2 变更对照（doc §2.1）：
- 目录/标签拆双表：tag_catalog（目录树，dir_static/dir_dynamic）+
  tag_define（标签定义，val_fixed/val_generalized）；
- 生成产出与审核折进 pre_asset_tag（行即日志，reviewer/comment 内嵌），
  正式挂载独立为干净的 asset_tag，review_record 表删除；
- generation_task → tag_generation_task（外键随 media 合并改 asset_id）、
  import_batch → tag_import_task（改语义名，结构不变）。

建表 DDL 与 db/models.py v2 模型逐字对应（列/类型/CHECK/索引）；本环境
alembic 为非事务 DDL，PRAGMA foreign_keys=OFF 在迁移内真实生效（0006 同款
包裹先例）。downgrade 反向重建 v1 空表——业务数据已清零口径下不回迁数据
（仅结构回退，重建的表为空）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")

    # 1) drop v1 业务表（子表在前；tag_change_log / import_batch 无外键）
    op.drop_table("review_record")
    op.drop_table("asset_tag")
    op.drop_table("generation_task")
    op.drop_table("media_context")
    op.drop_table("tag_change_log")
    op.drop_table("import_batch")
    op.drop_table("tag_catalog")

    # 2) media_asset 重建对齐 v2（v1 + media_context 合并，doc §3.1 定稿
    #    DDL）：追加 content_json / source 两列，补齐 asset_type CHECK 与
    #    parent/type 索引；数据行 INSERT SELECT 保留。沿用 0006 的临时表
    #    重建模式（self-FK 引用临时名，RENAME 时 SQLite 自动改写为自引用）。
    op.create_table(
        "_media_asset_new",
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("video_uri", sa.Text(), nullable=True),
        sa.Column("content_json", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("asset_type IN ('movie', 'series', 'scene')"),
        sa.ForeignKeyConstraint(["parent_id"], ["_media_asset_new.asset_id"]),
        sa.PrimaryKeyConstraint("asset_id"),
    )
    op.execute(
        "INSERT INTO _media_asset_new (asset_id, asset_type, parent_id, title, "
        "duration_ms, video_uri, content_json, source, created_at) "
        "SELECT asset_id, asset_type, parent_id, title, duration_ms, video_uri, "
        "NULL, NULL, created_at FROM media_asset"
    )
    op.drop_table("media_asset")
    op.rename_table("_media_asset_new", "media_asset")
    op.create_index("idx_media_asset_parent", "media_asset", ["parent_id"])
    op.create_index("idx_media_asset_type", "media_asset", ["asset_type"])

    # 3) create v2 新表（父表在前），DDL 与 db/models.py 逐字对应
    # tag_catalog：目录树（目录本身不是标签，doc §3.2）
    op.create_table(
        "tag_catalog",
        sa.Column("dir_id", sa.Text(), nullable=False),
        sa.Column("dir_name", sa.Text(), nullable=False),
        sa.Column("dir_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("applicable_scope", sa.Text(), server_default="content", nullable=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("dir_type IN ('dir_static', 'dir_dynamic')"),
        sa.CheckConstraint("applicable_scope IN ('content', 'scene', 'character')"),
        sa.ForeignKeyConstraint(["parent_id"], ["tag_catalog.dir_id"]),
        sa.PrimaryKeyConstraint("dir_id"),
        sa.UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
    )
    op.create_index("idx_tag_catalog_type", "tag_catalog", ["dir_type"])
    op.create_index("idx_tag_catalog_scope", "tag_catalog", ["applicable_scope"])

    # tag_define：标签定义（标签适用范围继承目录，不在本表重复声明，doc §3.3）
    op.create_table(
        "tag_define",
        sa.Column("tag_id", sa.Text(), nullable=False),
        sa.Column("tag_name", sa.Text(), nullable=False),
        sa.Column("tag_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("dir_id", sa.Text(), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("tag_type IN ('val_fixed', 'val_generalized')"),
        sa.CheckConstraint("status IN ('active', 'deprecated')"),
        sa.ForeignKeyConstraint(["dir_id"], ["tag_catalog.dir_id"]),
        sa.PrimaryKeyConstraint("tag_id"),
        sa.UniqueConstraint("dir_id", "tag_name", name="ux_tag_define_dir_name"),
    )
    op.create_index("idx_tag_define_status", "tag_define", ["status"])
    op.create_index("idx_tag_define_dir", "tag_define", ["dir_id"])

    # tag_generation_task：生成任务（外键从 context_id 改 asset_id，doc §3.4）
    op.create_table(
        "tag_generation_task",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("policy_json", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("mode IN ('closed', 'open')"),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.asset_id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("idx_gen_task_asset", "tag_generation_task", ["asset_id"])
    op.create_index("idx_gen_task_status", "tag_generation_task", ["status"])

    # pre_asset_tag：待审核标签行（行即日志；两个部分唯一索引即重跑幂等边界，
    # doc §3.5/§6.1）
    op.create_table(
        "pre_asset_tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), server_default="content", nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("tag_id", sa.Text(), nullable=True),
        sa.Column("candidate_name", sa.Text(), nullable=True),
        sa.Column("candidate_dir_id", sa.Text(), nullable=True),
        sa.Column("candidate_description", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("relevance_score", sa.REAL(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("review_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("reviewer_id", sa.Text(), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("target_type IN ('content', 'scene', 'character')"),
        sa.CheckConstraint("origin IN ('closed', 'open')"),
        sa.CheckConstraint("review_status IN ('pending', 'approved', 'rejected')"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.asset_id"]),
        sa.ForeignKeyConstraint(["tag_id"], ["tag_define.tag_id"]),
        sa.ForeignKeyConstraint(["candidate_dir_id"], ["tag_catalog.dir_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tag_generation_task.task_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_pre_tag_catalog",
        "pre_asset_tag",
        ["asset_id", "target_type", "target_id", "tag_id"],
        unique=True,
        sqlite_where=sa.text("tag_id IS NOT NULL"),
    )
    op.create_index(
        "ux_pre_tag_candidate",
        "pre_asset_tag",
        ["asset_id", "target_type", "target_id", "candidate_name"],
        unique=True,
        sqlite_where=sa.text("tag_id IS NULL AND candidate_name IS NOT NULL"),
    )
    op.create_index("idx_pre_tag_asset", "pre_asset_tag", ["asset_id"])
    op.create_index("idx_pre_tag_task", "pre_asset_tag", ["task_id"])
    op.create_index("idx_pre_tag_review", "pre_asset_tag", ["review_status"])

    # asset_tag：正式挂载（tag_name 冗余拷贝，唯一键含 tag_name 为泛化值预留，
    # doc §3.6）
    op.create_table(
        "asset_tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), server_default="content", nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("tag_id", sa.Text(), nullable=False),
        sa.Column("tag_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("target_type IN ('content', 'scene', 'character')"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.asset_id"]),
        sa.ForeignKeyConstraint(["tag_id"], ["tag_define.tag_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "asset_id", "target_type", "target_id", "tag_id", "tag_name",
            name="ux_asset_tag",
        ),
    )
    op.create_index("idx_asset_tag_asset", "asset_tag", ["asset_id"])
    op.create_index("idx_asset_tag_tag", "asset_tag", ["tag_id"])

    # tag_import_task：导入任务（v1 import_batch 改语义名，结构不变，doc §3.7）
    op.create_table(
        "tag_import_task",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('processing', 'completed', 'failed', 'revoked')"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("idx_import_task_status", "tag_import_task", ["status"])

    # tag_change_log：变更日志（tag_id 不加外键——可指向已废弃/合并的标签，
    # doc §3.8）
    op.create_table(
        "tag_change_log",
        sa.Column("log_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("action IN ('create', 'update', 'merge', 'deprecate', 'import')"),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("idx_change_log_tag", "tag_change_log", ["tag_id"])

    op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    """反向重建 v1 空表。

    业务数据已清零口径下不回迁数据（0007 期间 drop 的 v1 业务行不复活，
    重建的表为空）；media_asset 重建回 v1 形状时既有行经 INSERT SELECT
    保留。
    """
    op.execute("PRAGMA foreign_keys=OFF")

    # 1) drop v2 新表（子表在前）
    op.drop_table("tag_change_log")
    op.drop_table("tag_import_task")
    op.drop_table("asset_tag")
    op.drop_table("pre_asset_tag")
    op.drop_table("tag_generation_task")
    op.drop_table("tag_define")
    op.drop_table("tag_catalog")

    # 2) media_asset 重建回 v1 形状（摘除合并列与 CHECK/索引，数据行保留）
    op.create_table(
        "_media_asset_v1",
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("video_uri", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["_media_asset_v1.asset_id"]),
        sa.PrimaryKeyConstraint("asset_id"),
    )
    op.execute(
        "INSERT INTO _media_asset_v1 (asset_id, asset_type, parent_id, title, "
        "duration_ms, video_uri, created_at) "
        "SELECT asset_id, asset_type, parent_id, title, duration_ms, video_uri, "
        "created_at FROM media_asset"
    )
    op.drop_table("media_asset")
    op.rename_table("_media_asset_v1", "media_asset")

    # 3) 重建 v1 空表（0001 形状；tag_catalog 为 0006 重建后的形状）
    op.create_table(
        "tag_catalog",
        sa.Column("tag_id", sa.Text(), nullable=False),
        sa.Column("tag_name", sa.Text(), nullable=False),
        sa.Column("tag_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("applicable_scope", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["tag_catalog.tag_id"]),
        sa.PrimaryKeyConstraint("tag_id"),
        sa.UniqueConstraint("parent_id", "tag_name", name="ux_tag_catalog_parent_name"),
    )
    op.create_table(
        "import_batch",
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_table(
        "media_context",
        sa.Column("context_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("recognition_status", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.asset_id"]),
        sa.PrimaryKeyConstraint("context_id"),
    )
    op.create_index("ix_media_context_asset_id", "media_context", ["asset_id"])
    op.create_table(
        "generation_task",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("context_id", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("policy_json", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["context_id"], ["media_context.context_id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "asset_tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("tag_id", sa.Text(), nullable=True),
        sa.Column("candidate_name", sa.Text(), nullable=True),
        sa.Column("candidate_type", sa.Text(), nullable=True),
        sa.Column("candidate_description", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("relevance_score", sa.REAL(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("review_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.asset_id"]),
        sa.ForeignKeyConstraint(["tag_id"], ["tag_catalog.tag_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["generation_task.task_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_asset_tag_candidate",
        "asset_tag",
        ["asset_id", "candidate_name"],
        unique=True,
        sqlite_where=sa.text("tag_id IS NULL AND candidate_name IS NOT NULL"),
    )
    op.create_index(
        "ux_asset_tag_catalog",
        "asset_tag",
        ["asset_id", "tag_id"],
        unique=True,
        sqlite_where=sa.text("tag_id IS NOT NULL"),
    )
    op.create_table(
        "review_record",
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("result_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reviewer_id", sa.Text(), nullable=True),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["result_id"], ["asset_tag.id"]),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_table(
        "tag_change_log",
        sa.Column("log_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("ix_tag_change_log_tag_id", "tag_change_log", ["tag_id"])

    op.execute("PRAGMA foreign_keys=ON")
