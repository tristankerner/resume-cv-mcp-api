from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from alembic import context

# Imported for their side effect of registering on SQAlchemyBase.metadata.
# Without them `alembic revision --autogenerate` sees an empty model and
# proposes dropping every table.
from persistence import (  # noqa: F401
    api_key,
    auth_failure,
    document,
    oauth_authorization_code,
    oauth_client,
    oauth_refresh_token,
    role_scope,
    user,
)
from persistence.base import SQAlchemyBase


def _configured_url() -> str | None:
    """The application's own DATABASE_URL, as a synchronous URL.

    Startup runs `alembic upgrade head` in-process, so without this the
    migrations would go to whatever alembic.ini hardcodes while the app talked
    to DATABASE_URL — fine locally where they agree, silently wrong in a
    container where the database lives on a mounted volume.

    Falls back to the ini value when settings can't be loaded, so alembic
    remains usable without a full environment.
    """
    try:
        from services.config.config_service import ConfigService

        url = make_url(str(ConfigService.get_without_deps().settings.database_url))
    except Exception:  # noqa: BLE001 - any settings failure falls back to the ini
        return None

    # Migrations run on a sync engine; drop the async driver if there is one.
    sync_drivers = {
        "sqlite+aiosqlite": "sqlite",
        "postgresql+asyncpg": "postgresql+psycopg",
    }
    if url.drivername in sync_drivers:
        url = url.set(drivername=sync_drivers[url.drivername])

    # The two Postgres drivers spell TLS differently: asyncpg takes `ssl`,
    # psycopg takes libpq's `sslmode`. Swapping only the driver would hand
    # psycopg a parameter it rejects, so a managed database that requires TLS —
    # Neon, and most others — could not be migrated with the URL the
    # application uses. Translate rather than ask for the setting twice.
    if url.drivername == "postgresql+psycopg" and "ssl" in url.query:
        query = dict(url.query)
        query["sslmode"] = query.pop("ssl")
        url = url.set(query=query)

    return url.render_as_string(hide_password=False)


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would switch off every
    # logger already configured — including the one the app logs to. Startup
    # runs migrations in-process before anything else, so leaving the default
    # silently swallows every log line the application emits afterwards.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = SQAlchemyBase.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = _configured_url() or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    section = config.get_section(config.config_ini_section, {})
    configured_url = _configured_url()
    if configured_url:
        section["sqlalchemy.url"] = configured_url

    connectable = engine_from_config(
        section,
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
