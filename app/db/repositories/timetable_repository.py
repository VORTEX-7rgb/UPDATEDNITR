"""TimetableEntry repository — atomic replace + ordered reads.

The replace operation is a single DELETE+INSERT inside one BEGIN/COMMIT, so
a sync never leaves the table in a half-written state. The "now/next" lookup
is a single SELECT (≤45 rows for a full week) — instant, no joins.

All times stored are wall-clock IST (HH:MM). The now/next algorithm in
app.services.now_next_service compares them against
`datetime.now(config.IST).time()` — both sides are IST wall-clock by
convention, so no tz-conversion is ever needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Iterable, Optional

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import config, IST
from app.db.models import TimetableEntry
from app.nitris.parser import TimetableSlot

logger = logging.getLogger(__name__)


class TimetableRepository:
    """Reads + atomically replaces a user's weekly timetable."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_user_timetable(self, user_id: int) -> list[TimetableEntry]:
        """Load all timetable entries for a user, ordered by (weekday, period).

        A typical student week has ≤ 45 entries — this is a single indexed
        SELECT that returns instantly. Used by /now and /timetable display.
        """
        stmt = (
            select(TimetableEntry)
            .where(TimetableEntry.user_id == user_id)
            .order_by(TimetableEntry.weekday, TimetableEntry.period_index)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_last_synced_at(self, user_id: int) -> Optional[datetime]:
        """Return the most recent `synced_at` across all the user's entries.

        Used to display "Last refreshed: 20 Aug 14:32 IST" in the /timetable
        and /now responses so the user knows data freshness.
        """
        stmt = select(func.max(TimetableEntry.synced_at)).where(
            TimetableEntry.user_id == user_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def replace_user_timetable(
        self,
        user_id: int,
        slots: Iterable[TimetableSlot],
        synced_at: Optional[datetime] = None,
    ) -> int:
        """Atomically replace the user's full timetable with `slots`.

        Single transaction:
            DELETE existing rows for this user
            INSERT all new rows (parsed fresh from NITRIS)
            COMMIT

        Returns the number of rows inserted.

        Mirrors the compare-and-swap discipline of QPaperService: no partial
        state, no orphaned rows, no read-modify-write race window.
        """
        if synced_at is None:
            # Always stamp with IST-aware UTC tz — never datetime.now() bare.
            synced_at = datetime.now(IST)

        slots_list = list(slots)

        # DELETE then INSERT, in one transaction (caller controls begin/commit
        # — typically `async with session.begin():`).
        await self.session.execute(
            delete(TimetableEntry).where(TimetableEntry.user_id == user_id)
        )

        for slot in slots_list:
            entry = TimetableEntry(
                user_id=user_id,
                weekday=slot.weekday,
                period_index=slot.period_index,
                start_time=_parse_time(slot.start_time),
                end_time=_parse_time(slot.end_time),
                subject_code=slot.subject,
                room=slot.room or "",
                is_break=slot.is_break,
                subject_name=slot.subject_name or "",
                course_type=slot.course_type or "",
                synced_at=synced_at,
            )
            self.session.add(entry)

        logger.info(
            "Replaced timetable for user_id=%d: %d entries (synced_at=%s)",
            user_id, len(slots_list), synced_at.isoformat(),
        )
        return len(slots_list)


def _parse_time(hhmm: str) -> time:
    """Parse a "HH:MM" string into a datetime.time. Strict format — no guessing.

    Raises ValueError on malformed input so a bad NITRIS response fails loudly
    instead of silently producing 00:00.
    """
    return datetime.strptime(hhmm, "%H:%M").time()
