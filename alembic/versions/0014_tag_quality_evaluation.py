"""Persist production batches and tag quality evaluations."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

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


def upgrade() -> None:
    if (not op.get_context().as_sql
            and sa.inspect(op.get_bind()).has_table("tag_quality_evaluation")):
        # Pilot databases already have the quality schema under revision 0013.
        # Replay main's closed prompt update before adopting the pilot data.
        prompts = sa.table("prompt_template", sa.column("mode", sa.Text()),
                           sa.column("version", sa.Integer()),
                           sa.column("template_text", sa.Text()))
        op.execute(prompts.update()
                   .where(prompts.c.mode == "closed", prompts.c.version == 1)
                   .values(template_text=_PROMPT_V1_CLOSED_TEXT))
        return

    op.add_column("tag_generation_task", sa.Column("batch_id", sa.Text()))
    op.add_column("tag_generation_task", sa.Column("quality_model", sa.Text()))
    op.create_index("idx_gen_task_batch", "tag_generation_task", ["batch_id"])
    op.create_table(
        "tag_quality_evaluation",
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tag_generation_task.task_id"),
                  primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("content_json", sa.Text()),
        sa.Column("frame_paths", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'partial', 'failed', 'not_needed')",
            name="ck_tag_quality_status",
        ),
    )
    op.create_index("idx_tag_quality_status", "tag_quality_evaluation", ["status"])


def downgrade() -> None:
    op.drop_table("tag_quality_evaluation")
    op.drop_index("idx_gen_task_batch", table_name="tag_generation_task")
    op.drop_column("tag_generation_task", "quality_model")
    op.drop_column("tag_generation_task", "batch_id")
