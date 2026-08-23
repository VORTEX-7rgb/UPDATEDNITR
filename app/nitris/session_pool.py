"""Per-user authenticated NITRIS session pool (PERF P1).

THE WIN: every operation used to pay a fresh paced login (~4.5s measured).
With the pool, a warm operation reuses the user's already-authenticated
NitrisClient — login cost drops to ~0ms for 10 minutes of activity.

SAFETY MODEL
============
* Per-user asyncio.Lock serializes concurrent jobs on the SAME account
  (protects the shared httpx cookie jar + ASP.NET session state).
* Every run happens inside ONE gateway.acquire() slot — identical admission,
  circuit-breaker and pacing behavior as before; nothing bypasses the gate.
* Passwords stay JIT: decrypted inside the held slot, passed only to the
  caller's work callable, never logged or stored.
* Automatic invalidation on LoginError / SessionExpiredError /
  CredentialsQuarantinedError → entry dropped + closed → next run
  re-authenticates fresh. Quarantine semantics are untouched because
  login_through_gateway remains the ONLY login path.
* Sliding TTL (default 10 min): each successful run extends the lease.
  Hard cap on pool size with lazy eviction of idle entries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar

from app.config import config
from app.db.crypto import decrypt_password
from app.nitris.client import NitrisClient

logger = logging.getLogger(__name__)

T = TypeVar("T")

# PERF: raised from 600/64. ASP.NET portal sessions typically live ~20–30 min
# server-side (sliding), so a 30-min client TTL stays inside that window and
# background syncs mostly stop re-logging entirely. A stale reuse is harmless:
# SessionExpiredError drops the entry and the next run re-authenticates.
SESSION_TTL_SECONDS = config.NITRIS_SESSION_TTL_SECONDS   # 30 min, sliding on every successful run
MAX_POOLED_SESSIONS = config.NITRIS_SESSION_POOL_MAX      # safe with the SHARED httpx transport (#3)

from app.nitris.exceptions import (  # noqa: E402
    CredentialsQuarantinedError,
    LoginError,
    SessionExpiredError,
)


class _Entry:
    __slots__ = ("client", "lock", "expires", "user_id")

    def __init__(self, client: NitrisClient, user_id: int) -> None:
        self.client = client
        self.user_id = user_id
        self.lock = asyncio.Lock()
        self.expires = time.monotonic() + SESSION_TTL_SECONDS


_pool: dict[int, _Entry] = {}


async def drop_session(user_id: int) -> bool:
    """Remove and close a user's pooled session. Returns True if one existed."""
    entry = _pool.pop(user_id, None)
    if entry is None:
        return False
    try:
        await entry.client.close()
    except Exception:
        pass
    return True


async def drop_all_sessions() -> int:
    """Shutdown helper — close everything. Returns count dropped."""
    ids = list(_pool.keys())
    for uid in ids:
        await drop_session(uid)
    return len(ids)


def _client_is_usable(entry: _Entry) -> bool:
    try:
        return not entry.client.client.is_closed
    except Exception:
        return False


def is_session_warm(user_id: int) -> bool:
    """True when a usable pooled session exists for this user with a healthy
    remaining lifetime. Used by the Layer-1 session warmer to skip jobs."""
    entry = _pool.get(user_id)
    return (
        entry is not None
        and entry.expires > time.monotonic() + 60
        and _client_is_usable(entry)
    )


async def _evict_if_over_cap() -> None:
    if len(_pool) < MAX_POOLED_SESSIONS:
        return
    # Drop expired/idle-unlocked entries oldest-first until under cap.
    candidates = sorted(
        (e for e in _pool.values() if not e.lock.locked()),
        key=lambda e: e.expires,
    )
    for entry in candidates:
        if len(_pool) < MAX_POOLED_SESSIONS:
            break
        await drop_session(entry.user_id)


async def with_pooled_session(
    *,
    user_id: int,
    roll_number: str,
    encrypted_password: str,
    work: Callable[[NitrisClient, str], Awaitable[T]],
) -> T:
    """Run ``work(client, plaintext_password)`` with a pooled authenticated
    client for this user.

    * Cache MISS  → new NitrisClient + paced gateway login, then work.
    * Cache HIT   → zero login cost; straight to work.
    Both paths hold exactly ONE gateway slot for [login?] + work, preserving
    the established lease-boundary discipline.
    """
    global _pool  # noqa: F841  (module dict mutated in place; kept for clarity)

    # ── Validate/reap any existing entry ────────────────────────────────
    entry = _pool.get(user_id)
    if entry is not None and (
        entry.expires <= time.monotonic() or not _client_is_usable(entry)
    ):
        await drop_session(user_id)
        entry = None

    if entry is None:
        await _evict_if_over_cap()
        entry = _Entry(NitrisClient(), user_id)
        _pool[user_id] = entry
        needs_login = True
        logger.debug("session_pool: NEW session for user_id=%d", user_id)
    else:
        needs_login = False
        logger.debug("session_pool: REUSE session for user_id=%d", user_id)

    async with entry.lock:                       # serialize same-account jobs
        async with _gateway_acquire():           # single slot: [login?] + work
            password = decrypt_password(encrypted_password)   # JIT inside slot
            if needs_login:
                from app.nitris.gateway import nitris_gateway
                await nitris_gateway.login_through_gateway(
                    entry.client, roll_number, password, user_id=user_id,
                )
            try:
                result = await work(entry.client, password)
            except (LoginError, SessionExpiredError, CredentialsQuarantinedError):
                logger.info(
                    "session_pool: dropping user_id=%d session (%s)",
                    user_id, "auth/session fault",
                )
                await drop_session(user_id)
                raise
            except BaseException:
                # Transient failure — session is likely still valid; keep it.
                raise
            entry.expires = time.monotonic() + SESSION_TTL_SECONDS   # sliding TTL
            return result


# Imported late to avoid a circular import at module load.
def _gateway_acquire():
    from app.nitris.gateway import nitris_gateway
    return nitris_gateway.acquire()
