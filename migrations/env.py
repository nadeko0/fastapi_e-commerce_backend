from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.clock import UTCDateTime
from app.core.config import settings

# Import all models here
from app.models.base import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Set the database URL from settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URI.get_secret_value())

# Logging from alembic.ini, for the alembic command. disable_existing_loggers
# is False because the default disables every logger already created, which
# in a process that runs migrations itself (a deploy hook, the migration
# tests) silences the application's own loggers from then on. A caller that
# keeps its own logging passes config.attributes["configure_logger"] = False
# (Alembic's cookbook recipe) and is left alone.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Set SQLAlchemy metadata object for 'autogenerate' support
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Autogenerate writes a model's UTCDateTime as the plain column type it
    is in the database: a migration states the schema of its own revision and
    does not import application code, and Alembic would otherwise write
    app.core.clock.UTCDateTime(...) without importing it."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False

# Custom configuration values
config.set_section_option(
    config.config_ini_section,
    "sqlalchemy.pool_size",
    str(settings.DB_POOL_SIZE)
)
config.set_section_option(
    config.config_ini_section,
    "sqlalchemy.max_overflow",
    str(settings.DB_MAX_OVERFLOW)
)
config.set_section_option(
    config.config_ini_section,
    "sqlalchemy.pool_timeout",
    str(settings.DB_POOL_TIMEOUT)
)

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # Enable column type comparison
        compare_server_default=True,  # Compare server defaults
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.QueuePool,
    )

    # Disposed at the end: the pool would otherwise keep its connection open
    # until garbage collection, which matters when Alembic runs inside a
    # longer-lived process (the migration tests call command.upgrade directly).
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,  # Enable column type comparison
                compare_server_default=True,  # Compare server defaults
                render_item=render_item,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
