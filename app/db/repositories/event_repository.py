"""Event persistence repository using SQLAlchemy async sessions."""

import logging
from typing import Any
from sqlalchemy import select
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
