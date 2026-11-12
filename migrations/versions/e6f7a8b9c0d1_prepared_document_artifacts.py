"""Versioned document artifacts and resumable summary work.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    text = sa.Text().with_variant(mysql.LONGTEXT(), 'mysql')
    op.add_column('document_preparations', sa.Column('parse_manifest_json', text))
    op.add_column('document_preparations', sa.Column('artifact_id', sa.String(64)))
    op.add_column('document_preparations', sa.Column('coverage_summary_json', text))
    op.create_table('document_prepared_artifacts',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('document_id', sa.String(100), sa.ForeignKey('document_preparations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('preparation_version', sa.String(80), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('result_json', text, nullable=False),
        sa.Column('card_json', text, nullable=False),
        sa.Column('manifest_json', text, nullable=False),
        sa.Column('coverage_json', text, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_document_prepared_artifacts_document_id', 'document_prepared_artifacts', ['document_id'])
    for column in (
        sa.Column('kind', sa.String(20), nullable=False, server_default='legacy'),
        sa.Column('state', sa.String(20), nullable=False, server_default='ready'),
        sa.Column('generation', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('lease_token', sa.String(36)), sa.Column('lease_until', sa.DateTime()),
        sa.Column('search_text', text),
    ):
        op.add_column('document_summary_segments', column)
    op.create_index('ix_prepared_section_search', 'document_summary_segments', ['search_text'], mysql_prefix='FULLTEXT')


def downgrade():
    op.drop_index('ix_prepared_section_search', table_name='document_summary_segments')
    for name in ('search_text', 'lease_until', 'lease_token', 'attempts', 'generation', 'state', 'kind'):
        op.drop_column('document_summary_segments', name)
    op.drop_table('document_prepared_artifacts')
    op.drop_column('document_preparations', 'artifact_id')
    op.drop_column('document_preparations', 'coverage_summary_json')
    op.drop_column('document_preparations', 'parse_manifest_json')
