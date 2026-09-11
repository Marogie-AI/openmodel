import socket
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.errors import DatabaseUnavailable


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session, rolled back on exception.

    It does not commit: teardown runs after the response body is on the wire, so a commit that
    failed here could no longer turn the 201 the client already read into a 503. Write handlers
    commit themselves, before they build a response.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
        except (ConnectionError, socket.gaierror) as exc:
            # SQLAlchemy only wraps errors the driver reports, and it never sees these: a
            # failure to open the socket comes through raw from asyncpg. Not the OSError base,
            # which since 3.11 also covers TimeoutError — a slow anything is not a dead database.
            await session.rollback()
            raise DatabaseUnavailable() from exc
        except Exception:
            await session.rollback()
            raise
