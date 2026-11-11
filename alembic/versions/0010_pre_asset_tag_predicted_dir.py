"""pre_asset_tag.predicted_dir_id：留住"生成时给的目录"，供归属评测对比。

为什么需要这一列
================

审核动作 `modify` 会把人工选定的目录直接写进 `candidate_dir_id`，生成时
系统给出的那个预测目录就此被覆盖、无迹可寻。于是"排序器准不准"永远无法
从数据里回答——只能靠人工感觉。

本迁移补一列 `predicted_dir_id`：**生成时写入，之后不可变**。审核改目录只
动 `candidate_dir_id`，两者一比就是一次天然的 A/B：

    预测被采纳 ← predicted_dir_id == candidate_dir_id
    预测被改掉 ← predicted_dir_id != candidate_dir_id

口径（避免误读）：

- `candidate_dir_id` = 人工最终采纳的目录（业务真相，审核可改）；
- `predicted_dir_id` = 生成流水线给出的目录（`dir_source` 是 model 还是
  fallback 都不影响本列语义），**只供评测读取，业务逻辑不得据此分支**；
- 命中库内标签（`tag_id` 非空）的行不参与归属评测——它们没有"预测"这个
  动作，本列留 NULL。

为什么不做整表重建
==================

SQLite 3.35+ 的 `ALTER TABLE ... ADD COLUMN` 是纯元数据操作，不需要像
0008 那样 `create_table + INSERT SELECT + drop + rename`。按 0009 的既有
做法直接加列即可，对生产库零风险，`render_as_batch` 也不会介入。

新建索引 `idx_pre_tag_predicted_dir`：评测脚本按本列做 IS NOT NULL /
`!=` 过滤，行数上去后没索引会全表扫。downgrade 反向删除。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pre_asset_tag",
        sa.Column("predicted_dir_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_pre_tag_predicted_dir", "pre_asset_tag", ["predicted_dir_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_pre_tag_predicted_dir", table_name="pre_asset_tag")
    op.drop_column("pre_asset_tag", "predicted_dir_id")