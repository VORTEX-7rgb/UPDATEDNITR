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

                    # 4. Proactive Background Pre-Caching for Question Papers
                    is_mock = (
                        "Mock" in client.__class__.__name__ or
                        (hasattr(client, "fetch_question_papers") and "Mock" in client.fetch_question_papers.__class__.__name__)
                    )
                    if data and data.records and not is_mock:
                        try:
                            logger.info("Proactively pre-caching question papers for Roll %s...", roll_number)
                            from app.bot.telegram import YEAR_MAP
                            from app.services.examination_service import ExaminationService
                            from app.db.models import QuestionPaperCache
                            
                            async with get_db_session() as session:
                                for record in data.records:
                                    subject_code = record.subject_code
                                    clean_code = subject_code.upper().replace(" ", "").replace("-", "").replace("_", "")
                                    if not clean_code:
                                        continue
                                    
                                    for year_code, full_year_str in YEAR_MAP.items():
                                        stmt = (
                                            select(QuestionPaperCache)
                                            .where(QuestionPaperCache.subject_code == clean_code)
                                            .where(QuestionPaperCache.academic_year == full_year_str)
                                        )
                                        res = await session.execute(stmt)
                                        if res.first() is not None:
                                            continue
                                        
                                        logger.info("Pre-cache miss for Subject: %s, Year: %s. Syncing...", clean_code, full_year_str)
                                        exam_service = ExaminationService(session)
                                        await exam_service.sync_subject_papers_metadata(
                                            username=roll_number,
                                            password=password,
                                            academic_year=full_year_str,
                                            subject_code=clean_code,
                                            client=client
                                        )
                                        await session.commit()
                                        await asyncio.sleep(0.5)
                        except Exception as e:
                            logger.error("Proactive pre-caching failed for Roll %s: %r", roll_number, e)
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


def format_notification_message(event_type: EventType | str, payload: dict) -> str:
    """Build highly aesthetic HTML messages for different event types."""
    sub_name = esc(payload.get("subject_name") or payload.get("subject_code", "Unknown"))
    sub_code = esc(payload.get("subject_code", ""))
    
    if event_type == EventType.NEW_SUBJECT_ADDED:
        return (
            f"📚 <b>New Subject Registered</b>\n\n"
            f"🎓 Course: <b>{sub_name}</b> ({sub_code})\n"
            f"👨‍🏫 Faculty: <b>{esc(payload.get('faculty', 'N/A'))}</b>\n"
            f"📊 Initial Stats: TC: {esc(payload.get('tc', '0'))} | UA: {esc(payload.get('ua', '0'))} | OA: 0\n"
        )
        
    elif event_type == EventType.ATTENDANCE_UPDATED:
        msg = f"📊 <b>Attendance Update Detected</b>\n\n🔸 Subject: <b>{sub_name}</b> ({sub_code})\n📈 Class Stats changed:\n"
        changes = payload.get("changes", {})
        for field, delta in changes.items():
            name = field.upper()
            msg += f"  • {name}: <b>{esc(delta.get('old'))} ➡️ {esc(delta.get('new'))}</b>\n"
        return msg
        
    elif event_type == EventType.NEW_ABSENCE_DETECTED:
        return (
            f"🚨 <b>New Absence Logged!</b>\n\n"
            f"🔸 Subject: <b>{sub_name}</b> ({sub_code})\n"
            f"⚠️ You were marked <b>ABSENT</b>!\n"
            f"📉 Unauthorized Absences: <b>{esc(payload.get('old_ua', '0'))} ➡️ {esc(payload.get('new_ua', '0'))}</b>\n"
            f"📊 Current Stats: TC: {esc(payload.get('total_classes', '0'))} | UA: {esc(payload.get('new_ua', '0'))}\n\n"
            f"<i>Keep an eye on your attendance to avoid debarment!</i>"
        )
        
    elif event_type == EventType.NEW_MESSAGE_RECEIVED:
        attach_str = "📎 Attachment included" if payload.get("has_attachment") else "No attachments"
        body_snippet = safe_truncate(esc(payload.get('body_snippet')), 150)
        return (
            f"📩 <b>New Message Received!</b>\n\n"
            f"👤 From: <b>{esc(payload.get('sender'))}</b>\n"
            f"📌 Subject: <b>{esc(payload.get('subject'))}</b>\n\n"
            f"<i>\"{body_snippet}\"</i>\n\n"
            f"💡 {attach_str}\n"
            f"👉 Use /latest or open your Inbox to read the full notice!"
        )
        
    elif event_type == EventType.MESSAGE_UPDATED:
        return (
            f"🔄 <b>Notice Updated!</b>\n\n"
            f"👤 From: <b>{esc(payload.get('sender'))}</b>\n"
            f"📌 Subject: <b>{esc(payload.get('subject'))}</b>\n\n"
            f"⚠️ The university has updated this notice. Open your Inbox to read the revised version."
        )
        
    return f"ℹ️ <b>System Alert</b>\n\nSubject <b>{sub_name}</b> ({sub_code}) changed: {esc(payload)}"


