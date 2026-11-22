"""tag_change_log 加导入批键列 task_id + 索引（四层重构 Task 12）

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-22

导入批次反查（撤销/导出）原实现全表扫描 tag_change_log 后逐行 JSON 解析
after_json 按 task_id 文本匹配——O(全部审计行)。本迁移为 action='import'
审计行加可空批键列 `task_id`（治理流水线写入的反查键，替代 v1 的
batch_id）+ `idx_change_log_task` 索引，反查 SQL 化。

upgrade 两步：
1. 加列 + 建索引；
2. 回填存量行：逐行解析 action='import' 行的 after_json 取 task_id 写列
   （一次性 Python 侧回填在迁移内合法——量级 = import 审计行数）。
   坏 JSON / 无 task_id 键的行留 NULL（反查语义与旧实现一致：这类行本就
   不参与匹配）。生命周期行（create/update/merge/deprecate/delete）不动。

downgrade：drop 索引与列（反查退回旧全表扫描实现属代码层回滚，表结构
回到 0016 形态）。
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加批键列 + 索引，并从存量 import 行的 after_json 回填。"""
    op.add_column("tag_change_log", sa.Column("task_id", sa.Text, nullable=True))
    op.create_index("idx_change_log_task", "tag_change_log", ["task_id"])

    # 回填存量行：Python 侧逐行解析 after_json.task_id（坏 JSON/缺键留 NULL）
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT log_id, after_json FROM tag_change_log"
        " WHERE action = 'import' AND after_json IS NOT NULL"
    )).fetchall()
    for log_id, after_json in rows:
        try:
            payload = json.loads(after_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        task_id = payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            conn.execute(
                sa.text("UPDATE tag_change_log SET task_id = :task_id WHERE log_id = :log_id"),
                {"task_id": task_id, "log_id": log_id},
            )


def downgrade() -> None:
    """Drop 批键：索引与列（反查退回代码层旧实现，表结构回 0016 形态）。"""
    op.drop_index("idx_change_log_task", table_name="tag_change_log")
    op.drop_column("tag_change_log", "task_id")
