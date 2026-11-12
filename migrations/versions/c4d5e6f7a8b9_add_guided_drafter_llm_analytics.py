"""add Guided Drafter main-step LLM analytics

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "stepAnalytics",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "stepAnalytics",
        sa.Column("completedAt", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "stepAnalytics",
        sa.Column("celeryTaskId", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "stepAnalytics",
        sa.Column("llmCallCount", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "stepAnalytics",
        sa.Column("llmSuccessCount", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "stepAnalytics",
        sa.Column("llmFailureCount", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "stepAnalytics",
        sa.Column("totalTokens", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_stepAnalytics_celeryTaskId",
        "stepAnalytics",
        ["celeryTaskId"],
        unique=False,
    )
    op.create_index(
        "ix_step_analytics_draft_doc_step",
        "stepAnalytics",
        ["draftId", "docType", "step", "id"],
        unique=False,
    )

    op.create_table(
        "stepLlmModelUsage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stepAnalyticsId", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("llmCallCount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llmSuccessCount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llmFailureCount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("totalTokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["stepAnalyticsId"],
            ["stepAnalytics.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stepAnalyticsId",
            "model",
            name="uq_step_llm_model_usage_step_model",
        ),
    )
    op.create_index(
        "ix_step_llm_model_usage_step_id",
        "stepLlmModelUsage",
        ["stepAnalyticsId"],
        unique=False,
    )
    op.create_index(
        "ix_step_llm_model_usage_model",
        "stepLlmModelUsage",
        ["model"],
        unique=False,
    )

    # These codes represented auxiliary refinements in the previous design.
    # Usage is now attributed to the documented outer/main step instead.
    step_analytics = sa.table(
        "stepAnalytics",
        sa.column("docType", sa.String()),
        sa.column("step", sa.Integer()),
    )
    documented_auxiliary_rows = sa.or_(
        sa.and_(step_analytics.c.docType == "complaint", step_analytics.c.step == 100),
        sa.and_(step_analytics.c.docType == "Complaint Analyzer", step_analytics.c.step == 100),
        sa.and_(step_analytics.c.docType == "motion to dismiss", step_analytics.c.step.in_([100, 101])),
        sa.and_(step_analytics.c.docType == "RFA", step_analytics.c.step == 100),
        sa.and_(step_analytics.c.docType == "RFP", step_analytics.c.step == 100),
        sa.and_(step_analytics.c.docType == "ROG", step_analytics.c.step == 100),
    )
    op.get_bind().execute(sa.delete(step_analytics).where(documented_auxiliary_rows))


def downgrade():
    op.drop_index("ix_step_llm_model_usage_model", table_name="stepLlmModelUsage")
    op.drop_index("ix_step_llm_model_usage_step_id", table_name="stepLlmModelUsage")
    op.drop_table("stepLlmModelUsage")
    op.drop_index("ix_step_analytics_draft_doc_step", table_name="stepAnalytics")
    op.drop_index("ix_stepAnalytics_celeryTaskId", table_name="stepAnalytics")
    op.drop_column("stepAnalytics", "totalTokens")
    op.drop_column("stepAnalytics", "llmFailureCount")
    op.drop_column("stepAnalytics", "llmSuccessCount")
    op.drop_column("stepAnalytics", "llmCallCount")
    op.drop_column("stepAnalytics", "celeryTaskId")
    op.drop_column("stepAnalytics", "completedAt")
    op.drop_column("stepAnalytics", "status")
