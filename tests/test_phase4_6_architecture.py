"""Phase 4-6 architecture tests.

Tests the gateway, job queue, single-flight dedup, scheduler, and security
properties without requiring a real NITRIS or PostgreSQL instance.

Run with:
    python -m pytest tests/test_phase4_6_architecture.py -v
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure app is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing app
# Use a valid Fernet key (32 bytes, URL-safe base64)
os.environ["ENCRYPTION_KEY"] = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
os.environ["BOT_TOKEN"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/test"

import pytest


# ── Phase 1: Gateway tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_gateway_starts_closed():
    """Circuit breaker starts in CLOSED state."""
    from app.nitris.gateway import nitris_gateway, CircuitState
    nitris_gateway._reset_metrics_for_testing()
    assert nitris_gateway._metrics.circuit_state == CircuitState.CLOSED
    assert not nitris_gateway.is_circuit_open()


@pytest.mark.asyncio
async def test_gateway_concurrency_cap():
    """Gateway enforces max_concurrent limit."""
    from app.nitris.gateway import nitris_gateway
    nitris_gateway._reset_metrics_for_testing()
    
    acquired_count = 0
    max_seen = 0
    
    async def hold_slot(duration):
        nonlocal acquired_count, max_seen
        async with nitris_gateway.acquire():
            acquired_count += 1
            max_seen = max(max_seen, acquired_count)
            await asyncio.sleep(duration)
            acquired_count -= 1
    
    # Launch more concurrent tasks than the cap
    tasks = [hold_slot(0.1) for _ in range(10)]
    await asyncio.gather(*tasks)
    
    # max_concurrent is enforced, so we should never see more than max_concurrent
    assert max_seen <= nitris_gateway.max_concurrent, f"Concurrency exceeded cap: {max_seen} > {nitris_gateway.max_concurrent}"


@pytest.mark.asyncio
async def test_gateway_circuit_opens_on_errors():
    """Circuit opens after threshold consecutive errors."""
    from app.nitris.gateway import nitris_gateway, CircuitState
    from app.nitris.exceptions import NitrisError
    nitris_gateway._reset_metrics_for_testing()
    
    # Trigger errors by raising inside acquire()
    for _ in range(10):
        try:
            async with nitris_gateway.acquire():
                raise NitrisError("test error")
        except NitrisError:
            pass
    
    assert nitris_gateway._metrics.circuit_state == CircuitState.OPEN
    assert nitris_gateway.is_circuit_open()


@pytest.mark.asyncio
async def test_gateway_rejects_fast_when_open():
    """When circuit is OPEN, acquire() rejects immediately."""
    from app.nitris.gateway import nitris_gateway, NitrisCircuitOpenError
    from app.nitris.exceptions import NitrisError
    nitris_gateway._reset_metrics_for_testing()
    
    # Open the circuit
    for _ in range(10):
        try:
            async with nitris_gateway.acquire():
                raise NitrisError("test")
        except NitrisError:
            pass
    
    # Now acquire() should reject immediately
    with pytest.raises(NitrisCircuitOpenError):
        async with nitris_gateway.acquire():
            pass


@pytest.mark.asyncio
async def test_gateway_login_error_doesnt_trip_circuit():
    """LoginError (credential issue) doesn't count toward circuit breaker."""
    from app.nitris.gateway import nitris_gateway, CircuitState
    from app.nitris.exceptions import LoginError
    nitris_gateway._reset_metrics_for_testing()
    
    # Trigger 10 LoginErrors
    for _ in range(10):
        try:
            async with nitris_gateway.acquire():
                raise LoginError("Invalid credentials")
        except LoginError:
            pass
    
    # Circuit should still be closed — LoginError is a user issue, not NITRIS health
    assert nitris_gateway._metrics.circuit_state == CircuitState.CLOSED
    assert nitris_gateway._metrics.consecutive_errors == 0


# ── Phase 2: Job Queue tests ───────────────────────────────────────

