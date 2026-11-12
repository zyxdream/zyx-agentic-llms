"""Separate current workspace drafts from durable conversation turns."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = 'b9c0d1e2f3a4'
down_revision = 'a8b9c0d1e2f3'
branch_labels = None
depends_on = None


def upgrade():
    text = sa.Text().with_variant(LONGTEXT(), 'mysql')
    op.create_table('workspace_draft_states',
        sa.Column('draft_id', sa.Integer(), sa.ForeignKey('drafts.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('draft_json', text, nullable=False),
        sa.Column('draft_before_json', text),
        sa.Column('draft_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('next_turn_position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('legacy_snapshot_json', text), sa.Column('migration_report_json', text),
        sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_table('conversation_turns',
        sa.Column('id', sa.String(80), primary_key=True),
        sa.Column('draft_id', sa.Integer(), sa.ForeignKey('drafts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('workflow_run_id', sa.String(80), sa.ForeignKey('case_assistant_workflows.id', ondelete='SET NULL')),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('user_text', text, nullable=False), sa.Column('assistant_text', text, nullable=False),
        sa.Column('metadata_json', text, nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('draft_id', 'position', name='uq_conversation_turn_position'),
        sa.UniqueConstraint('workflow_run_id', name='uq_conversation_turn_workflow'))
    op.create_index('ix_conversation_turns_draft_id', 'conversation_turns', ['draft_id'])


def downgrade():
    op.drop_table('conversation_turns')
    op.drop_table('workspace_draft_states')
