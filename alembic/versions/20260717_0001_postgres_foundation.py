"""Create the shared PostgreSQL foundation schema."""

import hashlib

from alembic import op

from infrastructure.schema import Base

revision = "20260717_0001"
down_revision = None
branch_labels = None
depends_on = None

FROZEN_SCHEMA_FINGERPRINT = "44799c6741957a953251793da412f7f6ae9aff147ae51643bb783c24f773b2dc"


def _metadata_fingerprint() -> str:
    parts: list[str] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        parts.append(f"T:{table.name}")
        for column in table.columns:
            parts.append(
                "C:"
                f"{column.name}:{column.type}:{column.nullable}:{column.primary_key}:"
                f"{column.default is not None}:{column.server_default is not None}"
            )
        for constraint in sorted(
            table.constraints,
            key=lambda item: (
                item.__class__.__name__,
                str(getattr(item, "name", "")),
                ",".join(sorted(getattr(column, "name", "") for column in getattr(item, "columns", []))),
            ),
        ):
            columns = ",".join(column.name for column in getattr(constraint, "columns", []))
            parts.append(f"K:{constraint.__class__.__name__}:{getattr(constraint, 'name', None)}:{columns}")
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            columns = ",".join(column.name for column in index.columns)
            parts.append(f"I:{index.name}:{columns}:{index.unique}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    fingerprint = _metadata_fingerprint()
    if fingerprint != FROZEN_SCHEMA_FINGERPRINT:
        raise RuntimeError(
            "foundation migration metadata fingerprint changed; freeze a new explicit baseline "
            f"or update the migration intentionally (expected {FROZEN_SCHEMA_FINGERPRINT}, got {fingerprint})"
        )
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
