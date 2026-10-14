from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.database import Base
from app import models  # noqa: F401  确保模型注册到 metadata

config = context.config
# 迁移测试可经 config.attributes["migration_url"] 或 OPSSOP_MIGRATION_URL 指向一次性库
import os

_migration_url = config.attributes.get("migration_url") or os.environ.get("OPSSOP_MIGRATION_URL")
_url = _migration_url or get_settings().database_url
# ConfigParser 会把 % 当作插值符号;SQLAlchemy URL 已正确转义,这里再转义 % 供 ConfigParser 解析
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
