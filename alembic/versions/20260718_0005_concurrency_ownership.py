"""Add task/outbox fencing and dual-watermark state."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260718_0005"
down_revision = "20260718_0004"
branch_labels = None
depends_on = None


def _add_columns(inspector: sa.Inspector, table: str, columns: dict[str, sa.Column]) -> None:
    if not inspector.has_table(table):
        return
    existing = {item["name"] for item in inspector.get_columns(table)}
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table, column)


def _create_index(inspector: sa.Inspector, name: str, table: str, columns: list[str]) -> None:
    if inspector.has_table(table) and name not in {item["name"] for item in inspector.get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _add_columns(
        inspector,
        "spaces",
        {"note_watermark": sa.Column("note_watermark", sa.BigInteger(), nullable=False, server_default="0")},
    )
    inspector = sa.inspect(bind)
    _add_columns(
        inspector,
        "inbox_messages",
        {
            "note_status": sa.Column("note_status", sa.String(32), nullable=False, server_default="pending"),
            "memory_status": sa.Column("memory_status", sa.String(32), nullable=False, server_default="pending"),
            "note_completed_at": sa.Column("note_completed_at", sa.DateTime(timezone=True)),
            "memory_completed_at": sa.Column("memory_completed_at", sa.DateTime(timezone=True)),
        },
    )
    inspector = sa.inspect(bind)
    _add_columns(
        inspector,
        "tasks",
        {
            "claimed_by": sa.Column("claimed_by", sa.String(255)),
            "lease_token": sa.Column("lease_token", sa.String(64)),
            "lease_expires_at": sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            "claim_version": sa.Column("claim_version", sa.Integer(), nullable=False, server_default="0"),
        },
    )
    inspector = sa.inspect(bind)
    _add_columns(
        inspector,
        "outbox_events",
        {
            "status": sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            "claimed_by": sa.Column("claimed_by", sa.String(255)),
            "lease_token": sa.Column("lease_token", sa.String(64)),
            "lease_expires_at": sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            "next_attempt_at": sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            "max_attempts": sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="10"),
            "last_attempt_at": sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
            "failed_at": sa.Column("failed_at", sa.DateTime(timezone=True)),
        },
    )

    if inspector.has_table("spaces"):
        op.execute("UPDATE spaces SET note_watermark = GREATEST(note_watermark, processed_sequence_no)")
    if inspector.has_table("inbox_messages"):
        op.execute(
            """
            UPDATE inbox_messages
            SET note_status = CASE WHEN status = 'failed' THEN 'failed' WHEN status = 'processed' THEN 'completed' ELSE note_status END,
                memory_status = CASE WHEN status = 'failed' THEN 'failed' WHEN status = 'processed' THEN 'completed' ELSE memory_status END,
                note_completed_at = CASE WHEN status IN ('processed', 'failed') THEN COALESCE(note_completed_at, received_at) ELSE note_completed_at END,
                memory_completed_at = CASE WHEN status IN ('processed', 'failed') THEN COALESCE(memory_completed_at, received_at) ELSE memory_completed_at END
            """
        )
    if inspector.has_table("outbox_events"):
        op.execute("UPDATE outbox_events SET status = CASE WHEN published_at IS NULL THEN 'pending' ELSE 'published' END")

    inspector = sa.inspect(bind)
    _create_index(inspector, "ix_tasks_status_lease", "tasks", ["status", "lease_expires_at"])
    inspector = sa.inspect(bind)
    _create_index(inspector, "ix_outbox_status_next_created", "outbox_events", ["status", "next_attempt_at", "created_at"])
    inspector = sa.inspect(bind)
    _create_index(inspector, "ix_inbox_space_note_sequence", "inbox_messages", ["space_id", "note_status", "sequence_no"])
    inspector = sa.inspect(bind)
    _create_index(inspector, "ix_inbox_space_memory_sequence", "inbox_messages", ["space_id", "memory_status", "sequence_no"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table in (
        ("ix_inbox_space_memory_sequence", "inbox_messages"),
        ("ix_inbox_space_note_sequence", "inbox_messages"),
        ("ix_outbox_status_next_created", "outbox_events"),
        ("ix_tasks_status_lease", "tasks"),
    ):
        if inspector.has_table(table) and name in {item["name"] for item in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
        inspector = sa.inspect(bind)
    for table, columns in (
        ("outbox_events", ("failed_at", "last_attempt_at", "max_attempts", "next_attempt_at", "lease_expires_at", "lease_token", "claimed_by", "status")),
        ("tasks", ("claim_version", "lease_expires_at", "lease_token", "claimed_by")),
        ("inbox_messages", ("memory_completed_at", "note_completed_at", "memory_status", "note_status")),
        ("spaces", ("note_watermark",)),
    ):
        for column in columns:
            inspector = sa.inspect(bind)
            if inspector.has_table(table) and column in {item["name"] for item in inspector.get_columns(table)}:
                op.drop_column(table, column)
