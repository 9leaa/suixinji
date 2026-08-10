"""Add an indexed task-family recall path for active task memories."""

from __future__ import annotations

from alembic import op


revision = "20260810_0012"
down_revision = "20260806_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_memories_task_family_active
        ON memories (space_id, (scope_json ->> 'task_family_key'))
        WHERE memory_type = 'task' AND status = 'active'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memories_task_family_active")
