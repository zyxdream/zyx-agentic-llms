"""add metadata_json to uploaded_files

Revision ID: e1f2a3b4c5d6
Revises: bd44fc8e7115
Create Date: 2026-06-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = 'e1f2a3b4c5d6'
down_revision = 'bd44fc8e7115'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('uploaded_files', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'metadata_json',
            sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('uploaded_files', schema=None) as batch_op:
        batch_op.drop_column('metadata_json')
