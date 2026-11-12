"""Remove the unused external identity field; keep existing local accounts intact."""
from alembic import op
import sqlalchemy as sa

revision = '0003_local_accounts_only'
down_revision = '0002_local_auth'
branch_labels = None
depends_on = None


def upgrade():
    # A forward migration preserves the already-created local account and its ID/hash.
    with op.batch_alter_table('user') as batch:
        batch.drop_column('cognito_username')


def downgrade():
    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('cognito_username', sa.String(500), nullable=True))
