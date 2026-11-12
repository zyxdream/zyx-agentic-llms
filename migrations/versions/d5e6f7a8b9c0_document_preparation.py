"""Durable Case Assistant document preparation.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'd5e6f7a8b9c0'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('document_upload_groups',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('draft_id', sa.Integer(), nullable=True),
        sa.Column('scope_key', sa.String(length=64), nullable=False),
        sa.Column('request_key', sa.String(length=100), nullable=False),
        sa.Column('manifest_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['draft_id'], ['drafts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'request_key', name='uq_upload_group_request')
    )
    op.create_index(op.f('ix_document_upload_groups_scope_key'), 'document_upload_groups', ['scope_key'], unique=False)
    op.create_table('document_preparations',
        sa.Column('id', sa.String(length=100), nullable=False),
        sa.Column('group_id', sa.String(length=36), nullable=False),
        sa.Column('scope_key', sa.String(length=64), nullable=False),
        sa.Column('client_upload_id', sa.String(length=100), nullable=False),
        sa.Column('filename', sa.String(length=500), nullable=False),
        sa.Column('relative_path', sa.String(length=1000), nullable=True),
        sa.Column('folder_upload_id', sa.Integer(), nullable=True),
        sa.Column('file_size', sa.BigInteger(), nullable=False),
        sa.Column('uploaded_file_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('checkpoint', sa.String(length=20), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('source_key', sa.String(length=1000), nullable=True),
        sa.Column('source_hash', sa.String(length=64), nullable=True),
        sa.Column('lease_token', sa.String(length=36), nullable=True),
        sa.Column('lease_until', sa.DateTime(), nullable=True),
        sa.Column('task_id', sa.String(length=100), nullable=True),
        sa.Column('dispatch_pending', sa.Boolean(), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(), nullable=True),
        sa.Column('error_code', sa.String(length=80), nullable=True),
        sa.Column('error_message', sa.String(length=1000), nullable=True),
        sa.Column('warning', sa.String(length=1000), nullable=True),
        sa.Column('parsed_at', sa.DateTime(), nullable=True),
        sa.Column('indexed_at', sa.DateTime(), nullable=True),
        sa.Column('ready_at', sa.DateTime(), nullable=True),
        sa.Column('first_attached_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('coverage_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=True),
        sa.ForeignKeyConstraint(['folder_upload_id'], ['folder_uploads.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['group_id'], ['document_upload_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_file_id'], ['uploaded_files.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scope_key', 'client_upload_id', name='uq_preparation_client'),
        sa.UniqueConstraint('uploaded_file_id')
    )
    op.create_index(op.f('ix_document_preparations_scope_key'), 'document_preparations', ['scope_key'], unique=False)
    op.create_index('ix_preparation_dispatch', 'document_preparations', ['dispatch_pending', 'lease_until'], unique=False)
    op.create_table('document_summary_segments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.String(length=100), nullable=False),
        sa.Column('segment_key', sa.String(length=64), nullable=False),
        sa.Column('result_json', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['document_preparations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'segment_key', name='uq_summary_segment')
    )


def downgrade():
    op.drop_table('document_summary_segments')
    op.drop_index('ix_preparation_dispatch', table_name='document_preparations')
    op.drop_index(op.f('ix_document_preparations_scope_key'), table_name='document_preparations')
    op.drop_table('document_preparations')
    op.drop_index(op.f('ix_document_upload_groups_scope_key'), table_name='document_upload_groups')
    op.drop_table('document_upload_groups')
