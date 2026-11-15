"""Retain confirmed editor versions for explicit rollback."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = '0004_published_draft_versions'
down_revision = '0003_local_accounts_only'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('published_draft_versions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('draft_version', sa.Integer(), nullable=False),
        sa.Column('draft_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('candidate_id', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['draft_documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'draft_version', name='uq_published_document_version'))
    op.create_index('ix_published_draft_versions_document_id', 'published_draft_versions', ['document_id'])


def downgrade():
    op.drop_table('published_draft_versions')
