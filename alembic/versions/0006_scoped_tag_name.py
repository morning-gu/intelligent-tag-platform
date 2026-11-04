"""scope tag names by parent directory

原 PR #4 以 revision="0004"/down_revision="0003" 合入——与已上线的
0004_asset_build_task 撞号且从 0003 分叉（alembic 双头报错）。部署侧
重编号为 0006 接在 0005 之后，链恢复线性；迁移内容未改动。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")
    op.create_table(
        "_tag_catalog_new",
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
        sa.ForeignKeyConstraint(["parent_id"], ["_tag_catalog_new.tag_id"]),
        sa.PrimaryKeyConstraint("tag_id"),
        sa.UniqueConstraint("parent_id", "tag_name", name="ux_tag_catalog_parent_name"),
    )
    op.execute(
        "INSERT INTO _tag_catalog_new "
        "SELECT tag_id, tag_name, tag_type, description, parent_id, aliases, "
        "applicable_scope, status, embedding, created_at, updated_at "
        "FROM tag_catalog"
    )
    op.drop_table("tag_catalog")
    op.rename_table("_tag_catalog_new", "tag_catalog")
    op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    with op.batch_alter_table("tag_catalog") as batch_op:
        batch_op.drop_constraint("ux_tag_catalog_parent_name", type_="unique")
        batch_op.create_unique_constraint("tag_catalog_tag_name_key", ["tag_name"])
