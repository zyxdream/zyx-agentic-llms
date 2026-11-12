"""Add FreeLaw cluster cache

Revision ID: d4e5f6a7b8c9
Revises: e1f2a3b4c5d6
Create Date: 2026-04-30 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = 'd4e5f6a7b8c9'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'freelaw_cluster_cache',
        sa.Column('cluster_id', sa.Integer(), primary_key=True),
        sa.Column('case_name', sa.String(1000), nullable=True),
        sa.Column('opinion_id', sa.Integer(), nullable=True),
        sa.Column('opinion_url', sa.String(1000), nullable=True),
        sa.Column('docket_url', sa.String(1000), nullable=True),
        sa.Column('court', sa.String(250), nullable=True),
        sa.Column('freelaw_url', sa.String(1000), nullable=True),
        sa.Column('llm_holding', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=True),
        sa.Column('payload_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=True),
        sa.Column('hit_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_accessed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_freelaw_cluster_last_accessed', 'freelaw_cluster_cache', ['last_accessed_at'])
    op.create_index('ix_freelaw_cluster_hit_count', 'freelaw_cluster_cache', ['hit_count'])


def downgrade():
    op.drop_index('ix_freelaw_cluster_hit_count', table_name='freelaw_cluster_cache')
    op.drop_index('ix_freelaw_cluster_last_accessed', table_name='freelaw_cluster_cache')
    op.drop_table('freelaw_cluster_cache')
