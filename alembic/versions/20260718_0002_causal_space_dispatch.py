"""Add causal space dispatch state and separate task failure counters."""

from alembic import op
import sqlalchemy as sa

revision = "20260718_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("spaces", sa.Column("processed_sequence_no", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("spaces", sa.Column("memory_watermark", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("spaces", sa.Column("memory_gap_sequence_no", sa.BigInteger(), nullable=True))
    op.add_column("tasks", sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("tasks", sa.Column("defer_count", sa.Integer(), server_default="0", nullable=False))
    op.create_index("ix_tasks_space_status_created", "tasks", ["space_id", "status", "created_at"])
    op.execute(
        """
        UPDATE spaces AS s
        SET processed_sequence_no = COALESCE(
                (
                    SELECT MAX(i.sequence_no)
                    FROM inbox_messages AS i
                    WHERE i.space_id = s.id
                      AND i.status IN ('processed', 'failed')
                ),
                0
            ),
            memory_watermark = COALESCE(
                (
                    SELECT MAX(i.sequence_no)
                    FROM inbox_messages AS i
                    WHERE i.space_id = s.id
                      AND i.status = 'processed'
                ),
                0
            ),
            memory_gap_sequence_no = (
                SELECT MAX(i.sequence_no)
                FROM inbox_messages AS i
                WHERE i.space_id = s.id
                  AND i.status = 'failed'
            )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_space_status_created", table_name="tasks")
    op.drop_column("tasks", "defer_count")
    op.drop_column("tasks", "failure_count")
    op.drop_column("spaces", "memory_gap_sequence_no")
    op.drop_column("spaces", "memory_watermark")
    op.drop_column("spaces", "processed_sequence_no")
