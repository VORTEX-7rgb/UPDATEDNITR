"""Operation-specific rate limiter and cooldown tracker per user."""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Tuple, Optional

COOLDOWN_ATTENDANCE_REFRESH = 60
COOLDOWN_INBOX_REFRESH = 60
COOLDOWN_ATTACHMENT_DOWNLOAD = 10
COOLDOWN_PAPERS_SEARCH = 10


class OperationCooldown:
    """Tracks per-user operation cooldowns with async locking."""

    def __init__(self):
        self._cooldowns: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def check(
        self,
        user_id: int,
        operation: str,
        key: Optional[str] = None,
        cooldown_seconds: int = 60,
    ) -> Tuple[bool, int]:
        """Check if an operation is allowed, and sets the cooldown if allowed.

        Returns:
            (allowed: bool, remaining_seconds: int)
        """
        now = time.monotonic()
        lookup_key = f"{user_id}:{operation}" if key is None else f"{user_id}:{operation}:{key}"

        async with self._lock:
            # Prune expired keys occasionally
            expires_at = self._cooldowns.get(lookup_key)
            if expires_at is not None and now < expires_at:
                remaining = int(expires_at - now) + 1
                return False, remaining

            # Set new cooldown
            self._cooldowns[lookup_key] = now + cooldown_seconds
            return True, 0

    async def clear(self, user_id: int, operation: str, key: Optional[str] = None) -> None:
        """Clear cooldown for a user operation."""
        lookup_key = f"{user_id}:{operation}" if key is None else f"{user_id}:{operation}:{key}"
        async with self._lock:
            self._cooldowns.pop(lookup_key, None)

    def get_stats(self) -> dict:
        """Return diagnostic count of active cooldowns."""
        now = time.monotonic()
        active = [k for k, exp in self._cooldowns.items() if now < exp]
        return {
            "active_cooldowns": len(active),
        }


operation_cooldown = OperationCooldown()


# Backward-compatible sync helpers
_sync_cooldowns: Dict[Tuple[int, str], float] = {}

def check_and_set_cooldown(user_id: int, operation: str, cooldown_seconds: int = 60) -> Tuple[bool, int]:
    now = time.monotonic()
    key = (user_id, operation)
    expires_at = _sync_cooldowns.get(key)

    if expires_at is not None and now < expires_at:
        remaining = int(expires_at - now) + 1
        return False, remaining

    _sync_cooldowns[key] = now + cooldown_seconds
    return True, 0


def clear_cooldown(user_id: int, operation: str) -> None:
    _sync_cooldowns.pop((user_id, operation), None)


def get_active_cooldowns_count() -> int:
    now = time.monotonic()
    expired = [k for k, exp in _sync_cooldowns.items() if now >= exp]
    for k in expired:
        _sync_cooldowns.pop(k, None)
    return len(_sync_cooldowns)
