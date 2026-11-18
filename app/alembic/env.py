import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name and os.path.exists(config.config_file_name):
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

# URL: сначала из alembic-конфига (его выставляет приложение), иначе из окружения.
url = config.get_main_option("sqlalchemy.url") or os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://inv:inv@db:5432/inventory")
config.set_main_option("sqlalchemy.url", url)

# Метаданные моделей (для autogenerate); при запуске миграций не обязательны.
try:
    from main import Base
    target_metadata = Base.metadata
except Exception:
    target_metadata = None


def run_migrations_offline():
    context.configure(url=url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True,
                          render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
