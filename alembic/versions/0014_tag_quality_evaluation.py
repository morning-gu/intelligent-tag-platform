"""Persist production batches and tag quality evaluations."""

import sqlalchemy as sa
from alembic import op

from tagplatform.db.models import _PROMPT_V1_CLOSED_TEXT

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


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
