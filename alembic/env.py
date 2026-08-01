"""文件作用：Alembic 运行环境。

项目关系：本文件依赖 `alembic`、`infrastructure.database`、`infrastructure.schema`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from infrastructure.database import get_engine
from infrastructure.schema import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """函数功能：`run_migrations_offline` 负责运行 migrations offline，服务于本文件职责：Alembic 运行环境。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    raise RuntimeError("Offline migrations are disabled; set DATABASE_URL and run Alembic online")


def run_migrations_online() -> None:
    """函数功能：`run_migrations_online` 负责运行 migrations online，服务于本文件职责：Alembic 运行环境。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with get_engine().connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
