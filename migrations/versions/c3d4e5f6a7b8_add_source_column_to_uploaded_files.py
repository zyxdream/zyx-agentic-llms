"""Add source column to uploaded_files

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-18 18:24:01.412243

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = '8fd6454468a7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('uploaded_files', sa.Column('source', sa.String(20), nullable=True))
    # Tag all existing matter-level files (draft_id IS NULL) as 'everything'
    op.execute("UPDATE uploaded_files SET source = 'everything' WHERE draft_id IS NULL")


def downgrade():
    op.drop_column('uploaded_files', 'source')
