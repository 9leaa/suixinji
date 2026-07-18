"""Persist auditable memory candidates and stable adjudication keys."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260718_0003"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def _add_columns(inspector: sa.Inspector, table: str, definitions: dict[str, sa.Column]) -> None:
    if not inspector.has_table(table):
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for name, column in definitions.items():
        if name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _add_columns(
        inspector,
        "memories",
        {
            "memory_key": sa.Column("memory_key", sa.String(512)),
            "polarity": sa.Column("polarity", sa.String(32)),
            "scope_json": sa.Column("scope_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        },
    )
    _add_columns(
        inspector,
        "memory_decisions",
        {
            "policy_version": sa.Column("policy_version", sa.String(128), nullable=False, server_default="memory-policy-v1"),
            "adjudicator_version": sa.Column("adjudicator_version", sa.String(128), nullable=False, server_default="memory-adjudicator-v1"),
            "model": sa.Column("model", sa.String(255)),
            "prompt_hash": sa.Column("prompt_hash", sa.String(128)),
            "input_hash": sa.Column("input_hash", sa.String(128)),
            "target_snapshot_version": sa.Column("target_snapshot_version", sa.Integer()),
            "retry_of_decision_id": sa.Column("retry_of_decision_id", sa.String(255)),
        },
    )

    inspector = sa.inspect(bind)
    if not inspector.has_table("memory_candidates"):
        op.create_table(
            "memory_candidates",
            sa.Column("candidate_id", sa.String(255), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("space_id", sa.String(255), nullable=False),
            sa.Column("note_id", sa.String(255), nullable=False),
            sa.Column("memory_type", sa.String(64), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("normalized_content", sa.Text(), nullable=False),
            sa.Column("memory_key", sa.String(512), nullable=False),
            sa.Column("subject", sa.Text()),
            sa.Column("predicate", sa.Text()),
            sa.Column("object_value", sa.Text()),
            sa.Column("task_status", sa.String(64)),
            sa.Column("polarity", sa.String(32)),
            sa.Column("scope_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("entities_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("valid_from", sa.String(64)),
            sa.Column("valid_until", sa.String(64)),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("importance", sa.Float(), nullable=False),
            sa.Column("evidence_span", sa.Text()),
            sa.Column("should_store", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("extractor_type", sa.String(32), nullable=False, server_default="rules"),
            sa.Column("extractor_version", sa.String(128), nullable=False, server_default="memory-extractor-v1"),
            sa.Column("model", sa.String(255)),
            sa.Column("prompt_hash", sa.String(128)),
            sa.Column("status", sa.String(32), nullable=False, server_default="extracted"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text()),
            sa.Column("decision_id", sa.String(255)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("applied_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("note_id", "candidate_id", name="uq_memory_candidate_note"),
        )
    else:
        inspector = sa.inspect(bind)
        _add_columns(
            inspector,
            "memory_candidates",
            {
                "task_status": sa.Column("task_status", sa.String(64)),
                "polarity": sa.Column("polarity", sa.String(32)),
                "scope_json": sa.Column("scope_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
                "entities_json": sa.Column("entities_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            },
        )

    inspector = sa.inspect(bind)
    memory_indexes = {index["name"] for index in inspector.get_indexes("memories")} if inspector.has_table("memories") else set()
    if "ix_memories_space_key_status" not in memory_indexes:
        op.create_index("ix_memories_space_key_status", "memories", ["space_id", "memory_key", "status"])
    candidate_indexes = {index["name"] for index in inspector.get_indexes("memory_candidates")} if inspector.has_table("memory_candidates") else set()
    if "ix_memory_candidates_space_status" not in candidate_indexes:
        op.create_index("ix_memory_candidates_space_status", "memory_candidates", ["space_id", "status", "updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("memory_candidates"):
        indexes = {index["name"] for index in inspector.get_indexes("memory_candidates")}
        if "ix_memory_candidates_space_status" in indexes:
            op.drop_index("ix_memory_candidates_space_status", table_name="memory_candidates")
        op.drop_table("memory_candidates")
    inspector = sa.inspect(bind)
    if inspector.has_table("memories") and "ix_memories_space_key_status" in {index["name"] for index in inspector.get_indexes("memories")}:
        op.drop_index("ix_memories_space_key_status", table_name="memories")
    for table, column in (
        ("memory_decisions", "retry_of_decision_id"),
        ("memory_decisions", "target_snapshot_version"),
        ("memory_decisions", "input_hash"),
        ("memory_decisions", "prompt_hash"),
        ("memory_decisions", "model"),
        ("memory_decisions", "adjudicator_version"),
        ("memory_decisions", "policy_version"),
        ("memories", "scope_json"),
        ("memories", "polarity"),
        ("memories", "memory_key"),
    ):
        inspector = sa.inspect(bind)
        if inspector.has_table(table) and column in {item["name"] for item in inspector.get_columns(table)}:
            op.drop_column(table, column)
