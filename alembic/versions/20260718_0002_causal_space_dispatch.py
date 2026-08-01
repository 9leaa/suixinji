"""文件作用：因果调度迁移。

项目关系：本文件依赖 `alembic`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from alembic import op
import sqlalchemy as sa

revision = "20260718_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """函数功能：`upgrade` 负责处理 upgrade，服务于本文件职责：因果调度迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    inspector = sa.inspect(op.get_bind())
    space_columns = {column["name"] for column in inspector.get_columns("spaces")}
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    task_indexes = {index["name"] for index in inspector.get_indexes("tasks")}

    if "processed_sequence_no" not in space_columns:
        op.add_column("spaces", sa.Column("processed_sequence_no", sa.BigInteger(), server_default="0", nullable=False))
    if "memory_watermark" not in space_columns:
        op.add_column("spaces", sa.Column("memory_watermark", sa.BigInteger(), server_default="0", nullable=False))
    if "memory_gap_sequence_no" not in space_columns:
        op.add_column("spaces", sa.Column("memory_gap_sequence_no", sa.BigInteger(), nullable=True))
    if "failure_count" not in task_columns:
        op.add_column("tasks", sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False))
    if "defer_count" not in task_columns:
        op.add_column("tasks", sa.Column("defer_count", sa.Integer(), server_default="0", nullable=False))
    if "ix_tasks_space_status_created" not in task_indexes:
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
    """函数功能：`downgrade` 负责处理 downgrade，服务于本文件职责：因果调度迁移。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    inspector = sa.inspect(op.get_bind())
    space_columns = {column["name"] for column in inspector.get_columns("spaces")}
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    task_indexes = {index["name"] for index in inspector.get_indexes("tasks")}

    if "ix_tasks_space_status_created" in task_indexes:
        op.drop_index("ix_tasks_space_status_created", table_name="tasks")
    if "defer_count" in task_columns:
        op.drop_column("tasks", "defer_count")
    if "failure_count" in task_columns:
        op.drop_column("tasks", "failure_count")
    if "memory_gap_sequence_no" in space_columns:
        op.drop_column("spaces", "memory_gap_sequence_no")
    if "memory_watermark" in space_columns:
        op.drop_column("spaces", "memory_watermark")
    if "processed_sequence_no" in space_columns:
        op.drop_column("spaces", "processed_sequence_no")
