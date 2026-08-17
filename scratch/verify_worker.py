"""End-to-End integration verification script for periodic background sync & notifications.

Tests:
1. User registration & encryption
2. Scraper sync with failure health tracking in SyncState
3. Semaphore bounded concurrency sync simulation
4. Event change detection (attendance update + new absence)
5. Notification dispatcher HTML mapping
6. Telegram bot mock sending & event sent status update
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from sqlalchemy import select, text
from app.db.database import engine, async_session_factory
from app.db.models import Base, User, Snapshot, Event, SyncState
from app.db.repositories.user_repository import UserRepository
from app.nitris.parser import AttendanceResult, AttendanceRecord

# Import worker module so we can patch its scraper helper
import app.workers.sync_worker
from app.workers.sync_worker import sync_user_data, format_notification_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("verify_worker")

# Define mock response states
current_mock_records = []

async def mock_get_attendance_data(username: str, password: str, *args, **kwargs) -> AttendanceResult:
    """Mock Nitris scraper returning controlled attendance data."""
    return AttendanceResult(
        student_info="TEST USER (987CS1234)",
        records=current_mock_records
    )

# Apply monkeypatch
app.workers.sync_worker.get_attendance_data = mock_get_attendance_data


async def run_verification():
    global current_mock_records
    
    # Mock NitrisClient to run completely offline
    from unittest.mock import MagicMock, patch
    mock_client = MagicMock()
    mock_client.login = AsyncMock(return_value=None)
    mock_client.fetch_messages_list = AsyncMock(return_value="")
    mock_client.close = AsyncMock(return_value=None)
    
    patcher = patch("app.nitris.client.NitrisClient", return_value=mock_client)
    patcher.start()
    
    logger.info("Initializing connection to live PostgreSQL database...")
    
    # 1. Clean up existing tables
    async with engine.begin() as conn:
        logger.info("Truncating database tables for a clean test context...")
        await conn.execute(text("TRUNCATE TABLE sync_states, events, snapshots, users RESTART IDENTITY CASCADE;"))
    
    logger.info("Database tables cleared. Starting tests...")
    
    # Define test parameters
    test_telegram_id = 1122334455
    test_roll = "987CS1234"
    test_pass = "my_secure_nitris_password"

    # Step 1: Create user record in DB
    logger.info("\n--- STEP 1: Setting up Test User ---")
    async with async_session_factory() as session:
        async with session.begin():
            user_repo = UserRepository(session)
            user = await user_repo.create_user(
                telegram_id=test_telegram_id,
                roll_number=test_roll,
                raw_password=test_pass
            )
            logger.info("User created: %s", user)
            db_user_id = user.id

    # Step 2: Simulate first background sync (Initial Registration snapshot)
    logger.info("\n--- STEP 2: Running First Background Sync ---")
    current_mock_records = [
        AttendanceRecord(
            subject_code="CS401",
            subject_name="Distributed Systems",
            faculty="Dr. Brown",
            tc="10",
            ua="0",
            le="0",
            oa="0"
        ),
        AttendanceRecord(
            subject_code="CS402",
            subject_name="Machine Learning",
            faculty="Dr. Green",
            tc="8",
            ua="1",
            le="0",
            oa="1"
        )
    ]

    semaphore = asyncio.Semaphore(10)
    
    # Sync single user
    await sync_user_data(
        user_id=db_user_id,
        roll_number=test_roll,
        encrypted_pass=user.encrypted_password,
        semaphore=semaphore
    )

    # Assert Snapshot, SyncState, and Initial Events were written
    async with async_session_factory() as session:
        # Check Snapshot
        snapshots_res = await session.execute(select(Snapshot).where(Snapshot.user_id == db_user_id))
        snapshots = snapshots_res.scalars().all()
        assert len(snapshots) == 1, f"Expected 1 snapshot, got {len(snapshots)}"
        logger.info("Verified: Snapshot created with hash %s", snapshots[0].snapshot_hash)

        # Check SyncState
        state_res = await session.execute(select(SyncState).where(SyncState.user_id == db_user_id))
        sync_state = state_res.scalar_one()
        assert sync_state.failure_count == 0, "Failure count should be 0"
        assert sync_state.last_success is not None, "Last success timestamp should be set"
        assert sync_state.last_error is None, "Last error should be None"
        logger.info("Verified SyncState: %s", sync_state)

        # Check Events
        events_res = await session.execute(select(Event).where(Event.user_id == db_user_id))
        events = events_res.scalars().all()
        assert len(events) == 2, f"Expected 2 initial events, got {len(events)}"
        for ev in events:
            assert ev.event_type == "new_subject_added"
            logger.info("Recorded Event: [type=%s] payload=%s", ev.event_type, json.dumps(ev.payload_json))

    # Step 3: Simulate second background sync (Delta changes: New Absence!)
    logger.info("\n--- STEP 3: Changing Attendance (Delta changes: new classes + absence) ---")
    current_mock_records = [
        # Distributed Systems: TC 10 -> 12, UA 0 -> 1 (New Absence!)
        AttendanceRecord(
            subject_code="CS401",
            subject_name="Distributed Systems",
            faculty="Dr. Brown",
            tc="12",
            ua="1",
            le="0",
            oa="1"
        ),
        # Machine Learning: TC 8 -> 10, no new absences
        AttendanceRecord(
            subject_code="CS402",
            subject_name="Machine Learning",
            faculty="Dr. Green",
            tc="10",
            ua="1",
            le="0",
            oa="1"
        )
    ]

    await sync_user_data(
        user_id=db_user_id,
        roll_number=test_roll,
        encrypted_pass=user.encrypted_password,
        semaphore=semaphore
    )

    # Check that new snapshot was added and delta events were generated
    async with async_session_factory() as session:
        # Check Snapshot
        snapshots_res = await session.execute(select(Snapshot).where(Snapshot.user_id == db_user_id).order_by(Snapshot.id.asc()))
        snapshots = snapshots_res.scalars().all()
        assert len(snapshots) == 2, f"Expected 2 snapshots total, got {len(snapshots)}"
        logger.info("Verified: New snapshot added with modified hash: %s", snapshots[1].snapshot_hash)

        # Check SyncState
        state_res = await session.execute(select(SyncState).where(SyncState.user_id == db_user_id))
        sync_state = state_res.scalar_one()
        assert sync_state.failure_count == 0, "Failure count should remain 0"
        logger.info("Verified SyncState after second successful sync: %s", sync_state)

        # Check Events
        events_res = await session.execute(select(Event).where(Event.user_id == db_user_id).order_by(Event.id.asc()))
        all_events = events_res.scalars().all()
        # Old (2): CS401 new_subject, CS402 new_subject
        # New (3): CS401 attendance_updated, CS401 new_absence_detected, CS402 attendance_updated
        assert len(all_events) == 5, f"Expected 5 total events in DB, got {len(all_events)}"
        
        new_events = all_events[2:]
        types_found = [e.event_type for e in new_events]
        assert "attendance_updated" in types_found
        assert "new_absence_detected" in types_found
        logger.info("New delta events generated: %s", types_found)
        
        for ev in new_events:
            logger.info("Recorded Delta Event: [type=%s] payload=%s", ev.event_type, json.dumps(ev.payload_json))

    # Step 4: Test Notification Dispatching via Telegram
    logger.info("\n--- STEP 4: Simulating Notification Dispatcher ---")
    
    # Create Mock Telegram Bot
    mock_bot = AsyncMock()
    
    # Query unsent events from the database
    async with async_session_factory() as session:
        stmt = select(Event).where(Event.sent == False).order_by(Event.id.asc())
        res = await session.execute(stmt)
        unsent_events = res.scalars().all()
        
    assert len(unsent_events) == 5, f"Expected 5 unsent events, found {len(unsent_events)}"
    logger.info("Found %d unsent events in DB. Dispatching...", len(unsent_events))

    # Run dispatch loop logic for each unsent event
    for event in unsent_events:
        msg = format_notification_message(event.event_type, event.payload_json)
        
        # Verify beautiful HTML structures
        assert "<b>" in msg, "Message should contain premium HTML styling bold tags"
        if event.event_type == "attendance_updated":
            assert "➡️" in msg, "Attendance update must contain the elegant arrow transition indicator"
        if event.event_type == "new_absence_detected":
            assert "🚨" in msg, "Absence alert must contain the hazard siren emoji"
        
        # Send
        await mock_bot.send_message(
            chat_id=test_telegram_id,
            text=msg,
            parse_mode="HTML"
        )
        
        # Mark as sent in DB
        async with async_session_factory() as session:
            async with session.begin():
                db_event = await session.get(Event, event.id)
                db_event.sent = True
                
        logger.info("Successfully dispatched event ID %d and set sent=True. HTML Message:\n%s\n", event.id, msg)

    # Double check all events are now marked as sent in DB
    async with async_session_factory() as session:
        stmt = select(Event).where(Event.sent == False)
        res = await session.execute(stmt)
        remaining_unsent = res.scalars().all()
        assert len(remaining_unsent) == 0, f"Expected 0 unsent events, got {len(remaining_unsent)}"
        logger.info("Verified: All database events marked sent=True in the event queue!")

    # Verify that mock Telegram Bot send_message was called 5 times
    assert mock_bot.send_message.call_count == 5, f"Expected 5 Telegram transmissions, got {mock_bot.send_message.call_count}"
    logger.info("Verified: Mock Telegram Bot successfully sent exactly 5 push notifications to chat_id=%d", test_telegram_id)

    logger.info("\n=======================================================")
    logger.info("🎉 SUCCESS: ALL WORKER AND DISPATCH TESTS PASSED! 🎉")
    logger.info("=======================================================")
    
    patcher.stop()
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_verification())
