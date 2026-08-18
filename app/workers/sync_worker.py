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


async def sync_messages_for_user(
    user_id: int, 
    roll_number: str, 
    password: str, 
    bot: Optional[Any] = None, 
    client: Optional[Any] = None
) -> None:
    """Sync inbox messages for a single user using an active NitrisClient session."""
    from app.nitris.client import NitrisClient
    from app.nitris.parser import parse_messages_list_html, parse_message_detail_html
    from app.db.repositories.inbox_repository import InboxRepository
    from app.db.repositories.event_repository import EventRepository
    
    should_close = False
    if client is None:
        client = NitrisClient()
        should_close = True
        await client.login(roll_number, password)
        
    try:
        # 1. Fetch raw messages list HTML
        list_html = await client.fetch_messages_list()
        scraped_messages = parse_messages_list_html(list_html)
        
        if not scraped_messages:
            logger.info("No messages found on portal for user_id=%s", user_id)
            return
            
        await wait_for_db_recovery(f"Sync-Inbox-{user_id}")
        async with get_db_session() as session:
            async with session.begin():
                inbox_repo = InboxRepository(session)
                event_repo = EventRepository(session)
                
                # Check each scraped message against database
                for msg in scraped_messages:
                    # Normalize scraped naive/aware sent_on to UTC
                    normalized_scraped_sent_on = normalize_to_utc(msg["sent_on"])
                    
                    # Uniqueness query using stable portal_message_id!
                    existing = await inbox_repo.get_by_portal_message_id(user_id, msg["portal_message_id"])
                    
                    if not existing:
                        # 2. Brand new message!
                        if msg["token"].startswith("postback:"):
                            # Older historical message: store header only, do NOT fetch detail/notify
                            new_msg = await inbox_repo.create_message(
                                user_id=user_id,
                                portal_message_id=msg["portal_message_id"],
                                token=msg["token"],
                                sender=msg["sender"],
                                subject=msg["subject"],
                                sent_on=normalized_scraped_sent_on,
                                body=None,
                                attachment_url=None
                            )
                            logger.info("Sync registered historical message ID %s for user %s (header only)", new_msg.portal_message_id, user_id)
                        else:
                            # Newly arrived message: fetch details live and emit notification event
                            detail_html = await client.fetch_message_detail(msg["token"])
                            detail_data = parse_message_detail_html(detail_html)
                            
                            new_msg = await inbox_repo.create_message(
                                user_id=user_id,
                                portal_message_id=msg["portal_message_id"],
                                token=msg["token"],
                                sender=msg["sender"],
                                subject=msg["subject"],
                                sent_on=normalized_scraped_sent_on,
                                body=detail_data["body"],
                                attachment_url=detail_data["attachment_url"]
                            )
                            
                            await event_repo.create_event(
                                user_id=user_id,
                                event_type=EventType.NEW_MESSAGE_RECEIVED,
                                payload_json={
                                    "message_id": new_msg.id,
                                    "sender": new_msg.sender,
                                    "subject": new_msg.subject,
                                    "body_snippet": new_msg.body[:150] + "..." if new_msg.body else "",
                                    "has_attachment": bool(new_msg.attachment_url)
                                }
                            )
                            logger.info("Sync detected new message ID %s for user %s", new_msg.portal_message_id, user_id)
                        
                    else:
                        # 3. Message exists. Update token dynamically if it shifted to prevent duplicates and maintain callback functionality.
                        if existing.token != msg["token"]:
                            logger.info("Message token shifted from %s to %s for user %s. Updating token dynamically.", existing.token, msg["token"], user_id)
                            existing.token = msg["token"]
                            
                        # Normalize existing timestamp to UTC before comparing
                        existing_sent_on_utc = normalize_to_utc(existing.sent_on)
                        
                        # Check for edits safely in UTC
                        if existing.subject != msg["subject"] or existing_sent_on_utc != normalized_scraped_sent_on:
                            logger.info("Sync detected edited notice. Invalidating cache for token: %s", msg["token"])
                            existing.subject = msg["subject"]
                            existing.sent_on = normalized_scraped_sent_on
                            existing.body = None
                            existing.attachment_url = None
                            existing.is_read = False
                            
                            await event_repo.create_event(
                                user_id=user_id,
                                event_type=EventType.MESSAGE_UPDATED,
                                payload_json={
                                    "message_id": existing.id,
                                    "sender": existing.sender,
                                    "subject": existing.subject,
                                }
                            )
                            
    except Exception as e:
        logger.error("Failed to sync messages for User ID %d: %r", user_id, e)
    finally:
        if should_close:
            await client.close()


