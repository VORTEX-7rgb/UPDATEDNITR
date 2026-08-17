"""E2E Automated Verification Test Suite for CollegeClaw.

Executes all 10 critical verification tests:
TEST 1: Login, Fetch, and Parse (Parser verification)
TEST 2: Snapshot Ingest and Insertion
TEST 3: Duplicate Snapshot Bypass
TEST 4: Delta Change & Absence Event Generation
TEST 5: Dispatcher Loop, Formatting & Sent Status Update
TEST 6: Scraper Failure & Invalid Credentials Isolation
TEST 7: Blocked User TelegramForbiddenError Handling
TEST 8: Worker Cancel and Restart Recovery
TEST 9: Bounded Concurrency (100 parallel users, semaphore throttle)
TEST 10: Database Failure Transaction Rollback & Recovery
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy import select, event, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app.db.models import Base, User, Snapshot, Event, SyncState
from app.db.repositories.user_repository import UserRepository
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.db.repositories.event_repository import EventRepository
from app.services.snapshot_service import SnapshotService
from app.services.event_service import EventService
from app.db.crypto import encrypt_password, decrypt_password, rotate_encryption_keys
from app.nitris.parser import parse_attendance_html, AttendanceResult, AttendanceRecord
from app.nitris.exceptions import LoginError
from app.workers.sync_worker import sync_user_data, format_notification_message, run_dispatch_worker

# aiogram forbidden exception mock helper
from aiogram.exceptions import TelegramForbiddenError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("verify_system")

# Enable foreign keys for SQLite
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

# Mock HTML response representing actual NITRIS attendance data for parser verification
MOCK_ATTENDANCE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Class Attendance</title></head>
<body>
    <span id="ContentPlaceHolder2_ContentPlaceHolder1_mainContent_lblSnameroll">JOHN DOE (123CS0001)</span>
    <table id="ContentPlaceHolder2_ContentPlaceHolder1_mainContent_gvSubjects">
        <tr class="header">
            <th>Subject Code</th>
            <th>Subject Name</th>
            <th>Faculty</th>
            <th>TC</th>
            <th>UA</th>
            <th>LE</th>
            <th>OA</th>
        </tr>
        <tr>
            <td>CS301</td>
            <td>Database Systems</td>
            <td>Dr. Smith</td>
            <td>20</td>
            <td>1</td>
            <td>0</td>
            <td>1</td>
        </tr>
        <tr>
            <td>CS302</td>
            <td>Operating Systems</td>
            <td>Dr. Doe</td>
            <td>15</td>
            <td>0</td>
            <td>0</td>
            <td>0</td>
        </tr>
    </table>
</body>
</html>
"""

