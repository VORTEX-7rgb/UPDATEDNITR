"""Predictive class-end attendance sync — freshness exactly when it matters.

THE INSIGHT
===========
Attendance only changes when classes happen, and students check attendance
RIGHT after their last class of the day. The old 12h TTL background sync
almost never ran, so the cache-first view was stale at exactly the moment
of peak demand — forcing a slow live refresh onto the portal.

This module watches each student's stored timetable (already synced via
/timetable) and fires ONE LOW-priority attendance sync per user per day,
shortly after their LAST class ends. Result: when the post-class checking
wave arrives, the cache is already fresh and the slow live path is rarely
needed. This also doubles as a daily session warm (the sync logs the pooled
session in), so evening taps skip paced logins too.

SAFETY MODEL
============
* LOW priority in the background lane — can never delay an interactive tap.
* Single-flight dedup_key `att_pred:<user>:<date>` collapses concurrent
  duplicates; an in-memory fired-set prevents re-firing within the same day.
* Respects the gateway's RESERVED_INTERACTIVE_SLOTS automatically (background
  callers admit below the interactive cap).
* Skips cleanly when: disabled via env, NITRIS circuit open, job queue
  saturated, DB unreachable, or the timetable has no classes today.
* Bounded per cycle (PREDICTIVE_SYNC_BATCH_SIZE) so even a mass timetable
  can never enqueue a herd in one instant.

NOTE ON DUPLICATES: a manual refresh inside the same window may double-sync
once per day per user. That is bounded, off-peak-ish, and strictly cheaper
than the old "everyone taps Refresh after class" storm.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Optional

from sqlalchemy import func, select

from app.config import IST, config
from app.db.models import TimetableEntry, User

logger = logging.getLogger(__name__)

# In-process fired-set for the current IST day: user_ids already synced today
# by this feature. Reset automatically at midnight IST / first fire of a new
# day. A restart within the window can re-fire once — bounded and harmless.
_fired_day_key: str = ""
_fired_user_ids: set[int] = set()


def _reset_if_new_day(now_ist: datetime) -> None:
    global _fired_day_key, _fired_user_ids
    key = now_ist.strftime("%Y%m%d")
    if key != _fired_day_key:
        _fired_day_key = key
        _fired_user_ids = set()


async def find_class_end_candidates(
    session,
    *,
    weekday: int,
    window_start: time,
    window_end: time,
    limit: int,
) -> list[int]:
    """Users whose LAST class of `weekday` ended within [start, end].

    MAX(end_time) per user over non-break entries for that weekday; HAVING
    keeps only users whose latest class falls inside the scan window. One
    indexed aggregate over ≤45 rows/user — cheap at 5k-user scale.
    """
    stmt = (
        select(TimetableEntry.user_id)
        .join(User, User.id == TimetableEntry.user_id)
        .where(
            User.credentials_valid == True,  # noqa: E712
            TimetableEntry.weekday == weekday,
            TimetableEntry.is_break == False,  # noqa: E712
        )
        .group_by(TimetableEntry.user_id)
        .having(func.max(TimetableEntry.end_time).between(window_start, window_end))
        .limit(limit)
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


def _scan_window(now_ist: datetime) -> Optional[tuple[time, time]]:
    """Compute the [start, end] TIME pair for this cycle's scan, or None when
    the window would wrap midnight (classes don't run past midnight; skipping
    one late-night cycle is always safe)."""
    target = now_ist - timedelta(minutes=config.PREDICTIVE_SYNC_DELAY_MINUTES)
    start_dt = target - timedelta(minutes=config.PREDICTIVE_SYNC_WINDOW_MINUTES)
    if target.date() != start_dt.date():
        return None
    return start_dt.time(), target.time()


async def run_class_end_sync_cycle() -> int:
    """One scheduler-cycle pass: find + enqueue due predictive syncs.

    Returns how many jobs were enqueued. NEVER raises into the scheduler loop
    beyond what the caller already guards (circuit/queue-depth checks live in
    run_scheduler_loop).
    """
    now_ist = datetime.now(IST)

    # Weekends have no classes in the table → query returns nothing naturally;
    # still short-circuit to keep idle cost at zero.
    if now_ist.weekday() >= 5:
        return 0

    window = _scan_window(now_ist)
    if window is None:
        return 0
    window_start, window_end = window

    _reset_if_new_day(now_ist)

    from app.db.database import get_db_session
    from app.nitris.job_queue import nitris_job_queue, Priority

    async with get_db_session() as session:
        candidates = await find_class_end_candidates(
            session,
            weekday=now_ist.weekday(),
            window_start=window_start,
            window_end=window_end,
            limit=config.PREDICTIVE_SYNC_BATCH_SIZE,
        )

    enqueued = 0
    for user_id in candidates:
        if user_id in _fired_user_ids:
            continue
        _fired_user_ids.add(user_id)
        try:
            await nitris_job_queue.enqueue(
                job_type="sync_attendance",
                user_id=user_id,
                priority=Priority.LOW,
                dedup_key=f"att_pred:{user_id}:{_fired_day_key}",
                payload={
                    "schedule_id": None,   # not TTL-scheduler work — no row bookkeeping
                    "module_name": "attendance",
                    "source": "class_end_predictive",
                },
            )
            enqueued += 1
        except Exception as e:
            # Queue full / circuit opened mid-batch — un-mark so a later cycle
            # inside the window can retry this user.
            _fired_user_ids.discard(user_id)
            logger.debug("predictive sync enqueue skipped user_id=%s: %r", user_id, e)
            break   # same backpressure semantics as the TTL claim loop

    if enqueued:
        logger.info(
            "Predictive class-end sync: fired %d attendance sync(es) "
            "(last class ended %s IST ±%dmin).",
            enqueued,
            window_end.strftime("%H:%M"),
            config.PREDICTIVE_SYNC_WINDOW_MINUTES,
        )
    return enqueued