async def sync_user_data(user_id: int, roll_number: str, encrypted_pass: str, semaphore: asyncio.Semaphore, bot: Optional[Any] = None) -> None:
    """Sync attendance and inbox messages for a single user with isolated failure tracking and performance telemetry."""
    async with semaphore:
        if not await user_lock.acquire(user_id):
            logger.info("Sync already in progress for User ID %d (Roll: %s). Skipping background sync cycle.", user_id, roll_number)
            return
            
        try:
            import time
            start_sync = time.time()
            
            login_time = 0.0
            attendance_fetch_time = 0.0
            inbox_fetch_time = 0.0
            db_write_time = 0.0
            
            now = datetime.now(timezone.utc)
            logger.info("Starting background sync for Roll: %s (User ID: %d)", roll_number, user_id)
            
            # 1. Decrypt user password
            try:
                password = decrypt_password(encrypted_pass)
            except Exception as e:
                logger.error("Failed to decrypt password for User ID %d: %r", user_id, e)
                await _update_sync_state(user_id, success=False, error_msg=f"Decryption failed: {str(e)}", sync_time=now)
                return

            from app.nitris.client import NitrisClient

            # ONE authenticated session per user sync cycle
            client = NitrisClient()
            login_success = False
            attendance_success = False
            data = None

            try:
                logger.info("Single login path: authenticating session for User ID %d...", user_id)
                login_start = time.time()
                await client.login(roll_number, password)
                login_time = time.time() - login_start
                login_success = True
            except Exception as e:
                logger.error("Single login session failed for Roll %s: %r", roll_number, e)
                metrics_fail = {
                    "login_time": round(login_time, 2),
                    "attendance_fetch_time": 0.0,
                    "inbox_fetch_time": 0.0,
                    "db_write_time": 0.0,
                    "full_sync_time": round(time.time() - start_sync, 2)
                }
                await _update_sync_state(user_id, success=False, error_msg=f"Login failed: {str(e)}", sync_time=now, metrics=metrics_fail)

            try:
                if login_success:
                    # 2. Fetch latest attendance using reuse client instance
                    try:
                        att_start = time.time()
                        data = await get_attendance_data(roll_number, password, client=client)
                        attendance_fetch_time = time.time() - att_start
                        attendance_success = True
                    except Exception as e:
                        logger.warning("Scraper failed to fetch attendance for Roll %s: %r", roll_number, e)
                        metrics_fail = {
                            "login_time": round(login_time, 2),
                            "attendance_fetch_time": round(attendance_fetch_time, 2),
                            "inbox_fetch_time": 0.0,
                            "db_write_time": 0.0,
                            "full_sync_time": round(time.time() - start_sync, 2)
                        }
                        await _update_sync_state(user_id, success=False, error_msg=f"Scraper failed: {str(e)}", sync_time=now, metrics=metrics_fail)

                    # 3. Fetch latest inbox messages using the SAME active client session
                    try:
                        inbox_start = time.time()
                        await sync_messages_for_user(user_id, roll_number, password, bot, client=client)
                        inbox_fetch_time = time.time() - inbox_start
                    except Exception as e:
                        logger.error("Inbox sync failed for Roll %s: %r", roll_number, e)

                    # QP pre-cache loop REMOVED — acquisition is now lazy via QPaperService
                    # when a student taps "Download". See app/services/qpaper_service.py.
            finally:
                await client.close()
                logger.info("Closed NitrisClient session for Roll %s (User ID: %d)", roll_number, user_id)

            if not attendance_success or data is None:
                return

            # 4. Save snapshot & events atomically in a transaction
            try:
                await wait_for_db_recovery(f"Sync-User-{user_id}")
                db_start = time.time()
                async with get_db_session() as session:
                    async with session.begin():
                        snapshot_service = SnapshotService(session)
                        await snapshot_service.create_snapshot_if_changed(
                            user_id=user_id,
                            module_name="attendance",
                            attendance_result=data
                        )
                db_write_time = time.time() - db_start
                
                full_sync_time = time.time() - start_sync
                
                metrics = {
                    "login_time": round(login_time, 2),
                    "attendance_fetch_time": round(attendance_fetch_time, 2),
                    "inbox_fetch_time": round(inbox_fetch_time, 2),
                    "db_write_time": round(db_write_time, 2),
                    "full_sync_time": round(full_sync_time, 2)
                }
                
                # 5. Mark success in health tracker and save metrics
                await _update_sync_state(user_id, success=True, error_msg=None, sync_time=now, metrics=metrics)
                
                logger.info(
                    "[METRICS] User=%s LOGIN=%.2fs ATTENDANCE=%.2fs INBOX=%.2fs DB=%.2fs TOTAL=%.2fs",
                    roll_number, login_time, attendance_fetch_time, inbox_fetch_time, db_write_time, full_sync_time
                )
                logger.info("Successfully completed background sync for Roll: %s", roll_number)

            except Exception as e:
                logger.error("Failed to persist snapshot/events in database for User ID %d: %r", user_id, e)
                metrics_db_fail = {
                    "login_time": round(login_time, 2),
                    "attendance_fetch_time": round(attendance_fetch_time, 2),
                    "inbox_fetch_time": round(inbox_fetch_time, 2),
                    "db_write_time": round(time.time() - db_start, 2),
                    "full_sync_time": round(time.time() - start_sync, 2)
                }
                await _update_sync_state(user_id, success=False, error_msg=f"Database save failed: {str(e)}", sync_time=now, metrics=metrics_db_fail)
        finally:
            await user_lock.release(user_id)


