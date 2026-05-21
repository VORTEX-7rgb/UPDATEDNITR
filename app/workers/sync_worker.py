"""Periodic background sync worker and event notification dispatcher."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import get_db_session, is_db_connection_error
from app.db.models import User, SyncState, Event
from app.db.crypto import decrypt_password
from app.services.attendance_service import get_attendance_data
from app.services.snapshot_service import SnapshotService

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


async def sync_user_data(user_id: int, roll_number: str, encrypted_pass: str, semaphore: asyncio.Semaphore) -> None:
    """Sync attendance data for a single user with failure isolation and health tracking."""
    async with semaphore:
        now = datetime.now(timezone.utc)
        logger.info("Starting background sync for Roll: %s (User ID: %d)", roll_number, user_id)
        
        # 1. Decrypt user password
        try:
            password = decrypt_password(encrypted_pass)
        except Exception as e:
            logger.error("Failed to decrypt password for User ID %d: %r", user_id, e)
            await _update_sync_state(user_id, success=False, error_msg=f"Decryption failed: {str(e)}", sync_time=now)
            return

        # 2. Fetch latest attendance from NITRIS
        try:
            data = await get_attendance_data(roll_number, password)
        except Exception as e:
            logger.warning("Scraper failed to fetch data for Roll %s: %r", roll_number, e)
            await _update_sync_state(user_id, success=False, error_msg=f"Scraper failed: {str(e)}", sync_time=now)
            return

        # 3. Save snapshot & events atomically in a transaction
        try:
            await wait_for_db_recovery(f"Sync-User-{user_id}")
            async with get_db_session() as session:
                async with session.begin():
                    snapshot_service = SnapshotService(session)
                    await snapshot_service.create_snapshot_if_changed(
                        user_id=user_id,
                        module_name="attendance",
                        attendance_result=data
                    )
            
            # 4. Mark success in health tracker
            await _update_sync_state(user_id, success=True, error_msg=None, sync_time=now)
            logger.info("Successfully completed background sync for Roll: %s", roll_number)

        except Exception as e:
            logger.error("Failed to persist snapshot/events in database for User ID %d: %r", user_id, e)
            await _update_sync_state(user_id, success=False, error_msg=f"Database save failed: {str(e)}", sync_time=now)


async def _update_sync_state(user_id: int, success: bool, error_msg: Optional[str], sync_time: datetime) -> None:
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
                        semaphore=semaphore
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


def format_notification_message(event_type: str, payload: dict) -> str:
    """Build highly aesthetic HTML messages for different event types."""
    sub_name = payload.get("subject_name") or payload.get("subject_code", "Unknown")
    sub_code = payload.get("subject_code", "")
    
    if event_type == "new_subject_added":
        return (
            f"📚 <b>New Subject Registered</b>\n\n"
            f"🎓 Course: <b>{sub_name}</b> ({sub_code})\n"
            f"👨‍🏫 Faculty: <b>{payload.get('faculty', 'N/A')}</b>\n"
            f"📊 Initial Stats: TC: {payload.get('tc', '0')} | UA: {payload.get('ua', '0')} | OA: 0\n"
        )
        
    elif event_type == "attendance_updated":
        msg = f"📊 <b>Attendance Update Detected</b>\n\n🔸 Subject: <b>{sub_name}</b> ({sub_code})\n📈 Class Stats changed:\n"
        changes = payload.get("changes", {})
        for field, delta in changes.items():
            name = field.upper()
            msg += f"  • {name}: <b>{delta.get('old')} ➡️ {delta.get('new')}</b>\n"
        return msg
        
    elif event_type == "new_absence_detected":
        return (
            f"🚨 <b>New Absence Logged!</b>\n\n"
            f"🔸 Subject: <b>{sub_name}</b> ({sub_code})\n"
            f"⚠️ You were marked <b>ABSENT</b>!\n"
            f"📉 Unauthorized Absences: <b>{payload.get('old_ua', '0')} ➡️ {payload.get('new_ua', '0')}</b>\n"
            f"📊 Current Stats: TC: {payload.get('total_classes', '0')} | UA: {payload.get('new_ua', '0')}\n\n"
            f"<i>Keep an eye on your attendance to avoid debarment!</i>"
        )
        
    return f"ℹ️ <b>System Alert</b>\n\nSubject <b>{sub_name}</b> ({sub_code}) changed: {payload}"


async def run_dispatch_worker(bot: Bot) -> None:
    """Periodically queries unsent events and dispatches beautiful Telegram alerts."""
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
                
                for event in unsent_events:
                    user = event.user
                    if not user:
                        logger.error("Orphaned Event found: ID %d has no associated user.", event.id)
                        # Mark as sent so we don't try again
                        await wait_for_db_recovery("Dispatcher")
                        async with get_db_session() as session:
                            async with session.begin():
                                db_event = await session.get(Event, event.id)
                                if db_event:
                                    db_event.sent = True
                        continue

                    # Construct message
                    msg = format_notification_message(event.event_type, event.payload_json)
                    
                    # Dispatch to Telegram Bot
                    success = False
                    try:
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=msg,
                            parse_mode=ParseMode.HTML
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
                    
                    # Update status in DB
                    if success:
                        await wait_for_db_recovery("Dispatcher")
                        try:
                            async with get_db_session() as session:
                                async with session.begin():
                                    db_event = await session.get(Event, event.id)
                                    if db_event:
                                        db_event.sent = True
                            logger.info("Successfully dispatched event ID %d to telegram_id=%d", event.id, user.telegram_id)
                        except Exception as e:
                            logger.error("Failed to mark event ID %d as sent in database: %r", event.id, e)
            
            dispatch_completed_successfully = True

        except Exception as e:
            if is_db_connection_error(e):
                logger.error("Database connection lost during dispatcher worker execution. Retrying immediately...", exc_info=True)
            else:
                logger.error("Unexpected error in background notification dispatcher: %r", e, exc_info=True)
                await asyncio.sleep(10)
            
        if dispatch_completed_successfully:
            await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)
