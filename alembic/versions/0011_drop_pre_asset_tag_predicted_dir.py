"""删除 pre_asset_tag.predicted_dir_id：评测快照列随 review 口径一并退役。

背景
====

0010 补了 `predicted_dir_id`（生成时预测目录快照），支撑
`eval_dir_assignment.py --method review` 的人工审核留痕口径。复盘后结论：
该列**没有任何业务读者**，MTG 流水线也从不显式设置它（落库时永远退化成
`tag.dir_id`），唯一价值是"审核改目录后原值留存"——而这条口径需要几百条
已定稿样本才有统计意义，冷启动阶段用不上。为消除"迁移曾存在过"与
"列是否被业务依赖"的疑虑成本，整体退役：

- 删列 + 删 `idx_pre_tag_predicted_dir` 索引（本迁移）；
- 删模型字段 / `ProducedTag.predicted_dir_id` / `persist_tags` 落库逻辑；
- 删评测脚本的 review 口径（`--method` 参数一并移除，只留 library 留一法）；
- 迁移 0010 文件**保留**（main 上已有库升到过 0010，历史链不能断）。

与 0010 的 downgrade 的区别：downgrade 是"回到没这列的过去"（供本地回退
调试），0011 是"向前推进的正式退役"——alembic head 前移，所有库统一到
无该列的终态。

实现
====

- `DROP INDEX` + `DROP COLUMN`（SQLite 3.35+，纯元数据操作，与 0010 的
  `ADD COLUMN` 对称，零风险、无整表重建）；
- downgrade 重建列与索引（照抄 0010 upgrade，供万一回退；快照数据已丢，
  重建后全 NULL——与 0010 downgrade 的已知代价同口径）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_pre_tag_predicted_dir", table_name="pre_asset_tag")
    op.drop_column("pre_asset_tag", "predicted_dir_id")


def downgrade() -> None:
    op.add_column(
        "pre_asset_tag",
        sa.Column("predicted_dir_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_pre_tag_predicted_dir", "pre_asset_tag", ["predicted_dir_id"]
    )
