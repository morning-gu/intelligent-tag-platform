"""drop prompt_template（prompt 本地化，四层重构 Task 3）

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-21

Prompt 文案本地化为代码常量（tagplatform.capabilities.prompts.templates，
git 即版本机制），prompt_template 表及其 DB 版本机制退役——本迁移 drop
该表。downgrade 重建表结构（列契约自被删的 PromptTemplate ORM 定义逐字
抄录，与 0002 建表 DDL 同源）并从 templates.py 常量重新播种 closed/open
v1 激活行各一条——旧镜像回滚路径（先 `alembic downgrade` 再回滚镜像）
依赖这两行可读（closed 为生成流水线消费；open 由 mtg-v1 自拼 prompt，
不读库，行仅保契约完整）。

与 0002/0005/0012/0013/0016 的"字面量冻结"不同：本迁移的 downgrade 按
任务定义从 templates.py 常量取文案（回滚即回到当前代码口径，正合"git
即版本"）。created_at 沿用 0002 的 v1 种子创作日期（确定性：同一种子
在任何环境落库时间戳一致）。
"""
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from tagplatform.capabilities.prompts.templates import CLOSED_TEMPLATE_TEXT

# 历史 open v1 冻结字面量（仅用于 0018 downgrade 回滚旧库；运行时 open 模板常量已退役）
_OPEN_V1_FALLBACK_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "任务：为该内容自由生成最贴切的语义标签（不受任何既有标签体系限制）。对每个标签输出：",
    "- tag_name：简洁的标签名（2-8 字）",
    "- tag_type：标签分类（如 genre/mood/theme/narrative/style/character 或自拟）",
    "- description：一句话语义定义",
    "- confidence：你对该标签的置信度，0 到 1",
    "- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description",
    "",
    "只输出 JSON 数组，不要输出其他文字。输出 5 到 15 个标签。",
])


# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Lightweight table shape for data DML (alembic recommendation for seed rows).
_prompt_template = sa.table(
    'prompt_template',
    sa.column('mode', sa.Text),
    sa.column('version', sa.Integer),
    sa.column('template_text', sa.Text),
    sa.column('is_active', sa.Boolean),
    sa.column('created_at', sa.DateTime),
)

# v1 种子创作日期：0002 原始播种口径（回填行与首次落库时间戳一致）
_V1_SEED_CREATED_AT = datetime(2026, 10, 31)

# downgrade 回填行：closed/open v1 激活各一条（文案 = templates.py 当前
# 常量；0016 时点的 closed 文案即 0016 播种版，二者逐字一致——
# test_prompts_local 以迁移冻结字面量为基准钉死）
_DOWNGRADE_SEED = (
    {
        "mode": "closed",
        "version": 1,
        "template_text": CLOSED_TEMPLATE_TEXT,
        "is_active": True,
        "created_at": _V1_SEED_CREATED_AT,
    },
    {
        "mode": "open",
        "version": 1,
        "template_text": _OPEN_V1_FALLBACK_TEXT,
        "is_active": True,
        "created_at": _V1_SEED_CREATED_AT,
    },
)


def upgrade() -> None:
    """Drop schema: prompt_template 表退役（文案已本地化为代码常量）。"""
    op.drop_table('prompt_template')


def downgrade() -> None:
    """Downgrade schema: 重建表（列契约 = 被删的 PromptTemplate ORM 逐字）
    并从 templates.py 常量重新播种 v1 激活行。"""
    # 列契约抄自 PromptTemplate ORM 定义（0002 建表 DDL 同源：id 自增主键、
    # (mode, version) 唯一、is_active server_default='0'）
    op.create_table('prompt_template',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('template_text', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mode', 'version')
    )
    op.bulk_insert(_prompt_template, [dict(row) for row in _DOWNGRADE_SEED])
