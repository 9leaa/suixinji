"""Preference workflow state and unique card identity; existing memories unchanged."""
from alembic import op
from sqlalchemy import text
from memory.workflow_state import metadata
revision = '20261018_0016'
down_revision = '20261018_0015'
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())
    op.create_index('ix_preference_card_identity', 'memories', ['space_id', 'memory_key'], unique=True,
                    postgresql_where=text("memory_key_version = 'preference-card-v1'"), if_not_exists=True)


def downgrade():
    op.drop_index('ix_preference_card_identity', table_name='memories')
    metadata.drop_all(op.get_bind())
