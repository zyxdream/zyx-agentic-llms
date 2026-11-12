"""Versioned tool controller, operation history and candidate artifacts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = 'a8b9c0d1e2f3'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    text = sa.Text().with_variant(LONGTEXT(), 'mysql')
    op.add_column('case_assistant_workflows', sa.Column('execution_version', sa.Integer(), nullable=False, server_default='0'))
    op.create_table('case_assistant_agent_steps',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('workflow_run_id', sa.String(80), sa.ForeignKey('case_assistant_workflows.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False), sa.Column('name', sa.String(80), nullable=False),
        sa.Column('call_id', sa.String(200)), sa.Column('arguments_json', text, nullable=False),
        sa.Column('result_json', text), sa.Column('summary_json', text), sa.Column('state', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('workflow_run_id', 'sequence', name='uq_agent_step_sequence'))
    op.create_index('ix_case_assistant_agent_steps_workflow_run_id', 'case_assistant_agent_steps', ['workflow_run_id'])
    op.create_table('case_assistant_agent_artifacts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('workflow_run_id', sa.String(80), sa.ForeignKey('case_assistant_workflows.id', ondelete='CASCADE'), nullable=False),
        sa.Column('step_id', sa.String(36), sa.ForeignKey('case_assistant_agent_steps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False), sa.Column('content_json', text, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_case_assistant_agent_artifacts_workflow_run_id', 'case_assistant_agent_artifacts', ['workflow_run_id'])


def downgrade():
    op.drop_table('case_assistant_agent_artifacts')
    op.drop_table('case_assistant_agent_steps')
    op.drop_column('case_assistant_workflows', 'execution_version')
