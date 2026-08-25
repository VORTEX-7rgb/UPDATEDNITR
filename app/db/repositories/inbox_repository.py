"""Inbox persistence repository using SQLAlchemy async sessions."""

import logging
from typing import Optional, Any
from datetime import datetime, timezone
from sqlalchemy import select, update, func, or_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InboxMessage

logger = logging.getLogger(__name__)

# _parse_inbox_sent_on (app/nitris/parser.py) falls back to a fixed sentinel
# (2000-01-01 UTC after normalize_to_utc) whenever NITRIS renders a date string
# it doesn't recognize (e.g. a relative label like "Today" on the notification
# dropdown). The fallback must stay deterministic (it feeds _content_portal_id
# hashing), so recency ordering can NOT trust sent_on alone: a sentinel-dated
# message would sort below every real message forever and vanish from /inbox
# even though it was just synced and push-notified (incident 2026-08-25).
# Instead we order by an effective-recency key: portal date for well-dated
# rows, row creation time for sentinel-dated rows. created_at is assigned at
# INSERT time and is immune to upstream date-parsing failures.
_SENTINEL_CUTOFF = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _effective_recency():
    """SQL expression: well-dated rows sort by portal date, sentinel-dated rows by insertion time."""
    return case(
        (InboxMessage.sent_on >= _SENTINEL_CUTOFF, InboxMessage.sent_on),
        else_=InboxMessage.created_at,
    )


class InboxRepository:
    """Manages database persistence for the InboxMessage model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_message(
        self,
        user_id: int,
        portal_message_id: int,
        token: str,
        sender: str,
        subject: str,
        sent_on: datetime,
        body: Optional[str] = None,
        attachment_url: Optional[str] = None,
    ) -> InboxMessage:
        """Create and store a new message header/record."""
        logger.debug("Creating inbox message for user_id=%s, token='%s'", user_id, token)
        
        message = InboxMessage(
            user_id=user_id,
            portal_message_id=portal_message_id,
            token=token,
            sender=sender,
            subject=subject,
            body=body,
            attachment_url=attachment_url,
            is_read=False,
            sent_on=sent_on,
        )
        
        self.session.add(message)
        await self.session.flush()
        return message

    async def get_by_token(self, user_id: int, token: str) -> Optional[InboxMessage]:
        """Fetch a specific message by its unique user token."""
        stmt = select(InboxMessage).where(InboxMessage.user_id == user_id, InboxMessage.token == token)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_portal_message_id(self, user_id: int, portal_message_id: int) -> Optional[InboxMessage]:
        """Fetch a specific message by its unique user and portal message ID.
        
        Uses limit(1) to avoid MultipleResultsFound in case of pre-existing duplicates.
        """
        stmt = (
            select(InboxMessage)
            .where(
                InboxMessage.user_id == user_id,
                InboxMessage.portal_message_id == portal_message_id
            )
            .order_by(InboxMessage.id.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_portal_message_ids(
        self, user_id: int, portal_message_ids: list[int]
    ) -> list[InboxMessage]:
        """Fetch all messages matching a set of portal message IDs for a user."""
        if not portal_message_ids:
            return []
        stmt = (
            select(InboxMessage)
            .where(
                InboxMessage.user_id == user_id,
                InboxMessage.portal_message_id.in_(portal_message_ids),
            )
            .order_by(InboxMessage.id.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def has_any_messages(self, user_id: int) -> bool:
        """True if the user has AT LEAST ONE inbox row (cheap LIMIT-1 probe).

        This is the authoritative "has this inbox ever been populated" check —
        NOT the same as "did any of the currently-scraped portal IDs match".
        Used by persist_inbox_sync's implicit-baseline guard: whatever sync
        path populates a brand-new inbox (onboarding retry, scheduler tick
        that fired early, user-tapped refresh), the FIRST population is the
        student's historical backlog and must never notify.
        """
        stmt = select(InboxMessage.id).where(InboxMessage.user_id == user_id).limit(1)
        result = await self.session.execute(stmt)
        return result.first() is not None

    async def update_message_body(
        self, message_id: int, body: str, attachment_url: Optional[str]
    ) -> None:
        """Update a message's body content, attachment link, and timestamp after lazy-loading."""
        logger.debug("Updating body for InboxMessage ID: %s", message_id)
        stmt = (
            update(InboxMessage)
            .where(InboxMessage.id == message_id)
            .values(
                body=body,
                attachment_url=attachment_url,
                body_fetched_at=func.now(),
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def link_attachment_cache(
        self, message_id: int, attachment_cache_id: int
    ) -> None:
        """Link an InboxMessage to a global AttachmentCache row."""
        logger.debug(
            "Linking InboxMessage ID %s to AttachmentCache ID %s",
            message_id,
            attachment_cache_id,
        )
        stmt = (
            update(InboxMessage)
            .where(InboxMessage.id == message_id)
            .values(attachment_cache_id=attachment_cache_id)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def update_telegram_file_id(self, message_id: int, file_id: str) -> None:
        """Cache the successfully uploaded Telegram file ID for attachments (legacy)."""
        logger.debug("Caching Telegram file ID for InboxMessage ID: %s", message_id)
        stmt = (
            update(InboxMessage)
            .where(InboxMessage.id == message_id)
            .values(telegram_file_id=file_id)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def mark_as_read(self, message_id: int) -> None:
        """Mark a single message as read."""
        logger.debug("Marking InboxMessage ID %s as read", message_id)
        stmt = (
            update(InboxMessage)
            .where(InboxMessage.id == message_id)
            .values(is_read=True)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def mark_all_as_read(self, user_id: int) -> None:
        """Bulk update all user messages to is_read = True."""
        logger.info("Marking all messages as read for User ID: %s", user_id)
        stmt = (
            update(InboxMessage)
            .where(InboxMessage.user_id == user_id, InboxMessage.is_read == False)
            .values(is_read=True)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def get_unread_count(self, user_id: int) -> int:
        """Return the total count of unread items."""
        stmt = (
            select(func.count(InboxMessage.id))
            .where(InboxMessage.user_id == user_id, InboxMessage.is_read == False)
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def get_latest_messages(self, user_id: int, offset: int = 0, limit: int = 5) -> list[InboxMessage]:
        """Fetch a page of latest messages for the inbox list menu.

        Ordered by effective recency (portal sent_on when parseable, else
        insertion time), NOT raw sent_on — a sentinel-dated message must
        surface by sync recency instead of sinking past the last page.
        """
        stmt = (
            select(InboxMessage)
            .where(InboxMessage.user_id == user_id)
            .order_by(_effective_recency().desc(), InboxMessage.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_messages(self, user_id: int, query: str, limit: int = 5) -> list[InboxMessage]:
        """Perform fast case-insensitive ILIKE search on subject, sender, or body."""
        logger.info("Searching messages for User ID %s with query: '%s'", user_id, query)
        q = f"%{query}%"
        stmt = (
            select(InboxMessage)
            .where(
                InboxMessage.user_id == user_id,
                or_(
                    InboxMessage.subject.ilike(q),
                    InboxMessage.sender.ilike(q),
                    InboxMessage.body.ilike(q),
                ),
            )
            .order_by(_effective_recency().desc(), InboxMessage.id.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
