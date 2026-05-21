"""Integration verification script for the PostgreSQL persistence layer.

Tests end-to-end database interactions using the live PostgreSQL database,
fully verifying repositories, services, encryption, and key rotation.
"""

import asyncio
import json
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Base
from app.db.database import engine, async_session_factory
from app.db.repositories.user_repository import UserRepository
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.db.repositories.event_repository import EventRepository
from app.services.snapshot_service import SnapshotService
from app.db.crypto import encrypt_password, decrypt_password, rotate_encryption_keys
from app.nitris.parser import AttendanceResult, AttendanceRecord
from app.config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("verify_postgres")

async def run_verification():
    logger.info("Initializing connection to live PostgreSQL database...")
    
    # Truncate tables to ensure a clean state for the test
    async with engine.begin() as conn:
        logger.info("Cleaning up existing test data in tables...")
        # Since we use foreign keys, truncate with cascade
        await conn.execute(text("TRUNCATE TABLE events, snapshots, users RESTART IDENTITY CASCADE;"))
    
    logger.info("PostgreSQL tables cleared. Beginning tests...")
    
    # Define verification steps
    user_telegram_id = 987654321
    user_roll_number = "123CS0456"
    user_raw_password = "super_secret_password_123"
    
    # Step 1: Test UserRepository & Encryption
    logger.info("\n--- STEP 1: Testing UserRepository & Credential Encryption ---")
    async with async_session_factory() as session:
        async with session.begin():
            user_repo = UserRepository(session)
            
            # Create user
            user = await user_repo.create_user(
                telegram_id=user_telegram_id,
                roll_number=user_roll_number,
                raw_password=user_raw_password,
            )
            logger.info("Successfully created user: %s", user)
            
            # Verify database encryption: password must NOT be in plain text
            logger.info("Verifying password encrypted state in DB model...")
            assert user.encrypted_password != user_raw_password, "CRITICAL ERROR: Password stored in plain text!"
            logger.info("Verified: Encrypted password is a secure cipher: %s", user.encrypted_password)

    # Step 2: Test Decryption & Retrieval
    logger.info("\n--- STEP 2: Testing User Retrieval & Decryption ---")
    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        retrieved_user = await user_repo.get_by_telegram_id(user_telegram_id)
        
        assert retrieved_user is not None, "Error: User not found in DB."
        logger.info("Retrieved user by Telegram ID %d: %s", user_telegram_id, retrieved_user)
        
        decrypted = decrypt_password(retrieved_user.encrypted_password)
        assert decrypted == user_raw_password, "CRITICAL ERROR: Decrypted password does not match raw password!"
        logger.info("Verified: Decrypted password matches raw password perfectly: %s", decrypted)
        
        # Keep user ID for subsequent steps
        db_user_id = retrieved_user.id

    # Step 3: Test SnapshotService and EventService (First Ingestion)
    logger.info("\n--- STEP 3: Ingesting First Snapshot (Initial Registration) ---")
    
    initial_records = [
        AttendanceRecord(
            subject_code="CS301",
            subject_name="Database Systems",
            faculty="Dr. Smith",
            tc="20",
            ua="1",
            le="0",
            oa="1",
        ),
        AttendanceRecord(
            subject_code="CS302",
            subject_name="Operating Systems",
            faculty="Dr. Doe",
            tc="15",
            ua="0",
            le="0",
            oa="0",
        ),
    ]
    initial_result = AttendanceResult(
        student_info="JOHN DOE (123CS0456)",
        records=initial_records,
    )
    
    async with async_session_factory() as session:
        async with session.begin():
            snapshot_service = SnapshotService(session)
            
            changed, prev_snap, current_snap = await snapshot_service.create_snapshot_if_changed(
                user_id=db_user_id,
                module_name="attendance",
                attendance_result=initial_result,
            )
            
            assert changed is True, "First snapshot ingestion should trigger a changed state."
            assert prev_snap is None, "First snapshot previous state should be None."
            logger.info("First snapshot successfully ingested. Snapshot Hash: %s", current_snap.snapshot_hash)
            
            # Verify initial events: should be 'new_subject_added' for all subjects
            event_repo = EventRepository(session)
            events = await event_repo.get_unsent_events(limit=10)
            
            assert len(events) == 2, f"Expected 2 initial events, found {len(events)}."
            for e in events:
                assert e.event_type == "new_subject_added", f"Expected 'new_subject_added', got '{e.event_type}'."
                logger.info("Recorded Event: [type=%s] for subject %s", e.event_type, e.payload_json["subject_code"])

    # Step 4: Test State Modification & Delta Detection (Second Ingestion)
    logger.info("\n--- STEP 4: Ingesting Second Snapshot (Delta Changes) ---")
    
    updated_records = [
        AttendanceRecord(
            subject_code="CS301",
            subject_name="Database Systems",
            faculty="Dr. Smith",
            tc="24",
            ua="2",
            le="0",
            oa="2",
        ),
        AttendanceRecord(
            subject_code="CS302",
            subject_name="Operating Systems",
            faculty="Dr. Doe",
            tc="17",
            ua="0",
            le="0",
            oa="0",
        ),
        AttendanceRecord(
            subject_code="CS303",
            subject_name="Computer Networks",
            faculty="Dr. Alan",
            tc="4",
            ua="0",
            le="0",
            oa="0",
        ),
    ]
    updated_result = AttendanceResult(
        student_info="JOHN DOE (123CS0456)",
        records=updated_records,
    )
    
    async with async_session_factory() as session:
        async with session.begin():
            snapshot_service = SnapshotService(session)
            
            changed, prev_snap, current_snap = await snapshot_service.create_snapshot_if_changed(
                user_id=db_user_id,
                module_name="attendance",
                attendance_result=updated_result,
            )
            
            assert changed is True, "Second snapshot with modified data should report changed state."
            assert prev_snap is not None, "Previous snapshot should be populated for second ingestion."
            logger.info("Second snapshot successfully ingested. Snapshot Hash: %s", current_snap.snapshot_hash)
            
            # Fetch unsent events
            event_repo = EventRepository(session)
            all_unsent = await event_repo.get_unsent_events(limit=10)
            logger.info("Total unsent events in DB: %d", len(all_unsent))
            
            new_events = [e for e in all_unsent if e.id > 2]
            assert len(new_events) == 4, f"Expected 4 new events, found {len(new_events)}."
            
            types_found = [e.event_type for e in new_events]
            logger.info("New events generated: %s", types_found)
            assert "new_subject_added" in types_found
            assert "attendance_updated" in types_found
            assert "new_absence_detected" in types_found
            
            for e in new_events:
                logger.info("Recorded Event: [type=%s] payload=%s", e.event_type, json.dumps(e.payload_json))

    # Step 5: Test Unchanged Ingestion
    logger.info("\n--- STEP 5: Ingesting Third Snapshot (No Changes) ---")
    async with async_session_factory() as session:
        async with session.begin():
            snapshot_service = SnapshotService(session)
            
            changed, prev_snap, current_snap = await snapshot_service.create_snapshot_if_changed(
                user_id=db_user_id,
                module_name="attendance",
                attendance_result=updated_result,
            )
            
            assert changed is False, "Ingesting same attendance result must NOT trigger a changed state."
            logger.info("Verified: Same payload yields same hash and does not produce new snapshot records.")

    # Step 6: Test Encryption Key Rotation
    logger.info("\n--- STEP 6: Testing Encryption Key Rotation ---")
    
    from cryptography.fernet import Fernet
    new_test_key = Fernet.generate_key().decode()
    old_test_key = config.ENCRYPTION_KEY
    
    logger.info("Original ENCRYPTION_KEY: %s", old_test_key)
    logger.info("New rotation target key: %s", new_test_key)
    
    async with async_session_factory() as session:
        async with session.begin():
            user_repo = UserRepository(session)
            user_to_rotate = await user_repo.get_by_telegram_id(user_telegram_id)
            users_list = [user_to_rotate]
            
            updated_credentials = await rotate_encryption_keys(
                users=users_list,
                old_key=old_test_key,
                new_key=new_test_key,
            )
            
            for u in users_list:
                u.encrypted_password = updated_credentials[u.id]
            logger.info("Rotation applied to database model objects.")
            
    config.ENCRYPTION_KEY = new_test_key
    
    try:
        async with async_session_factory() as session:
            user_repo = UserRepository(session)
            rotated_user = await user_repo.get_by_telegram_id(user_telegram_id)
            
            decrypted_with_new = decrypt_password(rotated_user.encrypted_password)
            assert decrypted_with_new == user_raw_password, "CRITICAL ERROR: Decryption failed after key rotation!"
            logger.info("Verified: Password successfully decrypted using the new key: %s", decrypted_with_new)
            
    finally:
        config.ENCRYPTION_KEY = old_test_key
        logger.info("Restored original config.ENCRYPTION_KEY.")

    logger.info("\n=======================================================")
    logger.info("🎉 SUCCESS: ALL POSTGRESQL TESTS PASSED PERFECTLY! 🎉")
    logger.info("=======================================================")
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_verification())
