"""Unit and integration tests for NITRIS Gateway, Priority Job Queue, and Rate Limiter."""
from __future__ import annotations

import asyncio
import time
import pytest

from app.nitris.gateway import NitrisGateway, CircuitState, CircuitBreakerOpenError
from app.nitris.exceptions import AttendanceWorkflowError, NitrisError
from app.nitris.job_queue import NitrisJobQueue, JobPriority
from app.nitris.rate_limiter import check_and_set_cooldown, clear_cooldown, get_active_cooldowns_count
from app.bot.telegram import format_attendance_message_from_snapshot
from app.db.models import Snapshot


# ── Gateway Tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gateway_concurrency_and_downward_adaptation():
    gw = NitrisGateway(
        max_concurrent=4,
        min_login_interval=0.01,
        circuit_error_threshold=5,
        circuit_recovery_seconds=1,
    )

    active_count = 0
    max_observed_active = 0
    lock = asyncio.Lock()

    async def mock_portal_task():
        nonlocal active_count, max_observed_active
        async with gw.acquire():
            async with lock:
                active_count += 1
                if active_count > max_observed_active:
                    max_observed_active = active_count
            await asyncio.sleep(0.05)
            async with lock:
                active_count -= 1

    # Run 10 concurrent requests
    await asyncio.gather(*(mock_portal_task() for _ in range(10)))
    assert max_observed_active <= 4
    assert gw.circuit_state == CircuitState.CLOSED

    # Trigger 3 consecutive errors -> concurrency adapts downward
    for _ in range(3):
        try:
            async with gw.acquire():
                raise AttendanceWorkflowError("503 Service Unavailable")
        except AttendanceWorkflowError:
            pass

    assert gw.current_max_concurrent == 3

    # Trigger 2 more errors (total 5 consecutive) -> circuit trips to OPEN
    for _ in range(2):
        try:
            async with gw.acquire():
                raise AttendanceWorkflowError("503 Service Unavailable")
        except AttendanceWorkflowError:
            pass

    assert gw.circuit_state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_gateway_circuit_breaker_open_rejects_fast():
    gw = NitrisGateway(
        max_concurrent=3,
        min_login_interval=0.01,
        circuit_error_threshold=2,
        circuit_recovery_seconds=0.2,
    )

    # Trip the circuit
    for _ in range(2):
        try:
            async with gw.acquire():
                raise AttendanceWorkflowError("Portal Error")
        except AttendanceWorkflowError:
            pass
        except RuntimeError:
            pass

    assert gw.circuit_state == CircuitState.OPEN

    # Next attempt should be rejected immediately by circuit breaker
    with pytest.raises(CircuitBreakerOpenError):
        async with gw.acquire():
            pass

    # Wait for recovery window to expire
    await asyncio.sleep(0.25)
    # M2 fix: is_circuit_open() is a pure predicate — it no longer flips the
    # state as a read side-effect. The OPEN -> HALF_OPEN transition happens
    # lazily inside acquire(), which admits exactly ONE probe.
    assert gw.is_circuit_open() is False

    # First acquire becomes the single recovery probe; success closes circuit.
    async with gw.acquire():
        pass
    assert gw.circuit_state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_gateway_login_pacing():
    gw = NitrisGateway(
        max_concurrent=4,
        min_login_interval=0.05,  # 50ms interval
    )

    login_times = []

    async def paced_login():
        async with gw.acquire(is_login=True):
            login_times.append(time.monotonic())

    await asyncio.gather(*(paced_login() for _ in range(3)))

    assert len(login_times) == 3
    # Check that sequential logins are spaced out
    sorted_times = sorted(login_times)
    diff1 = sorted_times[1] - sorted_times[0]
    diff2 = sorted_times[2] - sorted_times[1]
    assert diff1 >= 0.04
    assert diff2 >= 0.04


# ── Job Queue Tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_queue_priority_ordering():
    jq = NitrisJobQueue(num_workers=1)
    processed_order = []

    async def mock_handler(payload: dict, bot):
        processed_order.append(payload["name"])
        await asyncio.sleep(0.01)
        return payload["name"]

    jq.register_handler("test_job", mock_handler)
    await jq.start(bot=None)

    try:
        # Pause queue processing briefly while enqueuing different priorities
        f_low = await jq.enqueue("test_job", {"name": "low_1"}, priority=JobPriority.LOW)
        f_high = await jq.enqueue("test_job", {"name": "high_1"}, priority=JobPriority.HIGH)
        f_normal = await jq.enqueue("test_job", {"name": "normal_1"}, priority=JobPriority.NORMAL)

        await asyncio.gather(f_low, f_high, f_normal)

        # HIGH should be processed before NORMAL, and NORMAL before LOW
        assert processed_order == ["high_1", "normal_1", "low_1"]
    finally:
        await jq.stop()


@pytest.mark.asyncio
async def test_job_queue_single_flight_dedup():
    jq = NitrisJobQueue(num_workers=2)
    executions = 0

    async def mock_handler(payload: dict, bot):
        nonlocal executions
        executions += 1
        await asyncio.sleep(0.05)
        return f"result_for_{payload['user_id']}"

    jq.register_handler("dedup_job", mock_handler)
    await jq.start(bot=None)

    try:
        # Enqueue 5 identical requests with the same dedup_key simultaneously
        futures = await asyncio.gather(*(
            jq.enqueue(
                "dedup_job",
                {"user_id": 42},
                priority=JobPriority.HIGH,
                dedup_key="attendance:42",
            )
            for _ in range(5)
        ))

        results = await asyncio.gather(*futures)
        # All 5 callers get the exact same result
        assert all(r == "result_for_42" for r in results)
        # But the handler executed only ONCE
        assert executions == 1
    finally:
        await jq.stop()


# ── Rate Limiter Tests ─────────────────────────────────────────────────────

def test_rate_limiter_cooldown():
    clear_cooldown(99, "attendance")

    allowed1, rem1 = check_and_set_cooldown(99, "attendance", cooldown_seconds=2)
    assert allowed1 is True
    assert rem1 == 0

    allowed2, rem2 = check_and_set_cooldown(99, "attendance", cooldown_seconds=2)
    assert allowed2 is False
    assert rem2 > 0

    time.sleep(2.1)
    allowed3, rem3 = check_and_set_cooldown(99, "attendance", cooldown_seconds=2)
    assert allowed3 is True
    assert rem3 == 0

    clear_cooldown(99, "attendance")


# ── Snapshot Formatter Tests ───────────────────────────────────────────────

def test_format_attendance_message_from_snapshot():
    snapshot = Snapshot(
        user_id=1,
        module_name="attendance",
        snapshot_json={
            "student_info": "ARADHY SINGH CHAUHAN {725MN1011}",
            "records": [
                {
                    "subject_name": "Underground Mining",
                    "subject_code": "MN2105",
                    "tc": "16",
                    "oa": "0",
                    "ua": "2",
                    "le": "0",
                }
            ],
        },
        snapshot_hash="dummyhash",
    )

    formatted = format_attendance_message_from_snapshot(snapshot)
    assert "ARADHY SINGH CHAUHAN" in formatted
    assert "Underground Mining" in formatted
    assert "TC: 16" in formatted
    assert "UA: 2" in formatted
