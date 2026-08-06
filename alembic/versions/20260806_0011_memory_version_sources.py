"""Persist complete provenance sets for immutable memory versions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260806_0011"
down_revision = "20260802_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("memory_version_sources"):
        return
    op.create_table(
        "memory_version_sources",
        sa.Column("version_id", sa.String(length=255), sa.ForeignKey("memory_versions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("note_id", sa.String(length=255), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Existing provenance is intentionally left untouched.  Readers fall
    # back to the legacy ``source_note_id`` for historical versions; only new
    # writes receive a complete, immutable source set.


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("memory_version_sources"):
        op.drop_table("memory_version_sources")
