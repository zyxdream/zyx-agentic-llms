"""add_metadata_json_to_smart_search_messages

Revision ID: bd44fc8e7115
Revises: 83f62478adf7
Create Date: 2026-04-22 10:18:04.040968

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'bd44fc8e7115'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('smart_search_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('metadata_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=True))


def downgrade():
    with op.batch_alter_table('smart_search_messages', schema=None) as batch_op:
        batch_op.drop_column('metadata_json')
