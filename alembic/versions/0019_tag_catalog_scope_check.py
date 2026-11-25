"""收紧 tag_catalog.applicable_scope 为 content / scene / character，缺省 content。

Revision ID: 0019
Revises: 0018

目录适用范围本身为单值（content | scene | character），0008 引入的逗号组合
属于过度防御。本迁移重建 tag_catalog 表，将 CHECK 约束收紧为三选一、默认值
恢复为 'content'；若旧库存在历史逗号组合值，升级时归一取首个合法取值（否则
回退 'content'）。downgrade 恢复 0015/0018 时的组合 CHECK 与默认值。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0019"
down_revision: Union[str, Sequence[str], None] = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")

    op.create_table(
        "_tag_catalog_new",
        sa.Column("dir_id", sa.Text(), nullable=False),
        sa.Column("dir_name", sa.Text(), nullable=False),
        sa.Column("dir_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("applicable_scope", sa.Text(), nullable=True,
                  server_default="content"),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("dir_type IN ('dir_static', 'dir_dynamic', 'dir_asset')"),
        sa.CheckConstraint("applicable_scope IN ('content', 'scene', 'character')"),
        sa.ForeignKeyConstraint(["parent_id"], ["_tag_catalog_new.dir_id"]),
        sa.PrimaryKeyConstraint("dir_id"),
        sa.UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
    )
    op.execute(
        "INSERT INTO _tag_catalog_new (dir_id, dir_name, dir_type, description, "
        "parent_id, aliases, applicable_scope, embedding, created_at, updated_at) "
        "SELECT dir_id, dir_name, dir_type, description, parent_id, aliases, "
        "CASE "
        "WHEN applicable_scope IS NULL THEN NULL "
        "WHEN applicable_scope IN ('content', 'scene', 'character') THEN applicable_scope "
        "WHEN substr(applicable_scope, 1, instr(applicable_scope || ',', ',') - 1) "
        "IN ('content', 'scene', 'character') "
        "THEN substr(applicable_scope, 1, instr(applicable_scope || ',', ',') - 1) "
        "ELSE 'content' END, "
        "embedding, created_at, updated_at FROM tag_catalog"
    )
    op.drop_table("tag_catalog")
    op.rename_table("_tag_catalog_new", "tag_catalog")
    op.create_index("idx_tag_catalog_type", "tag_catalog", ["dir_type"])
    op.create_index("idx_tag_catalog_scope", "tag_catalog", ["applicable_scope"])

    op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")

    op.create_table(
        "_tag_catalog_old",
        sa.Column("dir_id", sa.Text(), nullable=False),
        sa.Column("dir_name", sa.Text(), nullable=False),
        sa.Column("dir_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("applicable_scope", sa.Text(), nullable=True,
                  server_default="content,scene,character"),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("dir_type IN ('dir_static', 'dir_dynamic', 'dir_asset')"),
        sa.CheckConstraint(
            "applicable_scope IN ('content', 'scene', 'character', "
            "'content,scene', 'content,character', 'scene,character', "
            "'content,scene,character')"
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["_tag_catalog_old.dir_id"]),
        sa.PrimaryKeyConstraint("dir_id"),
        sa.UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
    )
    op.execute(
        "INSERT INTO _tag_catalog_old (dir_id, dir_name, dir_type, description, "
        "parent_id, aliases, applicable_scope, embedding, created_at, updated_at) "
        "SELECT dir_id, dir_name, dir_type, description, parent_id, aliases, "
        "applicable_scope, embedding, created_at, updated_at FROM tag_catalog"
    )
    op.drop_table("tag_catalog")
    op.rename_table("_tag_catalog_old", "tag_catalog")
    op.create_index("idx_tag_catalog_type", "tag_catalog", ["dir_type"])
    op.create_index("idx_tag_catalog_scope", "tag_catalog", ["applicable_scope"])

    op.execute("PRAGMA foreign_keys=ON")
