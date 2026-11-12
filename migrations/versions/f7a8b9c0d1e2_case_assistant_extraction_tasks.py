"""Durable Case Assistant submitted prompts and section task progress."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = 'f7a8b9c0d1e2'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade():
    text = sa.Text().with_variant(LONGTEXT(), 'mysql')
    op.create_table('case_assistant_workflows',
        sa.Column('id', sa.String(80), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('draft_id', sa.Integer(), sa.ForeignKey('drafts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('active_draft_id', sa.Integer(), unique=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('phase', sa.String(20), nullable=False),
        sa.Column('data_json', text, nullable=False),
        sa.Column('intent_json', text), sa.Column('payload_json', text), sa.Column('result_json', text),
        sa.Column('planning_task_id', sa.String(100)), sa.Column('answer_task_id', sa.String(100)),
        sa.Column('group_id', sa.String(100)), sa.Column('dispatch_pending', sa.Boolean(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(36)), sa.Column('lease_until', sa.DateTime()),
        sa.Column('draft_hash', sa.String(64), nullable=False),
        sa.Column('evidence_budget', sa.Integer(), nullable=False),
        sa.Column('reduction_round', sa.Integer(), nullable=False),
        sa.Column('last_progress_at', sa.DateTime(), nullable=False),
        sa.Column('error_message', sa.String(1000)),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deadline', sa.DateTime(), nullable=False))
    op.create_index('ix_case_assistant_workflows_draft_id', 'case_assistant_workflows', ['draft_id'])
    op.create_index('ix_case_assistant_workflows_updated_at', 'case_assistant_workflows', ['updated_at'])
    op.create_table('case_assistant_work',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('workflow_run_id', sa.String(80), sa.ForeignKey('case_assistant_workflows.id', ondelete='CASCADE'), nullable=False),
        sa.Column('work_key', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('document_id', sa.String(100)), sa.Column('artifact_id', sa.String(64)),
        sa.Column('material_json', text, nullable=False),
        sa.Column('result_id', sa.Integer(), sa.ForeignKey('document_summary_segments.id', ondelete='SET NULL')),
        sa.Column('state', sa.String(20), nullable=False), sa.Column('task_id', sa.String(100)),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(36)), sa.Column('lease_until', sa.DateTime()),
        sa.Column('error_message', sa.String(1000)), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('workflow_run_id', 'work_key', name='uq_case_assistant_work'))
    op.create_index('ix_case_assistant_work_workflow_run_id', 'case_assistant_work', ['workflow_run_id'])


def downgrade():
    op.drop_table('case_assistant_work')
    op.drop_table('case_assistant_workflows')
