"""Enforce memory-decision idempotency by space and candidate."""

from alembic import op
import sqlalchemy as sa

revision = "20260802_0010"
down_revision = "20260723_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("memory_decisions"):
        return
    # Historical duplicates are retained for audit, but only the newest row
    # keeps the idempotency key. Older rows receive a non-replayable legacy key.
    op.execute("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY space_id, candidate_id ORDER BY created_at DESC, id DESC
            ) AS row_number
            FROM memory_decisions
        )
        UPDATE memory_decisions AS decision
        SET candidate_id = decision.candidate_id || ':legacy:' || decision.id
        FROM ranked
        WHERE decision.id = ranked.id AND ranked.row_number > 1
    """)
    names = {item["name"] for item in inspector.get_unique_constraints("memory_decisions")}
    if "uq_memory_decision_space_candidate" not in names:
        op.create_unique_constraint("uq_memory_decision_space_candidate", "memory_decisions", ["space_id", "candidate_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    names = {item["name"] for item in inspector.get_unique_constraints("memory_decisions")} if inspector.has_table("memory_decisions") else set()
    if "uq_memory_decision_space_candidate" in names:
        op.drop_constraint("uq_memory_decision_space_candidate", "memory_decisions", type_="unique")
