"""Async SQLAlchemy/SQLModel engine and session factory.

The engine is created once at module import time (it is lazy — no
connection is opened until first use) and disposed on app shutdown.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
)

# expire_on_commit=False is essential in async code: after a commit,
# accessing attributes on an ORM object would otherwise trigger a lazy
# refresh, which fails outside of an active transaction in async mode.
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create tables if they do not exist.

    Fine for a single-developer project at this stage. Once the schema
    stabilizes and you care about migrations, swap this for Alembic.
    """
    # Import models so SQLModel.metadata knows about every table.
    from app.models import match, session  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a scoped async session."""
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Gracefully close the connection pool on shutdown."""
    await engine.dispose()