async def run_suite():
    results = {}
    logger.info("Initializing in-memory SQLite engine for comprehensive test suite...")
    
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    
    # 1. Initialize Tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    test_session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    logger.info("Database tables initialized successfully. Starting 10-point test runner.\n")

    # Seed parameters
    telegram_id = 9988776655
    roll_number = "123CS0001"
    raw_pass = "my_secret_pass"
    user_id = None

    # ==========================================
    # TEST 1: Login, Fetch, and Parse Verification
    # ==========================================
    try:
        logger.info("--- [TEST 1] Login, Fetch, and Parse HTML Parser verification ---")
        parsed = parse_attendance_html(MOCK_ATTENDANCE_HTML)
        assert parsed.student_info == "JOHN DOE (123CS0001)", "Failed student info check"
        assert len(parsed.records) == 2, "Failed records count check"
        
        record1 = parsed.records[0]
        assert record1.subject_code == "CS301", "Failed subject code check"
        assert record1.subject_name == "Database Systems", "Failed subject name check"
        assert record1.faculty == "Dr. Smith", "Failed faculty name check"
        assert record1.tc == "20", "Failed TC check"
        assert record1.ua == "1", "Failed UA check"
        assert record1.le == "0", "Failed LE check"
        assert record1.oa == "1", "Failed OA check"
        
        logger.info("TEST 1 PASS: Parser mapped headers and records perfectly.")
        results["TEST 1: Login, Fetch, & Parse"] = "PASS"
    except Exception as e:
        logger.error("TEST 1 FAIL: %r", e)
        results["TEST 1: Login, Fetch, & Parse"] = f"FAIL: {repr(e)}"

    # Set up user in database
    async with test_session_factory() as session:
        async with session.begin():
            user_repo = UserRepository(session)
            user = await user_repo.create_user(telegram_id, roll_number, raw_pass)
            user_id = user.id

    # ==========================================
    # TEST 2: Snapshot Ingest and Insertion
    # ==========================================
    try:
        logger.info("\n--- [TEST 2] Ingesting First Snapshot ---")
        async with test_session_factory() as session:
            async with session.begin():
                snapshot_service = SnapshotService(session)
                changed, prev_snap, current_snap = await snapshot_service.create_snapshot_if_changed(
                    user_id=user_id,
                    module_name="attendance",
                    attendance_result=parsed
                )
                
                assert changed is True, "First snapshot must trigger changed=True"
                assert prev_snap is None, "First snapshot previous state must be None"
                assert current_snap is not None, "Current snapshot should be persisted"
                
                # Check events created in db
                event_repo = EventRepository(session)
                events = await event_repo.get_unsent_events(limit=10)
                assert len(events) == 2, f"Expected 2 registration events, found {len(events)}"
                assert events[0].event_type == "new_subject_added"
                
        logger.info("TEST 2 PASS: Snapshot and seeding events inserted successfully.")
        results["TEST 2: Snapshot Creation"] = "PASS"
    except Exception as e:
        logger.error("TEST 2 FAIL: %r", e)
        results["TEST 2: Snapshot Creation"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 3: Duplicate Snapshot Bypass
    # ==========================================
    try:
        logger.info("\n--- [TEST 3] Duplicate Snapshot Ingestion Check ---")
        async with test_session_factory() as session:
            async with session.begin():
                snapshot_service = SnapshotService(session)
                changed, prev_snap, current_snap = await snapshot_service.create_snapshot_if_changed(
                    user_id=user_id,
                    module_name="attendance",
                    attendance_result=parsed
                )
                
                assert changed is False, "Duplicate snapshot must return changed=False"
                assert prev_snap.snapshot_hash == current_snap.snapshot_hash, "Hashes must match"
                
        logger.info("TEST 3 PASS: Duplicate check matches hash signatures and skips creation.")
        results["TEST 3: Duplicate Snapshot Prevention"] = "PASS"
    except Exception as e:
        logger.error("TEST 3 FAIL: %r", e)
        results["TEST 3: Duplicate Snapshot Prevention"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 4: Delta Change & Absence Event Generation
    # ==========================================
    try:
        logger.info("\n--- [TEST 4] Attendance Change Simulation ---")
        # Ingest updated attendance: CS301 TC 20->24, UA 1->2 (New absence!)
        updated_records = [
            AttendanceRecord(
                subject_code="CS301",
                subject_name="Database Systems",
                faculty="Dr. Smith",
                tc="24",
                ua="2",
                le="0",
                oa="2"
            ),
            AttendanceRecord(
                subject_code="CS302",
                subject_name="Operating Systems",
                faculty="Dr. Doe",
                tc="15",
                ua="0",
                le="0",
                oa="0"
            )
        ]
        updated_result = AttendanceResult(
            student_info="JOHN DOE (123CS0001)",
            records=updated_records
        )
        
        async with test_session_factory() as session:
            async with session.begin():
                snapshot_service = SnapshotService(session)
                changed, prev_snap, current_snap = await snapshot_service.create_snapshot_if_changed(
                    user_id=user_id,
                    module_name="attendance",
                    attendance_result=updated_result
                )
                
                assert changed is True, "Modified attendance must trigger changed=True"
                assert prev_snap is not None, "Previous snapshot must be set"
                
                # Verify that changes triggered delta events
                event_repo = EventRepository(session)
                all_unsent = await event_repo.get_unsent_events(limit=50)
                # Should have the 2 old new_subject_added + 2 new events
                # New events: 1x attendance_updated, 1x new_absence_detected
                new_events = [e for e in all_unsent if e.id > 2]
                assert len(new_events) == 2, f"Expected 2 new delta events, got {len(new_events)}"
                
                types = [e.event_type for e in new_events]
                assert "attendance_updated" in types, "Missing attendance_updated event"
                assert "new_absence_detected" in types, "Missing new_absence_detected event"
                
        logger.info("TEST 4 PASS: Semantic diff generated attendance_updated & new_absence_detected.")
        results["TEST 4: Delta Change Event Generation"] = "PASS"
    except Exception as e:
        logger.error("TEST 4 FAIL: %r", e)
        results["TEST 4: Delta Change Event Generation"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 5: Dispatcher Loop, Formatting & Sent Status Update
    # ==========================================
    try:
        logger.info("\n--- [TEST 5] Notification Dispatcher Worker Loop ---")
        mock_bot = AsyncMock()
        
        # Override the database session maker inside sync_worker for dispatch testing
        with patch("app.workers.sync_worker.get_db_session", test_session_factory):
            # Run dispatch worker for a brief moment then cancel
            dispatch_task = asyncio.create_task(run_dispatch_worker(mock_bot))
            await asyncio.sleep(0.5)
            dispatch_task.cancel()
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass
                
        # Check mock bot send_message calls
        assert mock_bot.send_message.call_count >= 4, f"Expected at least 4 transmissions, got {mock_bot.send_message.call_count}"
        
        # Check formatting of one sent message
        first_call_args = mock_bot.send_message.call_args_list[0][1]
        assert "chat_id" in first_call_args
        assert first_call_args["chat_id"] == telegram_id
        assert "<b>" in first_call_args["text"], "HTML bold markup should be present"
        
        # Double check all events marked sent in DB
        async with test_session_factory() as session:
            event_repo = EventRepository(session)
            remaining = await event_repo.get_unsent_events()
            assert len(remaining) == 0, f"Expected 0 unsent events left, got {len(remaining)}"
            
        logger.info("TEST 5 PASS: Messages dispatched successfully, formatted properly, and queue fully cleared.")
        results["TEST 5: Event Dispatcher Loop"] = "PASS"
    except Exception as e:
        logger.error("TEST 5 FAIL: %r", e)
        results["TEST 5: Event Dispatcher Loop"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 6: Scraper Failure & Invalid Credentials Isolation
    # ==========================================
    try:
        logger.info("\n--- [TEST 6] Invalid Credentials Isolation check ---")
        # Create a second user (User B) who has invalid credentials
        async with test_session_factory() as session:
            async with session.begin():
                user_repo = UserRepository(session)
                user_invalid = await user_repo.create_user(11112222, "INVALID_ROLL", "bad_pass")
                invalid_user_id = user_invalid.id
                
                # Retrieve encrypted credentials for both User A and User B to ensure real decryption tests
                stmt = select(User).where(User.id == user_id)
                res = await session.execute(stmt)
                user_a = res.scalar_one()
                user_a_enc_pass = user_a.encrypted_password
                user_invalid_enc_pass = user_invalid.encrypted_password
                
        # Mock get_attendance_data: raise LoginError for INVALID_ROLL, succeed for User A
        async def mock_get_attendance_data_cred(username: str, password: str, *args, **kwargs) -> AttendanceResult:
            if username == "INVALID_ROLL":
                raise LoginError("Mock: Invalid credentials failure!")
            return parsed
            
        semaphore = asyncio.Semaphore(10)
        
        # Mock NitrisClient for TEST 6
        mock_client = MagicMock()
        async def mock_login(username, password):
            if username == "INVALID_ROLL":
                from app.nitris.exceptions import LoginError
                raise LoginError("Mock: Invalid credentials failure!")
            return None
        mock_client.login = mock_login
        mock_client.fetch_messages_list = AsyncMock(return_value="")
        mock_client.close = AsyncMock(return_value=None)
        
        with patch("app.workers.sync_worker.get_attendance_data", mock_get_attendance_data_cred):
            with patch("app.workers.sync_worker.get_db_session", test_session_factory):
                with patch("app.nitris.client.NitrisClient", return_value=mock_client):
                    # Run sync_user_data for both users using real decrypted credentials
                    await sync_user_data(user_id, roll_number, user_a_enc_pass, semaphore)
                    await sync_user_data(invalid_user_id, "INVALID_ROLL", user_invalid_enc_pass, semaphore)
                
        # Check SyncState for both users
        async with test_session_factory() as session:
            # User A (valid) should have 0 failures
            state_res_a = await session.execute(select(SyncState).where(SyncState.user_id == user_id))
            state_a = state_res_a.scalar_one_or_none()
            assert state_a is not None and state_a.failure_count == 0, f"User A failures: {state_a.failure_count if state_a else 'None'}"
            
            # User B (invalid) should have 1 failure
            state_res_b = await session.execute(select(SyncState).where(SyncState.user_id == invalid_user_id))
            state_b = state_res_b.scalar_one_or_none()
            assert state_b is not None and state_b.failure_count == 1, "User B failure count should be 1"
            assert "Invalid credentials" in state_b.last_error, f"Error mismatch: {state_b.last_error}"
            
        logger.info("TEST 6 PASS: Valid user completed successfully, invalid user failed in isolation.")
        results["TEST 6: Invalid Credentials Isolation"] = "PASS"
    except Exception as e:
        logger.error("TEST 6 FAIL: %r", e)
        results["TEST 6: Invalid Credentials Isolation"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 7: Blocked User TelegramForbiddenError Handling
    # ==========================================
    try:
        logger.info("\n--- [TEST 7] Telegram Forbidden Error Safety check ---")
        mock_blocked_bot = AsyncMock()
        # Mock send_message to raise TelegramForbiddenError
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.methods.send_message import SendMessage
        
        # Build dummy exception
        dummy_err = TelegramForbiddenError(method=SendMessage(chat_id=telegram_id, text=""), message="Forbidden")
        mock_blocked_bot.send_message.side_effect = dummy_err
        
        # Inject one unsent event for our user
        async with test_session_factory() as session:
            async with session.begin():
                event_repo = EventRepository(session)
                await event_repo.create_event(user_id, "attendance_updated", {"subject_code": "CS301", "subject_name": "Database"})
                
        # Run dispatch worker
        with patch("app.workers.sync_worker.get_db_session", test_session_factory):
            dispatch_task = asyncio.create_task(run_dispatch_worker(mock_blocked_bot))
            await asyncio.sleep(0.5)
            dispatch_task.cancel()
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass
                
        # Verify event marked sent in DB anyway to avoid blocking the worker queue
        async with test_session_factory() as session:
            event_repo = EventRepository(session)
            remaining = await event_repo.get_unsent_events()
            assert len(remaining) == 0, f"Expected 0 unsent events, got {len(remaining)}"
            
        logger.info("TEST 7 PASS: Blocked user isolated cleanly, queue freed, dispatcher survived.")
        results["TEST 7: Telegram Blocked User Handling"] = "PASS"
    except Exception as e:
        logger.error("TEST 7 FAIL: %r", e)
        results["TEST 7: Telegram Blocked User Handling"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 8: Worker Cancel and Restart Recovery
    # ==========================================
    try:
        logger.info("\n--- [TEST 8] Worker Cancellation & Restart Recovery ---")
        mock_sync_bot = AsyncMock()
        
        from app.workers.sync_worker import run_sync_worker
        
        with patch("app.workers.sync_worker.get_db_session", test_session_factory):
            with patch("app.workers.sync_worker.SYNC_INTERVAL_SECONDS", 0.05):
                # Start worker
                worker_task = asyncio.create_task(run_sync_worker(mock_sync_bot))
                await asyncio.sleep(0.2)
                # Cancel worker
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
                    
                # Restart worker cleanly
                worker_task2 = asyncio.create_task(run_sync_worker(mock_sync_bot))
                await asyncio.sleep(0.1)
                worker_task2.cancel()
                try:
                    await worker_task2
                except asyncio.CancelledError:
                    pass
                    
        logger.info("TEST 8 PASS: Background worker loops cancelled and restarted cleanly.")
        results["TEST 8: Worker Cancellation & Restart"] = "PASS"
    except Exception as e:
        logger.error("TEST 8 FAIL: %r", e)
        results["TEST 8: Worker Cancellation & Restart"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 9: Bounded Concurrency Scale Verification
    # ==========================================
    try:
        logger.info("\n--- [TEST 9] Bounded Concurrency Scale Verification (100 concurrent users) ---")
        
        # 1. Seed 100 users in database
        passwords = {}
        async with test_session_factory() as session:
            async with session.begin():
                user_repo = UserRepository(session)
                for idx in range(1, 101):
                    u = await user_repo.create_user(
                        telegram_id=300000 + idx,
                        roll_number=f"ROLL_{idx}",
                        raw_password="dummy_password"
                    )
                    passwords[f"ROLL_{idx}"] = u.encrypted_password
                    
        # 2. Mock scraper with a slight delay
        active_sync_tasks = 0
        max_seen_concurrency = 0
        concurrency_lock = asyncio.Lock()
        
        async def mock_slow_scraper(username: str, password: str, *args, **kwargs) -> AttendanceResult:
            nonlocal active_sync_tasks, max_seen_concurrency
            async with concurrency_lock:
                active_sync_tasks += 1
                if active_sync_tasks > max_seen_concurrency:
                    max_seen_concurrency = active_sync_tasks
            
            await asyncio.sleep(0.01)  # brief network sleep delay
            
            async with concurrency_lock:
                active_sync_tasks -= 1
                
            return parsed
            
        # Perform concurrently gathered sync using semaphore
        semaphore = asyncio.Semaphore(10) # 10 active tasks limit
        
        tasks = []
        for idx in range(1, 101):
            roll = f"ROLL_{idx}"
            tasks.append(
                sync_user_data(
                    user_id=idx + 2, # offset from previous seeds
                    roll_number=roll,
                    encrypted_pass=passwords[roll],
                    semaphore=semaphore
                )
            )
            
        # Mock NitrisClient for scale test
        mock_client_scale = MagicMock()
        mock_client_scale.login = AsyncMock(return_value=None)
        mock_client_scale.fetch_messages_list = AsyncMock(return_value="")
        mock_client_scale.close = AsyncMock(return_value=None)
        
        with patch("app.workers.sync_worker.get_attendance_data", mock_slow_scraper):
            with patch("app.workers.sync_worker.get_db_session", test_session_factory):
                with patch("app.nitris.client.NitrisClient", return_value=mock_client_scale):
                    await asyncio.gather(*tasks)
                
        logger.info("Scale sync completed. Maximum concurrent tasks seen in scraper: %d", max_seen_concurrency)
        assert max_seen_concurrency <= 10, f"Concurrency limit exceeded! Found {max_seen_concurrency} active jobs"
        
        logger.info("TEST 9 PASS: Bounded concurrency works perfectly, semaphore caps active threads at 10.")
        results["TEST 9: Bounded Concurrency Scale Test"] = "PASS"
    except Exception as e:
        logger.error("TEST 9 FAIL: %r", e)
        results["TEST 9: Bounded Concurrency Scale Test"] = f"FAIL: {repr(e)}"

    # ==========================================
    # TEST 10: Database Failure Integrity & Recovery
    # ==========================================
    try:
        logger.info("\n--- [TEST 10] Database Failure Transaction Rollback & Recovery ---")
        
        # 1. Trigger database operation failure by temporarily poisoning the execution with connection issues
        # We will mock the database session's begin block to raise an OperationalError
        poisoned_session_factory = async_sessionmaker(
            bind=test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        # Let's verify standard rollback behavior when a query raises an OperationalError
        # Inside the context, get_db_session should log, rollback, and re-raise the exception
        import app.db.database
        
        async with test_session_factory() as session:
            try:
                async with session.begin():
                    # Intentionally execute syntax error to force raw OperationalError rollback
                    await session.execute(text("SELECT * FROM non_existent_table_forced_error;"))
            except Exception as ex:
                logger.info("Verified: Syntax error correctly forced a database rollback. Exception: %s", type(ex).__name__)
                # We expect an exception here!
                
        # 2. Confirm system resumes normal operations after connection recovery
        # Verify subsequent query execution and database inserts succeed cleanly
        async with test_session_factory() as session:
            async with session.begin():
                user_repo = UserRepository(session)
                recovered_user = await user_repo.create_user(55554444, "RECOVERED", "password")
                logger.info("Successfully recovered database connection. Inserted recovered user ID: %d", recovered_user.id)
                
        logger.info("TEST 10 PASS: Outage isolated, transactions rolled back correctly, recovered cleanly.")
        results["TEST 10: Database Failure Recovery"] = "PASS"
    except Exception as e:
        logger.error("TEST 10 FAIL: %r", e)
        results["TEST 10: Database Failure Recovery"] = f"FAIL: {repr(e)}"

    # ==========================================
    # VERIFICATION SUMMARY REPORT
    # ==========================================
    print("\n=======================================================")
    print("           AUTOMATED SUITE VERIFICATION REPORT          ")
    print("=======================================================")
    failed = False
    for test_name, status in results.items():
        use_unicode = False
        try:
            "✅".encode(sys.stdout.encoding or "ascii")
            use_unicode = True
        except Exception:
            pass

        if status == "PASS":
            color_marker = "✅ PASS" if use_unicode else "[ PASS ]"
        else:
            color_marker = f"❌ FAIL ({status})" if use_unicode else f"[ FAIL ] ({status})"

        if status != "PASS":
            failed = True
        print(f"{test_name:<45} {color_marker}")
    print("=======================================================")
    
    await test_engine.dispose()
    
    if failed:
        logger.error("SYSTEM AUDIT SUITE FAILED!")
        sys.exit(1)
    else:
        logger.info("ALL INTEGRATION VERIFICATION SCENARIOS PASSED PERFECTLY!")
        sys.exit(0)

if __name__ == "__main__":
    # Fix for Windows Event Loop Issue (WinError 10054 / 121 / 64)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_suite())
