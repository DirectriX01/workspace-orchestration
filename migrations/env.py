"""Alembic migration environment."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.models import Base

# Alembic Config object providing access to the values in alembic.ini.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the database URL from application settings.
config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)

# Metadata used for 'autogenerate' support.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    context.configure(
        url=get_settings().sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (with a live DBAPI connection)."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_settings().sync_database_url
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
