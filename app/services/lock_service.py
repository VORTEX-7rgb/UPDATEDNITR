"""User synchronization lock service providing a modular interface for concurrency control."""

import logging
import asyncio

logger = logging.getLogger(__name__)


class BaseUserLock:
    """Base interface for user-level synchronization locking."""

    async def acquire(self, user_id: int) -> bool:
        """Acquire a lock for the specified user_id.

        Returns:
            True if the lock was successfully acquired, False otherwise.
        """
        raise NotImplementedError()

    async def release(self, user_id: int) -> None:
        """Release the lock for the specified user_id."""
        raise NotImplementedError()


class InMemoryUserLock(BaseUserLock):
    """Single-process in-memory implementation of BaseUserLock using asyncio.Lock."""

    def __init__(self) -> None:
        self._active_syncs: set[int] = set()
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: int) -> bool:
        """Acquire the lock in a thread-safe, asyncio-compatible manner."""
        async with self._lock:
            if user_id in self._active_syncs:
                logger.debug("InMemoryUserLock: User ID %d is already locked.", user_id)
                return False
            self._active_syncs.add(user_id)
            logger.debug("InMemoryUserLock: Successfully locked User ID %d.", user_id)
            return True

    async def release(self, user_id: int) -> None:
        """Release the lock for the user_id."""
        async with self._lock:
            self._active_syncs.discard(user_id)
            logger.debug("InMemoryUserLock: Successfully unlocked User ID %d.", user_id)


# Expose a singleton instance of the lock manager
user_lock = InMemoryUserLock()
