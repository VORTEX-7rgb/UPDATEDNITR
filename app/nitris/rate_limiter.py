"""Operation-specific rate limiter and cooldown tracker per user."""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Tuple, Optional

from app.config import config

# Cooldown windows are env-tunable via config (source of truth) and re-exported
# here so handler imports stay stable.
COOLDOWN_ATTENDANCE_REFRESH = config.COOLDOWN_ATTENDANCE_REFRESH
COOLDOWN_INBOX_REFRESH = config.COOLDOWN_INBOX_REFRESH
COOLDOWN_ATTACHMENT_DOWNLOAD = config.COOLDOWN_ATTACHMENT_DOWNLOAD
COOLDOWN_PAPERS_SEARCH = config.COOLDOWN_PAPERS_SEARCH

# Sweep cadence: prune expired entries every N successful writes. Keeps the
# dict bounded without holding the lock for long (one O(n) pass per N cheap
# writes is amortized O(1)). Without this, keys accumulate forever — each
# user/operation/per-object combo (e.g. "{uid}:attachment_download:{msg_id}")
# would otherwise leak until restart.
_PRUNE_EVERY_WRITES = config.RATE_LIMITER_PRUNE_EVERY


class OperationCooldown:
    """Tracks per-user operation cooldowns with async locking."""

    def __init__(self):
        self._cooldowns: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._writes_since_prune = 0

    def _prune_expired(self, now: float) -> int:
        """Drop expired entries. Caller must hold the lock. Returns pruned count."""
        expired = [k for k, exp in self._cooldowns.items() if now >= exp]
        for k in expired:
            del self._cooldowns[k]
        return len(expired)

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
            # Prune expired keys periodically (bounded-memory guarantee).
            self._writes_since_prune += 1
            if self._writes_since_prune >= _PRUNE_EVERY_WRITES:
                self._writes_since_prune = 0
                self._prune_expired(now)

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

    async def prune_expired(self) -> int:
        """Force-prune expired entries. Returns number removed (diagnostics/tests)."""
        now = time.monotonic()
        async with self._lock:
            return self._prune_expired(now)

    def get_stats(self) -> dict:
        """Return diagnostic count of ACTIVE cooldowns, pruning stale entries first."""
        now = time.monotonic()
        # Best-effort prune without the lock (dict comprehension over a snapshot;
        # single-threaded event loop makes this safe — check()/clear() mutate
        # under the lock but we only delete already-expired keys).
        expired = [k for k, exp in list(self._cooldowns.items()) if now >= exp]
        for k in expired:
            self._cooldowns.pop(k, None)
        return {
            "active_cooldowns": len(self._cooldowns),
        }


operation_cooldown = OperationCooldown()


# Backward-compatible sync helpers (no awaitable context). Kept pruned on read
# so even this legacy path cannot grow without bound.
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
