"""Backfill dirty semantic profile facets from existing memories."""

from __future__ import annotations

from alembic import op


revision = "20260815_0014"
down_revision = "20260815_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO semantic_profile_projections (
            space_id, facet, tenant_id, projection_json, source_memory_ids_json,
            processed_revision, target_revision, status, dirty_since,
            created_at, updated_at
        )
        SELECT
            m.space_id,
            COALESCE(NULLIF(m.predicate, ''), 'other') AS facet,
            MIN(m.tenant_id) AS tenant_id,
            '{}'::jsonb,
            '[]'::jsonb,
            0,
            1,
            'dirty',
            now(),
            now(),
            now()
        FROM memories AS m
        WHERE m.memory_type = 'semantic' AND m.status = 'active'
        GROUP BY m.space_id, COALESCE(NULLIF(m.predicate, ''), 'other')
        ON CONFLICT (space_id, facet) DO NOTHING
        """
    )


def downgrade() -> None:
    # Projections are derived state; downgrade intentionally leaves rows alone.
    pass