async def _update_sync_state(user_id: int, success: bool, error_msg: Optional[str], sync_time: datetime, metrics: Optional[dict] = None) -> None:
    """Update or create the SyncState tracking record for a user."""
    try:
        await wait_for_db_recovery(f"SyncState-{user_id}")
        async with get_db_session() as session:
            async with session.begin():
                stmt = select(SyncState).where(SyncState.user_id == user_id)
                res = await session.execute(stmt)
                state = res.scalar_one_or_none()
                
                if not state:
                    state = SyncState(user_id=user_id, failure_count=0)
                    session.add(state)
                
                state.last_sync = sync_time
                if metrics:
                    state.last_metrics = metrics
                if success:
                    state.last_success = sync_time
                    state.last_error = None
                    state.failure_count = 0
                else:
                    state.last_error = error_msg[:1000] if error_msg else "Unknown error"
                    state.failure_count = (state.failure_count or 0) + 1
    except Exception as e:
        logger.error("Failed to update SyncState record for User ID %d: %r", user_id, e)


async def run_sync_worker(bot: Bot) -> None:
    """Periodically syncs all registered users using bounded concurrency."""
    logger.info("Background Sync Worker initialized.")
    semaphore = asyncio.Semaphore(SYNC_SEMAPHORE_LIMIT)

    while True:
        sync_completed_successfully = False
        try:
            # 1. Wait until DB is healthy
            await wait_for_db_recovery("Sync")
            
            logger.info("Beginning background attendance sync cycle for all users...")
            
            # Fetch all active users
            async with get_db_session() as session:
                stmt = select(User)
                res = await session.execute(stmt)
                users = res.scalars().all()
                
            if not users:
                logger.info("No registered users found. Skipping sync cycle.")
            else:
                logger.info("Syncing %d users with concurrency limit of %d...", len(users), SYNC_SEMAPHORE_LIMIT)
                
                # Perform bounded concurrent sync using asyncio.gather
                tasks = [
                    sync_user_data(
                        user_id=u.id,
                        roll_number=u.roll_number,
                        encrypted_pass=u.encrypted_password,
                        semaphore=semaphore,
                        bot=bot
                    )
                    for u in users
                ]
                await asyncio.gather(*tasks)
                logger.info("Completed attendance sync cycle for all users.")
            
            sync_completed_successfully = True

        except Exception as e:
            if is_db_connection_error(e):
                logger.error("Database connection lost during sync worker execution. Retrying immediately...", exc_info=True)
            else:
                logger.error("Unexpected error in background sync loop: %r", e, exc_info=True)
                await asyncio.sleep(60)
            
        if sync_completed_successfully:
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
# Singleton — set by init_event_dispatcher(bot) from main.py
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
