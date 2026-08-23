"""LAYER 1 — silent session pre-warming.

When a student touches ANY major surface (dashboard, inbox, timetable), we
fire a LOW-priority job that logs their pooled portal session in *now* if it
has gone cold. Their next attendance/inbox tap then skips the paced-login
wait entirely — cold taps drop from ~2-4s to ~1-1.5s.

Safety:
  - Skips instantly when the session is already warm (in-process check,
    zero DB/portal cost).
  - Per-user throttle window prevents render-storms from spamming jobs.
  - Jobs are LOW priority → can never delay an interactive tap.
  - Login goes through the SAME pooled-session/gateway path as real work.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Re-attempt a warm for the same user at most once per window (covers render
# storms; a genuinely failed login is also retried after this window).
_THROTTLE_WINDOW_SECONDS = 600.0
_throttle: dict[int, float] = {}
_THROTTLE_PRUNE_THRESHOLD = 5000


def is_session_warm(user_id: int) -> bool:
    from app.nitris.session_pool import is_session_warm as _pool_warm
    return _pool_warm(user_id)


def _throttle_ok(user_id: int) -> bool:
    now = time.monotonic()
    if len(_throttle) > _THROTTLE_PRUNE_THRESHOLD:
        expired = [k for k, ts in _throttle.items() if now - ts >= _THROTTLE_WINDOW_SECONDS]
        for k in expired:
            _throttle.pop(k, None)
    last = _throttle.get(user_id)
    if last is not None and now - last < _THROTTLE_WINDOW_SECONDS:
        return False
    _throttle[user_id] = now
    return True


async def request_session_warm(user_id: int) -> str:
    """Fire-and-forget entry point for touchpoints. Returns action taken:
    'warm' | 'queued' | 'throttled' | 'error'. Never raises."""
    try:
        if is_session_warm(user_id):
            return "warm"
        if not _throttle_ok(user_id):
            return "throttled"
        from app.nitris.job_queue import nitris_job_queue, Priority
        await nitris_job_queue.enqueue(
            job_type="session_warm",
            user_id=user_id,
            priority=Priority.LOW,
            dedup_key=f"session_warm:{user_id}",
            payload={"user_id": user_id},
        )
        return "queued"
    except Exception as e:  # never let warming disturb the touchpoint
        logger.debug("session warm enqueue failed for %s: %r", user_id, e)
        return "error"


async def warm_now(user_id: int) -> bool:
    """Job-side: load creds and run a no-op pass through the pooled session —
    this performs the paced gateway login if cold and refreshes the sliding
    TTL if warm."""
    from app.db.database import get_db_session
    from sqlalchemy import select

    try:
        async with get_db_session() as session:
            from app.db.models import User
            row = (
                await session.execute(
                    select(User.roll_number, User.encrypted_password).where(
                        User.id == user_id,
                        User.credentials_valid == True,  # noqa: E712
                    )
                )
            ).first()
        if row is None:
            return False
        roll_number, encrypted_password = row[0], row[1]

        from app.nitris.session_pool import with_pooled_session

        async def _noop(client, password):
            return True

        await with_pooled_session(
            user_id=user_id,
            roll_number=roll_number,
            encrypted_password=encrypted_password,
            work=_noop,
        )
        logger.info("session_warm: user_id=%s warmed", user_id)
        return True
    except Exception as e:
        logger.info("session_warm: user_id=%s could not warm (%r)", user_id, e)
        return False
