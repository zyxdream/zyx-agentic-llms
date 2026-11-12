"""add Case Assistant task analytics

Revision ID: a1b2c3d4e5f6
Revises: d4e5f6a7b8c9
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "taskAnalytics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("createdAt", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updatedAt", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("userId", sa.Integer(), nullable=False),
        sa.Column("draftId", sa.Integer(), nullable=False),
        sa.Column("promptsSent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("answersReturned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("llmCallCount", sa.Integer(), server_default="0", nullable=False),
        sa.Column("totalTokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("hallucinationGuardAttempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hallucinationGuardSuccesses", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hallucinationGuardFailures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hallucinationGuardLlmCallCount", sa.Integer(), server_default="0", nullable=False),
        sa.Column("hallucinationGuardTokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("downloadAttempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("downloadSuccesses", sa.Integer(), server_default="0", nullable=False),
        sa.Column("downloadFailures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("downloadLlmCallCount", sa.Integer(), server_default="0", nullable=False),
        sa.Column("downloadTokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draftId", name="uq_task_analytics_draft_id"),
    )
    op.create_index("ix_task_analytics_user_id", "taskAnalytics", ["userId"], unique=False)
    op.create_index("ix_task_analytics_updated_at", "taskAnalytics", ["updatedAt"], unique=False)

    # Historical usage cannot be reconstructed. Seed current Case Assistant tasks at zero.
    op.execute(
        sa.text(
            """
            INSERT INTO taskAnalytics (createdAt, updatedAt, userId, draftId)
            SELECT CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, projects.userId, drafts.id
            FROM drafts
            JOIN projects ON projects.id = drafts.projectId
            WHERE drafts.docCategory = 69
            """
        )
    )


def downgrade():
    op.drop_index("ix_task_analytics_updated_at", table_name="taskAnalytics")
    op.drop_index("ix_task_analytics_user_id", table_name="taskAnalytics")
    op.drop_table("taskAnalytics")