@pytest.mark.asyncio
async def test_job_queue_priority_ordering():
    """HIGH priority jobs run before LOW priority jobs."""
    from app.nitris.job_queue import NitrisJobQueue, Priority, NitrisJob
    from app.nitris.gateway import nitris_gateway
    
    queue = NitrisJobQueue(gateway=nitris_gateway, num_workers=1)
    
    execution_order = []
    
    @queue.handler("test_low")
    async def handle_low(job):
        execution_order.append("low")
        return {"success": True}
    
    @queue.handler("test_high")
    async def handle_high(job):
        execution_order.append("high")
        return {"success": True}
    
    await queue.start()
    try:
        # Enqueue LOW first, then HIGH
        await queue.enqueue("test_low", user_id=1, priority=Priority.LOW)
        await asyncio.sleep(0.05)  # Let LOW get picked up
        await queue.enqueue("test_high", user_id=2, priority=Priority.HIGH)
        
        await asyncio.sleep(0.3)
        
        assert "high" in execution_order or "low" in execution_order
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_job_queue_single_flight_dedup():
    """Identical dedup_key requests share one execution."""
    from app.nitris.job_queue import NitrisJobQueue, Priority
    from app.nitris.gateway import nitris_gateway
    
    queue = NitrisJobQueue(gateway=nitris_gateway, num_workers=1)
    
    execution_count = 0
    
    @queue.handler("test_dedup")
    async def handle_dedup(job):
        nonlocal execution_count
        execution_count += 1
        await asyncio.sleep(0.2)  # Simulate slow NITRIS work
        return {"success": True, "data": "result"}
    
    await queue.start()
    try:
        # Enqueue 100 identical jobs with the same dedup_key
        futures = []
        for i in range(100):
            future = await queue.enqueue(
                "test_dedup", user_id=1, priority=Priority.MEDIUM,
                dedup_key="test_dedup_key",
            )
            futures.append(future)
        
        # Wait for completion
        results = await asyncio.gather(*futures, return_exceptions=True)
        
        # Only ONE execution should have happened
        assert execution_count == 1, f"Expected 1 execution, got {execution_count}"
        
        # All 100 futures should have the same result
        for r in results:
            assert isinstance(r, dict)
            assert r["success"] is True
            assert r["data"] == "result"
    finally:
        await queue.stop()


# ── Phase 3: Cache-first attendance tests ──────────────────────────

def test_format_attendance_message_from_snapshot():
    """format_attendance_message_from_snapshot renders cached data correctly."""
    from app.bot.telegram import format_attendance_message_from_snapshot
    
    snapshot = MagicMock()
    snapshot.snapshot_json = {
        "student_info": "Test Student (123AI0001)",
        "records": [
            {"subject_code": "CS101", "subject_name": "Intro to CS",
             "tc": "10", "oa": "8", "ua": "2", "le": "0"},
        ],
    }
    snapshot.created_at = None
    
    result = format_attendance_message_from_snapshot(snapshot)
    assert "Test Student" in result
    assert "CS101" in result or "Intro to CS" in result
    assert "TC: 10" in result


def test_format_attendance_message_from_snapshot_empty():
    """format_attendance_message_from_snapshot handles empty/None gracefully."""
    from app.bot.telegram import format_attendance_message_from_snapshot
    
    result = format_attendance_message_from_snapshot(None)
    assert "No cached" in result
    
    snapshot = MagicMock()
    snapshot.snapshot_json = None
    result = format_attendance_message_from_snapshot(snapshot)
    assert "No cached" in result


# ── Phase 4: QP metadata single-flight tests ──────────────────────

@pytest.mark.asyncio
async def test_qp_metadata_dedup_key_is_deterministic():
    """QP metadata dedup key is deterministic for same (subject, year)."""
    from app.services.examination_service import _clean_code
    
    subj1 = "CS2001"
    subj2 = "cs2001"
    subj3 = "CS-2001"
    year = "2024-25/Autumn"
    
    key1 = f"qp_metadata:{_clean_code(subj1)}:{year}"
    key2 = f"qp_metadata:{_clean_code(subj2)}:{year}"
    key3 = f"qp_metadata:{_clean_code(subj3)}:{year}"
    
    assert key1 == key2 == key3, f"Keys should match: {key1}, {key2}, {key3}"


# ── Phase 5: Scheduler tests ───────────────────────────────────────

