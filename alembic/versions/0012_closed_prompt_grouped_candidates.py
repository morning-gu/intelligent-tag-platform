"""closed v1 prompt: candidates grouped as JSON in producer

Revision ID: 0012
Revises: 0011
Create Date: 2026-12-14 10:00:00.000000

closed 模式自 2026-12 起候选标签不再经 `{candidates}` 占位符注入
（runner 不再组装候选文本），改由 ClosedTagProducer 按目录分组、以 JSON
追加在渲染后的正文末尾。因此 closed v1 模板重写：只保留 `{content}`
占位符，正文说明候选分组 JSON 的注入位置与输出契约不变（五键 JSON 数组）。

对既有库做 UPDATE 而非新插入 v2 行：保持 mode+version=(closed,1) 的
身份不变，任务表引用的 prompt_version="1" 不漂移；create_all 路径
（测试 fixture）由 after_create 钩子用同一常量播种，两条路径不会漂移。
downgrade 把旧文案（{candidates} 版）字面还原。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
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

# upgrade 用新文案：本迁移时点 models.py 中 `_PROMPT_V1_CLOSED_TEXT` 的逐字
# 快照。迁移文件是历史快照，必须自包含——不随 models.py 演进（或删除）而失效。
_PROMPT_V1_CLOSED_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签按目录分组，以 JSON 格式附在上述正文之后。每组包含所属目录的",
    "名称与描述，组内标签给出 tag_id、tag_name 与 description。",
    "",
    "任务：从候选标签中选出适用于内容的标签。Applicability 和各模态的",
    "Evidence Strength 是独立维度，必须分别评分，不能互相替代。",
    "每个选中标签输出以下字段：",
    "- tag_id（必须与候选标签一致，不得编造）",
    "- applicability：整数 1 到 5。根据标签定义、使用条件和影视内容，判断标签是否适用，",
    "  以及是否构成作品的重要特征。1=完全不适用；2=存在弱关联但不满足主要适用条件；",
    "  3=部分符合标签定义但非主要特征；4=高度符合标签定义且具有明显内容特征；",
    "  5=完全符合标签定义且是作品核心特征。",
    "- evidence：对象，必须同时包含 content、scene、frame 三个模态节，",
    "  分别基于三种输入独立评分与举证：",
    "  - content 模态：基于剧情整体描述（摘要）判断；",
    "  - scene 模态：基于各场景的场景描述判断；",
    "  - frame 模态：基于随消息附上的关键帧图片判断（若无图片则基于正文推断）。",
    "  每个模态节包含两个字段：",
    "  - evidence_strength：整数 1 到 5，该模态下证据的直接性、明确程度和相互支撑程度。",
    "    1=缺乏有效证据，主要依赖推测；2=证据间接、模糊或可信度较低；",
    "    3=存在一定证据但覆盖不足；4=存在多个明确证据支持判断；",
    "    5=证据直接、充分且能够相互印证。",
    "  - evidences：该模态的证据摘要数组，每条包含 scene_id 和 evidence；",
    "    scene_id 必须来自正文已有场景，evidence 用简洁文字总结该场景中",
    "    该模态下的实际证据，不得编造或仅凭推测。",
    "",
    "只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。",
])

# 0002 版 closed v1 的旧文案（{candidates} 占位符版）。downgrade 还原用；
# 保留字面量而非 import 常量——旧文案在 models.py 中已被替换，历史版本
# 必须自包含（迁移文件是历史快照，不随代码演进）。
_OLD_CLOSED_V1_TEXT = "\n".join([
    "你是影视内容标签专家。以下是一部影视内容的结构化描述：",
    "",
    "{content}",
    "",
    "候选标签清单（格式：tag_id | 名称 | 分类 | 定义）：",
    "{candidates}",
    "",
    "任务：从候选清单中选出与内容真正相关的标签。对每个选中标签输出：",
    "- tag_id（必须与候选清单一致，不得编造）",
    "- tag_name（与候选一致）",
    "- relevance：标签与内容的相关程度，0 到 1",
    "- confidence：你对该判断的置信度，0 到 1",
    "- evidence：支持该标签的内容证据列表，每条含 scene_id、start_ms、end_ms、description",
    "",
    "只输出 JSON 数组，不要输出其他文字。未选中的标签不要输出。",
])


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        _prompt_template.update()
        .where(sa.and_(_prompt_template.c.mode == 'closed',
                       _prompt_template.c.version == 1))
        .values(template_text=_PROMPT_V1_CLOSED_TEXT))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        _prompt_template.update()
        .where(sa.and_(_prompt_template.c.mode == 'closed',
                       _prompt_template.c.version == 1))
        .values(template_text=_OLD_CLOSED_V1_TEXT))
