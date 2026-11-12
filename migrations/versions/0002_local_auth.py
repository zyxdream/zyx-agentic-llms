"""Project-local authentication, independent of any external user pool."""
from alembic import op
import sqlalchemy as sa
revision='0002_local_auth'
down_revision='0001_workspace'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('user',sa.Column('password_hash',sa.String(512),nullable=True))
    op.create_table('local_auth_bootstrap',sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('user.id'),nullable=False,unique=True),
        sa.CheckConstraint('id = 1',name='ck_local_auth_singleton'))

def downgrade():
    op.drop_table('local_auth_bootstrap')
    with op.batch_alter_table('user') as batch:batch.drop_column('password_hash')
