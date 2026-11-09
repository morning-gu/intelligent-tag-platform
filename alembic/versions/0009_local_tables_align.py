"""本地四表对齐：media_asset.updated_at / pre_asset_tag.candidate_alias / 构建任务索引

main 的 v2 重建（0007/0008）已经把四张本地表建到与本地逻辑兼容的形态，
本迁移只补上重建时漏掉的两列与一处索引命名差异，使「按 main 升级上来的库」
与本地 ORM 模型完全一致：

- `media_asset.updated_at`：资产列表的「最近更新时间」（没内容时等于上传时间，
  见 `api/routers/media.py`），重建时未带；
- `pre_asset_tag.candidate_alias`：开放生成候选的推荐别名（审核合并时直接落库，
  见 `review/service.py`），重建时未带；
- `asset_build_task` 索引：main 侧叫 `ix_asset_build_task_asset_id`，本地模型用
  `idx_build_task_asset` / `idx_build_task_status`，按本地命名统一（含 status
  索引本身是本地新增的）。

已知、可接受的结构漂移（不在本迁移里动，避免对生产库做整表重建）：
`asset_build_task.total_scenes/done_scenes` 与 `media_asset.created_at` 的服务端
默认值、以及若干 CHECK 约束的命名——语义等价，写入路径都显式赋值。

downgrade 反向删除两列与三个索引，恢复 main 侧索引名。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_asset", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("pre_asset_tag", sa.Column("candidate_alias", sa.Text(), nullable=True))

    op.drop_index("ix_asset_build_task_asset_id", table_name="asset_build_task")
    op.create_index("idx_build_task_asset", "asset_build_task", ["asset_id"])
    op.create_index("idx_build_task_status", "asset_build_task", ["status"])


def downgrade() -> None:
    op.drop_index("idx_build_task_status", table_name="asset_build_task")
    op.drop_index("idx_build_task_asset", table_name="asset_build_task")
    op.create_index("ix_asset_build_task_asset_id", "asset_build_task", ["asset_id"])

    op.drop_column("pre_asset_tag", "candidate_alias")
    op.drop_column("media_asset", "updated_at")
