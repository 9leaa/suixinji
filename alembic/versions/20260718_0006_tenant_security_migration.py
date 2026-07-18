"""Harden tenant isolation, API-era idempotency, and timestamp types."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260718_0006"
down_revision = "20260718_0005"
branch_labels = None
depends_on = None


TIME_COLUMNS = {
    "memories": [
        "valid_from",
        "valid_until",
        "last_confirmed_at",
        "created_at",
        "updated_at",
        "last_accessed_at",
    ],
    "memory_sources": ["created_at"],
    "memory_versions": ["valid_from", "valid_until", "created_at"],
    "memory_candidates": ["valid_from", "valid_until"],
    "memory_vectors": ["created_at", "updated_at"],
    "memory_extraction_states": ["started_at", "completed_at", "updated_at"],
    "memory_consolidation_runs": ["started_at", "completed_at"],
    "memory_decisions": ["created_at", "applied_at"],
    "memory_relations": ["created_at"],
    "memory_traces": ["started_at", "finished_at"],
    "deliveries": ["created_at", "updated_at", "reserved_at", "lease_expires_at"],
    "delivery_attempts": ["started_at", "finished_at"],
}


def _constraints(inspector: sa.Inspector, table: str) -> set[str]:
    if not inspector.has_table(table):
        return set()
    return {item["name"] for item in inspector.get_unique_constraints(table)}


def _indexes(inspector: sa.Inspector, table: str) -> set[str]:
    if not inspector.has_table(table):
        return set()
    return {item["name"] for item in inspector.get_indexes(table)}


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    if not inspector.has_table(table):
        return set()
    return {item["name"] for item in inspector.get_columns(table)}


def _alter_to_timestamptz(inspector: sa.Inspector, table: str, column: str) -> None:
    if column not in _columns(inspector, table):
        return
    op.execute(
        f"""
        ALTER TABLE {table}
        ALTER COLUMN {column}
        TYPE TIMESTAMP WITH TIME ZONE
        USING NULLIF({column}::text, '')::timestamp with time zone
        """
    )


def _alter_to_text(inspector: sa.Inspector, table: str, column: str) -> None:
    if column not in _columns(inspector, table):
        return
    op.execute(
        f"""
        ALTER TABLE {table}
        ALTER COLUMN {column}
        TYPE VARCHAR(64)
        USING {column}::text
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("inbox_messages"):
        constraints = _constraints(inspector, "inbox_messages")
        if "uq_inbox_source_message" in constraints:
            op.drop_constraint("uq_inbox_source_message", "inbox_messages", type_="unique")
        constraints = _constraints(sa.inspect(bind), "inbox_messages")
        if "uq_inbox_tenant_source_message" not in constraints:
            op.create_unique_constraint(
                "uq_inbox_tenant_source_message",
                "inbox_messages",
                ["tenant_id", "source", "source_message_id"],
            )
        indexes = _indexes(sa.inspect(bind), "inbox_messages")
        if "ix_inbox_tenant_source_message" not in indexes:
            op.create_index(
                "ix_inbox_tenant_source_message",
                "inbox_messages",
                ["tenant_id", "source", "source_message_id"],
            )

    if inspector.has_table("tasks"):
        indexes = _indexes(inspector, "tasks")
        if "ix_tasks_tenant_space_status_created" not in indexes:
            op.create_index(
                "ix_tasks_tenant_space_status_created",
                "tasks",
                ["tenant_id", "space_id", "status", "created_at"],
            )

    for table, columns in TIME_COLUMNS.items():
        inspector = sa.inspect(bind)
        if not inspector.has_table(table):
            continue
        for column in columns:
            _alter_to_timestamptz(inspector, table, column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, columns in reversed(TIME_COLUMNS.items()):
        inspector = sa.inspect(bind)
        if not inspector.has_table(table):
            continue
        for column in columns:
            _alter_to_text(inspector, table, column)

    inspector = sa.inspect(bind)
    if inspector.has_table("tasks") and "ix_tasks_tenant_space_status_created" in _indexes(inspector, "tasks"):
        op.drop_index("ix_tasks_tenant_space_status_created", table_name="tasks")

    inspector = sa.inspect(bind)
    if inspector.has_table("inbox_messages"):
        if "ix_inbox_tenant_source_message" in _indexes(inspector, "inbox_messages"):
            op.drop_index("ix_inbox_tenant_source_message", table_name="inbox_messages")
        constraints = _constraints(inspector, "inbox_messages")
        if "uq_inbox_tenant_source_message" in constraints:
            op.drop_constraint("uq_inbox_tenant_source_message", "inbox_messages", type_="unique")
        constraints = _constraints(sa.inspect(bind), "inbox_messages")
        if "uq_inbox_source_message" not in constraints:
            op.create_unique_constraint("uq_inbox_source_message", "inbox_messages", ["source", "source_message_id"])
