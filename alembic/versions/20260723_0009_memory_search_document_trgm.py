"""Add generated Memory search document and Chinese-friendly trigram indexes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260723_0009"
down_revision = "20260723_0008"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("memories"):
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("DROP INDEX IF EXISTS ix_memories_search_document")
    if "search_document" not in _columns(inspector, "memories"):
        op.add_column(
            "memories",
            sa.Column(
                "search_document",
                postgresql.TSVECTOR(),
                sa.Computed(
                    "to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(subject, '') || ' ' || coalesce(predicate, '') || ' ' || coalesce(object_value, ''))",
                    persisted=True,
                ),
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_memories_search_document ON memories USING gin (search_document)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_memories_content_trgm ON memories USING gin (content gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_memories_object_value_trgm ON memories USING gin (object_value gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memories_object_value_trgm")
    op.execute("DROP INDEX IF EXISTS ix_memories_content_trgm")
    op.execute("DROP INDEX IF EXISTS ix_memories_search_document")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("memories") and "search_document" in _columns(inspector, "memories"):
        op.drop_column("memories", "search_document")
