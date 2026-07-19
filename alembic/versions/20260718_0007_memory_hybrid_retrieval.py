"""Add memory hybrid retrieval metadata and indexes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260718_0007"
down_revision = "20260718_0006"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_column(inspector: sa.Inspector, table: str, column: sa.Column) -> None:
    if column.name not in _columns(inspector, table):
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("memories"):
        _add_column(
            inspector,
            "memories",
            sa.Column("memory_key_version", sa.String(64), nullable=False, server_default="memory-key-v2"),
        )
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_memories_space_type_status_key
            ON memories (space_id, memory_type, status, memory_key)
            """
        )
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_memories_search_document
            ON memories USING gin (
                to_tsvector(
                    'simple',
                    coalesce(content, '') || ' ' ||
                    coalesce(subject, '') || ' ' ||
                    coalesce(predicate, '') || ' ' ||
                    coalesce(object_value, '')
                )
            )
            """
        )

    inspector = sa.inspect(bind)
    if inspector.has_table("memory_candidates"):
        _add_column(
            inspector,
            "memory_candidates",
            sa.Column("memory_key_version", sa.String(64), nullable=False, server_default="memory-key-v2"),
        )

    inspector = sa.inspect(bind)
    if inspector.has_table("memory_vectors"):
        _add_column(inspector, "memory_vectors", sa.Column("dimension", sa.Integer()))
        _add_column(inspector, "memory_vectors", sa.Column("content_hash", sa.String(128)))
        _add_column(inspector, "memory_vectors", sa.Column("embedding_version", sa.String(128)))
        _add_column(inspector, "memory_vectors", sa.Column("status", sa.String(32), nullable=False, server_default="ready"))
        _add_column(inspector, "memory_vectors", sa.Column("last_error", sa.Text()))
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_memory_vectors_ready_embedding
            ON memory_vectors USING hnsw (embedding vector_cosine_ops)
            WHERE status = 'ready' AND embedding IS NOT NULL
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("memory_vectors"):
        op.execute("DROP INDEX IF EXISTS ix_memory_vectors_ready_embedding")
        for column in ("last_error", "status", "embedding_version", "content_hash", "dimension"):
            inspector = sa.inspect(bind)
            if column in _columns(inspector, "memory_vectors"):
                op.drop_column("memory_vectors", column)

    if inspector.has_table("memory_candidates") and "memory_key_version" in _columns(inspector, "memory_candidates"):
        op.drop_column("memory_candidates", "memory_key_version")

    inspector = sa.inspect(bind)
    if inspector.has_table("memories"):
        op.execute("DROP INDEX IF EXISTS ix_memories_search_document")
        op.execute("DROP INDEX IF EXISTS ix_memories_space_type_status_key")
        if "memory_key_version" in _columns(inspector, "memories"):
            op.drop_column("memories", "memory_key_version")
