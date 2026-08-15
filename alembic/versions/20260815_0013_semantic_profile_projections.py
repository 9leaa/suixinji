"""Add rebuildable semantic facet profile projections."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260815_0013"
down_revision = "20260810_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("semantic_profile_projections"):
        return
    op.create_table(
        "semantic_profile_projections",
        sa.Column("space_id", sa.String(length=255), sa.ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("facet", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("projection_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_memory_ids_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("processed_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("target_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="dirty"),
        sa.Column("dirty_since", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_semantic_profile_projection_dirty", "semantic_profile_projections", ["status", "dirty_since"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("semantic_profile_projections"):
        op.drop_index("ix_semantic_profile_projection_dirty", table_name="semantic_profile_projections")
        op.drop_table("semantic_profile_projections")
