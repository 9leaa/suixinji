"""Add console accounts and revocable sessions; leave existing tenant data untouched."""
from alembic import op
from repositories.postgres.console_auth import metadata

revision = '20261025_0017'
down_revision = '20261018_0016'
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())
