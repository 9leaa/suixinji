"""Add message console trace storage; preserve all existing business data."""
from alembic import op
from repositories.postgres.lifecycle import metadata
revision = '20261018_0015'
down_revision = '20260815_0014'
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())
