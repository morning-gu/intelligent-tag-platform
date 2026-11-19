"""tag_catalog.dir_type 扩枚举 dir_asset

目录类型新增第三种取值 `dir_asset`：由元数据导入/联网搜索获取对应标签
数据的目录。扩充语义与 `dir_dynamic` 相同（审核后可新增子标签），
`dir_static` 仍不可扩充——语义判定见 `library.Directory.allows_new_tags`。

SQLite 的 CHECK 约束无法 ALTER，沿用 0008 的临时表重建模式（PRAGMA
foreign_keys=OFF 包裹；self-FK 引用临时名，RENAME 时 SQLite 自动改写为
自引用）。目录行数据经 INSERT SELECT 全列保留、不改写任何存量值。

downgrade 反向重建回两值口径：`dir_asset` 行归一为 `dir_dynamic`
（两者扩充语义一致，回退保留可扩充能力），结构回退为主。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
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
        sa.ForeignKeyConstraint(["parent_id"], ["_tag_catalog_new.dir_id"]),
        sa.PrimaryKeyConstraint("dir_id"),
        sa.UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
    )
    op.execute(
        "INSERT INTO _tag_catalog_new (dir_id, dir_name, dir_type, description, "
        "parent_id, aliases, applicable_scope, embedding, created_at, updated_at) "
        "SELECT dir_id, dir_name, dir_type, description, parent_id, aliases, "
        "applicable_scope, embedding, created_at, updated_at FROM tag_catalog"
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
        sa.CheckConstraint("dir_type IN ('dir_static', 'dir_dynamic')"),
        sa.CheckConstraint(
            "applicable_scope IN ('content', 'scene', 'character', "
            "'content,scene', 'content,character', 'scene,character', "
            "'content,scene,character')"
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["_tag_catalog_old.dir_id"]),
        sa.PrimaryKeyConstraint("dir_id"),
        sa.UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
    )
    # dir_asset 行降级归一为 dir_dynamic：两者扩充语义一致，回退保留
    # 可扩充能力（downgrade 仅出现在 dir_asset 启用前的回滚场景）
    op.execute(
        "INSERT INTO _tag_catalog_old (dir_id, dir_name, dir_type, description, "
        "parent_id, aliases, applicable_scope, embedding, created_at, updated_at) "
        "SELECT dir_id, dir_name, "
        "CASE WHEN dir_type = 'dir_asset' THEN 'dir_dynamic' ELSE dir_type END, "
        "description, parent_id, aliases, "
        "applicable_scope, embedding, created_at, updated_at FROM tag_catalog"
    )
    op.drop_table("tag_catalog")
    op.rename_table("_tag_catalog_old", "tag_catalog")
    op.create_index("idx_tag_catalog_type", "tag_catalog", ["dir_type"])
    op.create_index("idx_tag_catalog_scope", "tag_catalog", ["applicable_scope"])

    op.execute("PRAGMA foreign_keys=ON")
