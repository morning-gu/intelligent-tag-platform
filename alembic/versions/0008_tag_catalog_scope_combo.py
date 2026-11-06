"""tag_catalog.applicable_scope 扩为 content/scene/character 组合

模板扩展字段链路（docs/movietags-template.xlsx 双表模板）：目录适用范围
从单值（content|scene|character）扩为三值组合（7 种非空子集，逗号分隔
规范序），缺省从 'content' 改为三值全选 'content,scene,character'。

SQLite 改 CHECK 需重建表：沿用 0006/0007 的临时表重建模式（PRAGMA
foreign_keys=OFF 包裹；self-FK 引用临时名，RENAME 时 SQLite 自动改写为
自引用）。目录行数据经 INSERT SELECT 全列保留；tag_define.dir_id 外键
在 PRAGMA OFF 下不受重建影响。downgrade 反向重建回单值口径：拷贝时组合值归一取首 token
（'content,scene' → 'content'），结构回退为主。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
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
        sa.CheckConstraint("dir_type IN ('dir_static', 'dir_dynamic')"),
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
        sa.Column("applicable_scope", sa.Text(), nullable=True, server_default="content"),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("dir_type IN ('dir_static', 'dir_dynamic')"),
        sa.CheckConstraint("applicable_scope IN ('content', 'scene', 'character')"),
        sa.ForeignKeyConstraint(["parent_id"], ["_tag_catalog_old.dir_id"]),
        sa.PrimaryKeyConstraint("dir_id"),
        sa.UniqueConstraint("parent_id", "dir_name", name="ux_tag_catalog_parent_name"),
    )
    # 组合值行降级归一：取首个 token（结构回退为主，downgrade 场景
    # 仅出现在导入功能启用前的回滚，组合行为 0008 期间新写入）
    op.execute(
        "INSERT INTO _tag_catalog_old (dir_id, dir_name, dir_type, description, "
        "parent_id, aliases, applicable_scope, embedding, created_at, updated_at) "
        "SELECT dir_id, dir_name, dir_type, description, parent_id, aliases, "
        "CASE WHEN applicable_scope IS NULL THEN NULL "
        "ELSE substr(applicable_scope, 1, "
        "instr(applicable_scope || ',', ',') - 1) END, "
        "embedding, created_at, updated_at FROM tag_catalog"
    )
    op.drop_table("tag_catalog")
    op.rename_table("_tag_catalog_old", "tag_catalog")
    op.create_index("idx_tag_catalog_type", "tag_catalog", ["dir_type"])
    op.create_index("idx_tag_catalog_scope", "tag_catalog", ["applicable_scope"])

    op.execute("PRAGMA foreign_keys=ON")
