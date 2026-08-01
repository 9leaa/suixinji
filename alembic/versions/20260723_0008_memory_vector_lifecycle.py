"""文件作用：Memory 向量生命周期迁移。

项目关系：本文件依赖 `alembic`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260723_0008"
down_revision = "20260718_0007"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    """函数功能：`_columns` 负责处理 columns，服务于本文件职责：Memory 向量生命周期迁移。
    传参：
        inspector: inspector 参数，由调用方传入，类型为 `sa.Inspector`。
        table: table 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_column(inspector: sa.Inspector, table: str, column: sa.Column) -> None:
    """函数功能：`_add_column` 负责处理 add column，服务于本文件职责：Memory 向量生命周期迁移。
    传参：
        inspector: inspector 参数，由调用方传入，类型为 `sa.Inspector`。
        table: table 参数，由调用方传入，类型为 `str`。
        column: column 参数，由调用方传入，类型为 `sa.Column`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if column.name not in _columns(inspector, table):
        op.add_column(table, column)


def upgrade() -> None:
    """函数功能：`upgrade` 负责处理 upgrade，服务于本文件职责：Memory 向量生命周期迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
    """函数功能：`downgrade` 负责处理 downgrade，服务于本文件职责：Memory 向量生命周期迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
