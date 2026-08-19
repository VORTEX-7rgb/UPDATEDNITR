"""Inbox persistence repository using SQLAlchemy async sessions."""

import logging
from typing import Optional, Any
from datetime import datetime
from sqlalchemy import select, update, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InboxMessage

logger = logging.getLogger(__name__)


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

    async def update_message_body(
        self, message_id: int, body: str, attachment_url: Optional[str]
    ) -> None:
        """Update a message's body content and attachment link after lazy-loading."""
        logger.debug("Updating body for InboxMessage ID: %s", message_id)
        stmt = (
            update(InboxMessage)
            .where(InboxMessage.id == message_id)
            .values(body=body, attachment_url=attachment_url)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def update_telegram_file_id(self, message_id: int, file_id: str) -> None:
        """Cache the successfully uploaded Telegram file ID for attachments."""
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
        """Fetch a page of latest messages for the inbox list menu."""
        stmt = (
            select(InboxMessage)
            .where(InboxMessage.user_id == user_id)
            .order_by(InboxMessage.sent_on.desc(), InboxMessage.id.desc())
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
            .order_by(InboxMessage.sent_on.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
