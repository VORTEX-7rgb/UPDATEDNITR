"""Periodic background sync worker and event notification dispatcher."""

import asyncio
import logging
import html
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

def normalize_to_utc(dt: datetime) -> datetime:
    """Normalize any datetime (aware or naive) to UTC timezone-aware datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

from app.utils import esc, safe_truncate


from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload

from app.db.database import get_db_session, is_db_connection_error
from app.db.models import User, SyncState, Event, EventType
from app.db.crypto import decrypt_password
from app.db.repositories.event_repository import EventRepository
from app.services.attendance_service import get_attendance_data
from app.services.snapshot_service import SnapshotService
from app.services.lock_service import user_lock

logger = logging.getLogger(__name__)

# Semaphores and intervals
SYNC_SEMAPHORE_LIMIT = 10
SYNC_INTERVAL_SECONDS = 7200  # Sync all users every 2 hours
DISPATCH_INTERVAL_SECONDS = 60  # Poll and send Telegram notifications every 60 seconds


async def wait_for_db_recovery(worker_name: str) -> None:
    """Blocks until the database connection is healthy, using exponential backoff."""
    backoff = 5
    attempt = 1
    has_failed = False
    
    while True:
        try:
            async with get_db_session() as session:
                # Issue a simple lightweight ping query
                await session.execute(select(1))
            if has_failed:
                logger.info("Database recovered. %s worker resumed.", worker_name)
            return
        except Exception as e:
            if is_db_connection_error(e):
                if not has_failed:
                    logger.error("Database connection lost. %s worker waiting for recovery...", worker_name)
                    has_failed = True
                
                logger.warning(
                    "Attempting reconnect for %s worker. Retry %d (waiting %ds)...",
                    worker_name, attempt, backoff
                )
                await asyncio.sleep(backoff)
                attempt += 1
                backoff = min(backoff * 2, 60)
            else:
                # If it's a non-connection error, propagate it immediately
                raise


async def prepare_inbox_sync(client, user_id):
    """Network phase for inbox sync. No open DB transaction, no DB writes.

    Returns (scraped_messages, detail_cache, existing_by_portal_id).
    """
    from app.nitris.parser import parse_messages_list_html, parse_message_detail_html
    from app.db.repositories.inbox_repository import InboxRepository

    # 1. Fetch + parse the raw messages list (network, no DB session held)
    list_html = await client.fetch_messages_list()
    scraped_messages = parse_messages_list_html(list_html)
    if not scraped_messages:
        logger.info("No messages found on portal for user_id=%s", user_id)
        return [], {}, {}

    await wait_for_db_recovery(f"Sync-Inbox-{user_id}")

    # 2. Short DB read of already-known portal messages. This connection is
    #    released before any slow network I/O below.
    portal_ids = [m["portal_message_id"] for m in scraped_messages]
    async with get_db_session() as session:
        inbox_repo = InboxRepository(session)
        existing = await inbox_repo.get_by_portal_message_ids(user_id, portal_ids)
    existing_by_id = {m.portal_message_id: m for m in existing}

    # 3. Pre-fetch detail pages for NEW non-historical messages over HTTP,
    #    outside any DB transaction (fixes pool starvation).
    detail_cache = {}
    for msg in scraped_messages:
        if msg["portal_message_id"] in existing_by_id:
            continue
        if msg["token"].startswith("postback:"):
            continue  # historical message: header only, no detail fetch
        try:
            detail_html = await client.fetch_message_detail(msg["token"])
            detail_cache[msg["portal_message_id"]] = parse_message_detail_html(detail_html)
        except Exception as e:
            logger.warning(
                "Failed to fetch message detail for portal_id=%s (user_id=%s): %r",
                msg["portal_message_id"], user_id, e,
            )
            detail_cache[msg["portal_message_id"]] = {"body": None, "attachment_url": None}

    return scraped_messages, detail_cache, existing_by_id


async def persist_inbox_sync(user_id, scraped_messages, detail_cache, existing_by_id):
    """DB write phase for inbox sync. One short transaction, no network I/O."""
    if not scraped_messages:
        return

    from app.db.repositories.inbox_repository import InboxRepository

    await wait_for_db_recovery(f"Sync-Inbox-Persist-{user_id}")

    async with get_db_session() as session:
        async with session.begin():
            inbox_repo = InboxRepository(session)
            event_repo = EventRepository(session)

            for msg in scraped_messages:
                normalized_scraped_sent_on = normalize_to_utc(msg["sent_on"])
                existing = existing_by_id.get(msg["portal_message_id"])

                if existing is None:
                    if msg["token"].startswith("postback:"):
                        new_msg = await inbox_repo.create_message(
                            user_id=user_id,
                            portal_message_id=msg["portal_message_id"],
                            token=msg["token"],
                            sender=msg["sender"],
                            subject=msg["subject"],
                            sent_on=normalized_scraped_sent_on,
                            body=None,
                            attachment_url=None,
                        )
                        logger.info(
                            "Sync registered historical message ID %s for user %s (header only)",
                            new_msg.portal_message_id, user_id,
                        )
                    else:
                        detail_data = detail_cache.get(
                            msg["portal_message_id"], {"body": None, "attachment_url": None}
                        )
                        new_msg = await inbox_repo.create_message(
                            user_id=user_id,
                            portal_message_id=msg["portal_message_id"],
                            token=msg["token"],
                            sender=msg["sender"],
                            subject=msg["subject"],
                            sent_on=normalized_scraped_sent_on,
                            body=detail_data.get("body"),
                            attachment_url=detail_data.get("attachment_url"),
                        )
                        if detail_data.get("body") is not None:
                            new_msg.body_fetched_at = datetime.now(timezone.utc)

                        await event_repo.create_event(
                            user_id=user_id,
                            event_type=EventType.NEW_MESSAGE_RECEIVED,
                            payload_json={
                                "message_id": new_msg.id,
                                "sender": new_msg.sender,
                                "subject": new_msg.subject,
                                "body_snippet": (new_msg.body[:150] + "..." if new_msg.body else ""),
                                "has_attachment": bool(new_msg.attachment_url),
                            },
                        )
                        logger.info(
                            "Sync detected new message ID %s for user %s",
                            new_msg.portal_message_id, user_id,
                        )
                else:
                    if existing.token != msg["token"]:
                        logger.info(
                            "Message token shifted from %s to %s for user %s. Updating token dynamically.",
                            existing.token, msg["token"], user_id,
                        )
                        existing.token = msg["token"]

                    existing_sent_on_utc = normalize_to_utc(existing.sent_on)
                    if existing.subject != msg["subject"] or existing_sent_on_utc != normalized_scraped_sent_on:
                        logger.info(
                            "Sync detected edited notice. Invalidating cache for token: %s",
                            msg["token"],
                        )
                        existing.subject = msg["subject"]
                        existing.sent_on = normalized_scraped_sent_on
                        existing.body = None
                        existing.body_fetched_at = None
                        existing.attachment_url = None
                        existing.attachment_cache_id = None
                        existing.is_read = False
                        await event_repo.create_event(
                            user_id=user_id,
                            event_type=EventType.MESSAGE_UPDATED,
                            payload_json={
                                "message_id": existing.id,
                                "sender": existing.sender,
                                "subject": existing.subject,
                            },
                        )


_event_dispatcher = None


async def init_event_dispatcher(bot: Bot) -> None:
    """Initialize the EventDispatcherService singleton.
    Call from main.py AFTER bot is created, BEFORE polling starts.
    Also starts the stale-claim reaper background task."""
    global _event_dispatcher
    from app.db.database import async_session_factory
    from app.services.event_dispatcher_service import EventDispatcherService
    _event_dispatcher = EventDispatcherService(
        bot=bot,
        session_factory=async_session_factory,
    )
    _event_dispatcher.start_reaper()
    logger.info("EventDispatcherService initialized (worker_id=%s)", _event_dispatcher.worker_id)


async def shutdown_event_dispatcher() -> None:
    """Graceful shutdown — call from main.py finally block."""
    global _event_dispatcher
    if _event_dispatcher is not None:
        await _event_dispatcher.stop()


def format_notification_message(event_type: str | EventType, payload: dict) -> str:
    """Backward-compatible helper delegating to event_dispatcher_service._format_notification."""
    from app.services.event_dispatcher_service import _format_notification
    text_content, _ = _format_notification(event_type, payload)
    return text_content


async def run_dispatch_worker(bot: Bot) -> None:
    """REWRITTEN — delegates to EventDispatcherService.

    The old implementation had the duplicate-notification bug:
      1. Fetch 50 unsent events
      2. Send each via bot.send_message()
      3. After ALL 50 sent, ONE bulk mark_sent
    Crash between 2 and 3 → all 50 stay sent=False → re-sent on restart →
    DUPLICATE NOTIFICATIONS.

    The new implementation (EventDispatcherService) uses:
      - Atomic claim via UPDATE...WHERE id IN (SELECT...) RETURNING
      - Per-event immediate mark_sent (crash window ~10ms, not ~30s)
      - Stale-claim reaper for crashed dispatcher recovery
      - FloodWait retry with retry_after sleep
      - Retry exhaustion → permanent_failure (terminal state)
      - Orphaned events (user deleted) → permanent_failure (not silent drop)
    """
    global _event_dispatcher
    while _event_dispatcher is None:
        await asyncio.sleep(0.5)
    await _event_dispatcher.run_forever()
