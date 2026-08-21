"""Database engine, sessionmaker, and session context management."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.exc import OperationalError, InterfaceError
from app.config import config

logger = logging.getLogger(__name__)

# Single source of truth for the async engine and sessionmaker
# Uses asyncpg driver under the hood
engine = create_async_engine(
    config.DATABASE_URL,
    echo=False,  # Set to True to log SQL statements during debugging
    pool_pre_ping=True,  # Prevent using stale/dropped connections
    pool_size=config.DB_POOL_SIZE,          # env-tunable (launch: 20)
    max_overflow=config.DB_MAX_OVERFLOW,    # env-tunable (launch: 30)
    pool_recycle=1800,  # Recycle connections older than 30 minutes to prevent stale sockets
)

# Async session factory
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Prevent attributes from expiring after commit
)


def is_db_connection_error(e: Exception) -> bool:
    """Identify if a given exception corresponds to a database connection failure."""
    err_str = str(e).lower()
    if isinstance(e, (OperationalError, InterfaceError)):
        return True
    if isinstance(e, (ConnectionResetError, ConnectionRefusedError, OSError)):
        return True
    # Catch asyncpg/postgres network reset error signatures
    if any(sig in err_str for sig in ("connection reset", "closed by remote host", "connection refused", "reset by peer", "cannot connect", "lost connection")):
        return True
    return False


# ── Debounced pool disposal ─────────────────────────────────────────────────
# During a DB blip, MANY concurrent tasks can hit connection errors at once.
# Disposing the whole engine pool from each of them tears down connections
# other tasks are still using and multiplies the churn. The dispose now runs
# at most once per debounce window, under a lock.
_POOL_DISPOSE_DEBOUNCE_SECONDS = 60.0
_last_pool_dispose = 0.0
_dispose_lock = asyncio.Lock()


async def _dispose_engine_debounced() -> None:
    global _last_pool_dispose
    async with _dispose_lock:
        now = time.monotonic()
        if now - _last_pool_dispose < _POOL_DISPOSE_DEBOUNCE_SECONDS:
            logger.debug("Pool disposal skipped (debounced).")
            return
        _last_pool_dispose = now
    try:
        await engine.dispose()
        logger.info("Database connection pool disposed successfully.")
    except Exception as dispose_err:
        logger.error("Failed to dispose engine connection pool: %s", dispose_err)


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional async database session context manager.
    
    Guarantees clean closure and rollback of the session to prevent pool exhaustion.
    Disposes connection pool proactively on connection failures to recover seamlessly.
    """
    session: AsyncSession = async_session_factory()
    try:
        yield session
    except Exception as e:
        if is_db_connection_error(e):
            logger.warning(
                "Database connection lost or reset encountered: %s. Disposing engine connection pool.",
                str(e)
            )
            await _dispose_engine_debounced()
        else:
            logger.error("Database session error encountered, rolling back: %s", e)
            
        try:
            await session.rollback()
        except Exception:
            pass
        raise
    finally:
        await session.close()