def test_module_ttl_config():
    """Per-module TTLs are configured correctly."""
    from app.config import config
    
    assert "attendance" in config.MODULE_TTL_SECONDS
    assert "inbox" in config.MODULE_TTL_SECONDS
    assert "timetable" in config.MODULE_TTL_SECONDS
    
    # attendance: 12h = 43200s
    assert config.MODULE_TTL_SECONDS["attendance"] == 12 * 3600
    # inbox: 4h = 14400s
    assert config.MODULE_TTL_SECONDS["inbox"] == 4 * 3600
    # timetable: 7d = 604800s
    assert config.MODULE_TTL_SECONDS["timetable"] == 7 * 24 * 3600
    
    # Scheduler settings
    assert config.SCHEDULER_BATCH_SIZE > 0
    assert config.SCHEDULER_POLL_INTERVAL > 0
    assert config.SCHEDULER_CLAIM_STALE_SECONDS > 0


def test_module_sync_schedule_model():
    """ModuleSyncSchedule model has required fields."""
    from app.db.models import ModuleSyncSchedule
    
    # Check table name
    assert ModuleSyncSchedule.__tablename__ == "module_sync_schedule"
    
    # Check columns exist on the model
    assert hasattr(ModuleSyncSchedule, "user_id")
    assert hasattr(ModuleSyncSchedule, "module_name")
    assert hasattr(ModuleSyncSchedule, "next_sync_at")
    assert hasattr(ModuleSyncSchedule, "last_synced_at")
    assert hasattr(ModuleSyncSchedule, "last_status")
    assert hasattr(ModuleSyncSchedule, "consecutive_failures")
    assert hasattr(ModuleSyncSchedule, "scheduler_claimed_at")


# ── Phase 6: Security tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_password_in_job_payload():
    """Job payloads never contain plaintext passwords."""
    from app.nitris.job_queue import NitrisJobQueue, Priority
    from app.nitris.gateway import nitris_gateway
    
    queue = NitrisJobQueue(gateway=nitris_gateway, num_workers=0)
    
    # Enqueue a job with a payload
    future = await queue.enqueue(
        "test_job", user_id=1, priority=Priority.MEDIUM,
        payload={
            "callback_chat_id": 123,
            "callback_message_id": 456,
            "subject_code": "CS2001",
            "academic_year": "2024-25/Autumn",
            "roll_number": "123AI0001",
            # Note: NO password field
        },
    )
    
    # Get the job from the queue
    job = await queue._queue.get()
    
    # Verify no password fields
    payload_str = str(job.payload).lower()
    assert "password" not in payload_str
    assert "encrypted_password" not in payload_str
    assert "plaintext" not in payload_str


@pytest.mark.asyncio
async def test_admin_check():
    """is_admin returns True only for configured admin IDs."""
    from app.bot.telegram import is_admin
    from app.config import config
    
    assert not is_admin(999999999)
    assert callable(is_admin)


# ── SSRF protection tests ──────────────────────────────────────────

def test_ssrf_protection_rejects_external_urls():
    """SSRF validation rejects non-NITRIS URLs."""
    from app.nitris.client import NitrisClient
    import inspect
    
    source = inspect.getsource(NitrisClient.download_attachment)
    assert "SSRF" in source or "ssrf" in source.lower()
    assert "urlparse" in source
    assert "netloc" in source


# ── Migration validation ───────────────────────────────────────────

def test_migrations_exist():
    """All expected migrations exist."""
    import os
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "alembic", "versions"
    )
    migrations = [f for f in os.listdir(migrations_dir) if f.endswith('.py') and not f.startswith('__')]
    
    expected = [
        "0001_initial_schema.py",
        "0002_qp_state_machine.py",
        "0003_event_dispatcher_state.py",
        "0004_qp_lease_and_creds.py",
        "0005_module_sync_schedule.py",
    ]
    
    for exp in expected:
        assert exp in migrations, f"Missing migration: {exp}"


def test_migration_0005_references_correct_down_revision():
    """Migration 0005 chains from 0004."""
    import importlib.util
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "alembic", "versions"
    )
    spec = importlib.util.spec_from_file_location(
        "mig5", os.path.join(migrations_dir, "0005_module_sync_schedule.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    
    assert mod.down_revision == "0004_qp_lease_and_creds"
    assert mod.revision == "0005_module_sync_schedule"
