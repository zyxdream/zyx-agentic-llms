"""add smart search sessions and messages

Revision ID: 8fd6454468a7
Revises: f2fa7eaa913c
Create Date: 2026-04-04 16:36:35.445985
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
import uuid

# revision identifiers, used by Alembic.
revision = '8fd6454468a7'
down_revision = 'abf6edea8d32'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Backfill any missing/blank public_id values
    rows = conn.execute(sa.text("""
        SELECT id
        FROM drafts
        WHERE public_id IS NULL OR public_id = ''
    """)).fetchall()

    for row in rows:
        conn.execute(
            sa.text("""
                UPDATE drafts
                SET public_id = :public_id
                WHERE id = :id
            """),
            {
                "public_id": str(uuid.uuid4()),
                "id": row.id,
            }
        )

    # Make public_id non-null and unique
    with op.batch_alter_table('drafts', schema=None) as batch_op:
        batch_op.alter_column(
            'public_id',
            existing_type=sa.String(length=36),
            nullable=False
        )
        batch_op.create_unique_constraint('uq_drafts_public_id', ['public_id'])

    with op.batch_alter_table('uploaded_files', schema=None) as batch_op:
        batch_op.add_column(sa.Column('page_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('folder_upload_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('relative_path', sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column('source', sa.String(length=20), nullable=True))
        batch_op.alter_column(
            'draft_id',
            existing_type=mysql.INTEGER(),
            nullable=True
        )
        batch_op.create_foreign_key(
            'fk_uploaded_files_folder_upload_id',
            'folder_uploads',
            ['folder_upload_id'],
            ['id']
        )
        batch_op.create_foreign_key(
            'fk_uploaded_files_project_id',
            'projects',
            ['project_id'],
            ['id']
        )
        batch_op.drop_column('summary')

def downgrade():
    with op.batch_alter_table('uploaded_files', schema=None) as batch_op:
        batch_op.add_column(sa.Column('summary', mysql.LONGTEXT(), nullable=True))
        batch_op.drop_constraint('fk_uploaded_files_folder_upload_id', type_='foreignkey')
        batch_op.drop_constraint('fk_uploaded_files_project_id', type_='foreignkey')
        batch_op.alter_column(
            'draft_id',
            existing_type=mysql.INTEGER(),
            nullable=False
        )
        batch_op.drop_column('source')
        batch_op.drop_column('relative_path')
        batch_op.drop_column('folder_upload_id')
        batch_op.drop_column('project_id')
        batch_op.drop_column('page_count')

    with op.batch_alter_table('drafts', schema=None) as batch_op:
        batch_op.drop_constraint('uq_drafts_public_id', type_='unique')
        batch_op.drop_column('public_id')
