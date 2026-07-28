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
    """负责“运行migrationsoffline”。

    该函数是 `alembic.env` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
    raise RuntimeError("Offline migrations are disabled; set DATABASE_URL and run Alembic online")


def run_migrations_online() -> None:
    """负责“运行migrationsonline”。

    该函数是 `alembic.env` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
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
