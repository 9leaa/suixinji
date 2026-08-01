"""文件作用：查询性能迁移。

项目关系：本文件依赖 `alembic`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260718_0004"
down_revision = "20260718_0003"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_notes_space_type_created", "notes", ["space_id", "note_type", "created_at"]),
    ("ix_notes_space_enrichment_created", "notes", ["space_id", "enrichment_status", "created_at"]),
    ("ix_note_tags_tag_note", "note_tags", ["tag", "note_id"]),
    ("ix_note_relations_target", "note_relations", ["target_note_id"]),
    ("ix_memories_space_status_updated", "memories", ["space_id", "status", "updated_at"]),
)


def upgrade() -> None:
    """函数功能：`upgrade` 负责处理 upgrade，服务于本文件职责：查询性能迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table, columns in INDEXES:
        if not inspector.has_table(table):
            continue
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns)
        inspector = sa.inspect(bind)


def downgrade() -> None:
    """函数功能：`downgrade` 负责处理 downgrade，服务于本文件职责：查询性能迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table, _columns in reversed(INDEXES):
        if inspector.has_table(table) and name in {index["name"] for index in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
        inspector = sa.inspect(bind)
