"""
Alembic runs as the Postgres ADMIN role (acbmsakadmin), never as the
restricted `acb_app` runtime role -- migrations need to CREATE ROLE, CREATE
TABLE, ALTER ... ENABLE ROW LEVEL SECURITY, and GRANT, none of which
`acb_app` should ever be allowed to do.

Connection string comes from the `DATABASE_URL` env var directly (a plain
`postgresql+asyncpg://acbmsakadmin:<password>@acb-msak-postgre-sql...`
string), not from app.config.Settings -- this keeps the migration runner
decoupled from the app's own Key-Vault-via-managed-identity settings loader,
so it can be handed a connection string however is most convenient for
whoever/whatever is invoking `alembic upgrade head` (a developer's shell
locally, or the one-shot Container Apps Job's secretRef-populated env var
against the real Azure Postgres instance).
"""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db.base import Base
import app.db.models  # noqa: F401  (populates Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL env var is required to run migrations "
        "(e.g. postgresql+asyncpg://acbmsakadmin:<pw>@acb-msak-postgre-sql"
        ".postgres.database.azure.com:5432/postgres)"
    )
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
