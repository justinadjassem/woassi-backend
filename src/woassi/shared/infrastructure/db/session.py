"""Database engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """Async engine. `pool_pre_ping` replaces connections the database has closed."""
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory.

    `expire_on_commit=False`: loaded objects stay readable after the commit, without a new query.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
