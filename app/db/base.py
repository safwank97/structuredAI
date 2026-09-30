"""
Async SQLAlchemy engine/session plumbing.

RLS note: the app connects as the restricted `acb_app` Postgres role (created
in migration 0001, NOT the table owner). Every user-owned table has
`ENABLE ROW LEVEL SECURITY` with a policy keyed off the Postgres session
variable `app.current_user_id`. Because `acb_app` does not own these tables,
Postgres enforces RLS automatically -- no FORCE ROW LEVEL SECURITY needed.

`get_db_session` (below) is the "no identity yet" dependency, used only by
the registration/login endpoints themselves (there is no authenticated user
yet at that point). Everything else goes through `get_scoped_db_session` in
app/core/deps.py, which additionally issues `SET LOCAL app.current_user_id`
for the authenticated caller inside the same transaction, so a bug in a
query's WHERE clause can never leak another user's rows -- the database
itself refuses to return them.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Plain session, no RLS context set. Use only where there is no
    authenticated user yet (register/login)."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session
