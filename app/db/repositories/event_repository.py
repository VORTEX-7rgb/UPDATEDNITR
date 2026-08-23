"""Event persistence repository using SQLAlchemy async sessions."""

import logging
from datetime import datetime
from typing import Any
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event

logger = logging.getLogger(__name__)


class EventRepository:
    """Manages database persistence for the Event model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_event(self, user_id: int, event_type: str, payload_json: dict[str, Any]) -> Event:
        """Create and store a new change detection event."""
        logger.debug("Creating event user_id=%s, type='%s'", user_id, event_type)
        
        event = Event(
            user_id=user_id,
            event_type=event_type,
            payload_json=payload_json,
            sent=False,
        )
        
        self.session.add(event)
        await self.session.flush()
        return event

    async def has_message_event(self, user_id: int, event_type: str, message_id: int) -> bool:
        """Check if an event of this type for this message_id already exists (prevents duplicate notification events)."""
        stmt = (
            select(Event)
            .where(
                Event.user_id == user_id,
                Event.event_type == event_type,
            )
        )
        try:
            result = await self.session.execute(stmt)
            scalars = result.scalars() if hasattr(result, "scalars") else None
            events = scalars.all() if (scalars and hasattr(scalars, "all") and callable(scalars.all)) else []
            for ev in events:
                if getattr(ev, "payload_json", None) and ev.payload_json.get("message_id") == message_id:
                    return True
        except Exception:
            pass
        return False

    async def get_unsent_events(self, limit: int = 100) -> list[Event]:
        """Fetch unsent events up to a specified limit, sorted by creation date."""
        stmt = (
            select(Event)
            .where(Event.sent == False)
            .order_by(Event.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def purge_terminal_batch(self, older_than: datetime, limit: int = 5000) -> int:
        """Delete ONE batch of terminal events (sent or permanent-failure) older than cutoff.

        Pending/in-flight events are never touched — only rows whose delivery
        state machine reached a terminal outcome AND aged past ``older_than``.
        Callers own transaction boundaries; run repeatedly until it returns
        less than ``limit`` to drain fully (see RetentionService).
        """
        if limit < 1:
            return 0
        victim_ids = (
            select(Event.id)
            .where(
                or_(Event.sent.is_(True), Event.permanent_failure.is_(True)),
                Event.created_at < older_than,
            )
            .limit(limit)
        )
        result = await self.session.execute(delete(Event).where(Event.id.in_(victim_ids)))
        deleted = result.rowcount or 0
        if deleted:
            logger.info("Retention: purged %d terminal event(s) this batch.", deleted)
        return deleted

