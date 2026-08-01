"""文件作用：Memory trigram 搜索迁移。

项目关系：本文件依赖 `alembic`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260723_0009"
down_revision = "20260723_0008"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    """函数功能：`_columns` 负责处理 columns，服务于本文件职责：Memory trigram 搜索迁移。
    传参：
        inspector: inspector 参数，由调用方传入，类型为 `sa.Inspector`。
        table: table 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    """函数功能：`upgrade` 负责处理 upgrade，服务于本文件职责：Memory trigram 搜索迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("memories"):
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("DROP INDEX IF EXISTS ix_memories_search_document")
    if "search_document" not in _columns(inspector, "memories"):
        op.add_column(
            "memories",
            sa.Column(
                "search_document",
                postgresql.TSVECTOR(),
                sa.Computed(
                    "to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(subject, '') || ' ' || coalesce(predicate, '') || ' ' || coalesce(object_value, ''))",
                    persisted=True,
                ),
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_memories_search_document ON memories USING gin (search_document)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_memories_content_trgm ON memories USING gin (content gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_memories_object_value_trgm ON memories USING gin (object_value gin_trgm_ops)")


def downgrade() -> None:
    """函数功能：`downgrade` 负责处理 downgrade，服务于本文件职责：Memory trigram 搜索迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    op.execute("DROP INDEX IF EXISTS ix_memories_object_value_trgm")
    op.execute("DROP INDEX IF EXISTS ix_memories_content_trgm")
    op.execute("DROP INDEX IF EXISTS ix_memories_search_document")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("memories") and "search_document" in _columns(inspector, "memories"):
        op.drop_column("memories", "search_document")
