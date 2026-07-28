"""Complete Memory Vector retry lifecycle metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260723_0008"
down_revision = "20260718_0007"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    """负责“columns”。

    该函数是 `alembic.versions.20260723_0008_memory_vector_lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_column(inspector: sa.Inspector, table: str, column: sa.Column) -> None:
    """负责“添加column”。

    该函数是 `alembic.versions.20260723_0008_memory_vector_lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    if column.name not in _columns(inspector, table):
        op.add_column(table, column)


def upgrade() -> None:
    """负责“upgrade”。

    该函数是 `alembic.versions.20260723_0008_memory_vector_lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("memory_vectors"):
        _add_column(inspector, "memory_vectors", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        _add_column(inspector, "memory_vectors", sa.Column("next_retry_at", sa.DateTime(timezone=True)))
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_memory_vectors_retryable
            ON memory_vectors (status, next_retry_at, updated_at)
            """
        )
    if inspector.has_table("memory_candidates"):
        _add_column(inspector, "memory_candidates", sa.Column("clause_index", sa.Integer()))


def downgrade() -> None:
    """负责“downgrade”。

    该函数是 `alembic.versions.20260723_0008_memory_vector_lifecycle` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("memory_candidates") and "clause_index" in _columns(inspector, "memory_candidates"):
        op.drop_column("memory_candidates", "clause_index")
    if inspector.has_table("memory_vectors"):
        op.execute("DROP INDEX IF EXISTS ix_memory_vectors_retryable")
        columns = _columns(inspector, "memory_vectors")
        for name in ("next_retry_at", "attempt_count"):
            if name in columns:
                op.drop_column("memory_vectors", name)