async def run_dispatch_worker(bot: Bot) -> None:
    """Periodically queries unsent events and dispatches beautiful Telegram alerts in batches."""
    logger.info("Notification Dispatcher Worker initialized.")

    while True:
        dispatch_completed_successfully = False
        try:
            # 1. Wait until DB is healthy
            await wait_for_db_recovery("Dispatcher")
            
            # 2. Query for unsent events including user relationship
            async with get_db_session() as session:
                stmt = (
                    select(Event)
                    .options(selectinload(Event.user))
                    .where(Event.sent == False)
                    .order_by(Event.id.asc())
                    .limit(50)
                )
                res = await session.execute(stmt)
                unsent_events = res.scalars().all()

            if unsent_events:
                logger.info("Found %d unsent events to dispatch.", len(unsent_events))
                
                successful_ids = []
                for event in unsent_events:
                    user = event.user
                    if not user:
                        logger.error("Orphaned Event found: ID %d has no associated user.", event.id)
                        successful_ids.append(event.id)  # Mark as sent so we clear it
                        continue

                    # Construct message
                    msg = format_notification_message(event.event_type, event.payload_json)
                    
                    # Custom Keyboard for message notifications
                    reply_markup = None
                    if event.event_type in (EventType.NEW_MESSAGE_RECEIVED, EventType.MESSAGE_UPDATED):
                        from aiogram.utils.keyboard import InlineKeyboardBuilder
                        from aiogram import types
                        builder = InlineKeyboardBuilder()
                        msg_id = event.payload_json.get("message_id")
                        builder.row(types.InlineKeyboardButton(text="📖 Read Full Notice", callback_data=f"msg_{msg_id}"))
                        reply_markup = builder.as_markup()
                        
                    # Dispatch to Telegram Bot
                    success = False
                    try:
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=msg,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                        success = True
                    except TelegramForbiddenError:
                        logger.warning("User %d has blocked the bot. Marking event as sent to clear queue.", user.telegram_id)
                        success = True  # Treat as success so we don't block queue
                    except TelegramAPIError as e:
                        logger.error("Telegram API error sending to %d: %r", user.telegram_id, e)
                        # We don't mark as success if it is a transient API failure (e.g. rate limits)
                        if "chat not found" in str(e).lower() or "user is deactivated" in str(e).lower():
                            success = True  # Clean up dead queues
                    except Exception as e:
                        logger.error("Unexpected error sending message to telegram_id %d: %r", user.telegram_id, e)
                    
                    if success:
                        successful_ids.append(event.id)
                        logger.info("Successfully dispatched event ID %d to telegram_id=%d", event.id, user.telegram_id)
                
                # Bulk update status in DB
                if successful_ids:
                    await wait_for_db_recovery("Dispatcher")
                    try:
                        async with get_db_session() as session:
                            async with session.begin():
                                event_repo = EventRepository(session)
                                await event_repo.mark_sent(successful_ids)
                        logger.info("Successfully marked %d events as sent in database", len(successful_ids))
                    except Exception as e:
                        logger.error("Failed to mark events as sent in database: %r", e)
            
            dispatch_completed_successfully = True

        except Exception as e:
            if is_db_connection_error(e):
                logger.error("Database connection lost during dispatcher worker execution. Retrying immediately...", exc_info=True)
            else:
                logger.error("Unexpected error in background notification dispatcher: %r", e, exc_info=True)
                await asyncio.sleep(10)
            
        if dispatch_completed_successfully:
            await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)
