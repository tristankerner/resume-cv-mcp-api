from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from typing import ClassVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config.config_service import ConfigService


class DatabaseService:
    _engine: ClassVar[AsyncEngine | None] = None
    _session_factory: ClassVar[async_sessionmaker[AsyncSession] | None] = None

    @classmethod
    def _ensure_built(cls) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
        if cls._engine is None or cls._session_factory is None:
            settings = ConfigService.get_with_deps().settings
            cls._engine = create_async_engine(
                str(settings.database_url),
                echo=settings.database_echo,
                # A serverless Postgres suspends its compute when idle (Neon
                # after five minutes) and every pooled connection dies with
                # it, so the first request after a quiet spell would be
                # served a dead socket without some form of mitigation.
                # `pool_pre_ping` would cover that, but it spends a round
                # trip on *every* checkout, not just a stale one - after
                # QUERY_PERFORMANCE_PLAN.md's phases land `/applications` at
                # two statements, that ping is a third of the request. Recycle
                # alone gets the same protection more cheaply: 240s is under
                # Neon's five-minute suspend, so a pooled connection is always
                # discarded and replaced before Neon would have killed it -
                # no request ever reaches a dead socket, and no request pays
                # for a ping it didn't need. No-op on SQLite.
                pool_recycle=240,
            )
            cls._session_factory = async_sessionmaker(
                bind=cls._engine,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
        return cls._engine, cls._session_factory

    @classmethod
    def engine(cls) -> AsyncEngine:
        engine, _ = cls._ensure_built()
        return engine

    @classmethod
    def session_factory(cls) -> async_sessionmaker[AsyncSession]:
        """Built on first use, not at import. Keeps the pool settings and
        their reasoning — see the `pool_recycle` comment in `_ensure_built`."""
        _, session_factory = cls._ensure_built()
        return session_factory

    @classmethod
    def session(cls) -> AbstractAsyncContextManager[AsyncSession]:
        """Replaces `async with AsyncSessionLocal() as db`."""
        return cls.session_factory()()

    @classmethod
    async def reset(cls) -> None:
        """Dispose the engine and drop it, so the next use rebuilds it against
        current settings.

        Async because dropping the reference is not enough: the engine owns a
        connection pool, and on SQLite an aiosqlite worker thread, both of
        which have to be closed rather than abandoned. A synchronous version
        left callers to dispose by hand before calling this, and a caller that
        forgot leaked a pool per call with nothing to show for it.
        """
        if cls._engine is not None:
            await cls._engine.dispose()
        cls._engine = None
        cls._session_factory = None

    @staticmethod
    async def get_async_db_session() -> AsyncGenerator[AsyncSession]:
        async with DatabaseService.session() as session:
            yield session
