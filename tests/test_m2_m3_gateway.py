"""Regression tests for gateway fixes:

M2 — HALF-OPEN circuit admits exactly ONE recovery probe; everyone else is
     rejected fast instead of stampeding a recovering portal. A failed probe
     re-trips OPEN immediately; a per-user verdict (portal responded) closes
     it. is_circuit_open() is a pure predicate with no side effects.

M3 — The paced-login wait runs WITHOUT occupying a portal concurrency slot,
     so queued logins can never starve interactive taps of capacity.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.nitris.exceptions import AttendanceWorkflowError, LoginError
from app.nitris.gateway import (
    CircuitBreakerOpenError,
    CircuitState,
    NitrisGateway,
)


# ── M2: single-probe HALF_OPEN ───────────────────────────────────────────────


async def _trip_open(gw: NitrisGateway):
    """Force the circuit OPEN by burning the full error threshold."""
    for _ in range(max(1, gw.circuit_error_threshold)):
        with pytest.raises(AttendanceWorkflowError):
            async with gw.acquire():
                raise AttendanceWorkflowError("portal down")
    assert gw.circuit_state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_m2_half_open_admits_exactly_one_probe():
    gw = NitrisGateway(
        max_concurrent=5, min_login_interval=0.0,
        circuit_error_threshold=1, circuit_recovery_seconds=0.15,
    )
    await _trip_open(gw)

    # Rejected while fully OPEN.
    with pytest.raises(CircuitBreakerOpenError):
        async with gw.acquire():
            pass

    await asyncio.sleep(0.2)  # recovery window expires

    entered: list[int] = []
    rejected = 0

    async def worker(i: int):
        nonlocal rejected
        try:
            async with gw.acquire():
                entered.append(i)
                await asyncio.sleep(0.08)  # hold the probe open briefly
        except CircuitBreakerOpenError:
            rejected += 1

    await asyncio.gather(*(worker(i) for i in range(5)))

    assert len(entered) == 1, "exactly ONE probe may enter HALF_OPEN"
    assert rejected == 4, "everyone else must fail fast, not stampede"
    assert gw.circuit_state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_m2_failed_probe_retrips_open_immediately():
    gw = NitrisGateway(
        max_concurrent=3, min_login_interval=0.0,
        circuit_error_threshold=10,  # high — only the probe path may trip
        circuit_recovery_seconds=0.2,
    )
    await _trip_open(gw)
    await asyncio.sleep(0.25)

    # Probe itself fails with a PORTAL fault → instant re-trip.
    with pytest.raises(AttendanceWorkflowError):
        async with gw.acquire():
            raise AttendanceWorkflowError("still down")

    assert gw.circuit_state == CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenError):
        async with gw.acquire():
            pass


@pytest.mark.asyncio
async def test_m2_per_user_fault_during_probe_closes_circuit():
    """A LoginError means the PORTAL responded (user-level verdict) — that is
    recovery evidence, so the probe closes the circuit."""
    gw = NitrisGateway(
        max_concurrent=3, min_login_interval=0.0,
        circuit_error_threshold=10, circuit_recovery_seconds=0.2,
    )
    await _trip_open(gw)
    await asyncio.sleep(0.25)

    client = SimpleNamespace(login=AsyncMock(side_effect=LoginError("Invalid credentials.")))
    with pytest.raises(LoginError):
        async with gw.acquire():
            await gw.login_through_gateway(client, "roll", "pw", user_id=7)

    assert gw.circuit_state == CircuitState.CLOSED
    assert gw.is_quarantined(7)


@pytest.mark.asyncio
async def test_m2_is_circuit_open_is_pure_predicate():
    gw = NitrisGateway(
        max_concurrent=2, min_login_interval=0.0,
        circuit_error_threshold=1, circuit_recovery_seconds=0.05,
    )
    await _trip_open(gw)

    # Before due → open.
    assert gw.is_circuit_open() is True
    assert gw.circuit_state == CircuitState.OPEN, "predicate must not mutate state"

    await asyncio.sleep(0.08)
    # Due → traffic allowed, but STILL no lazy state flip from a read.
    assert gw.is_circuit_open() is False
    assert gw.circuit_state == CircuitState.OPEN


# ── M3: slot-free login pacing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m3_paced_login_does_not_starve_capacity():
    """A queued paced login must NOT hold its portal slot while sleeping —
    an interactive tap arriving mid-pace gets served immediately."""
    gw = NitrisGateway(max_concurrent=1, min_login_interval=0.4)
    client = SimpleNamespace(login=AsyncMock())

    # Login #1 primes last_login_time (no pacing on the very first login).
    async with gw.acquire():
        await gw.login_through_gateway(client, "r", "p", user_id=1)

    # Login #2 must pace ~0.4s. While it waits, its slot must be RELEASED.
    a_done = {"flag": False}

    async def paced_login():
        async with gw.acquire():
            await gw.login_through_gateway(client, "r", "p", user_id=2)
        a_done["flag"] = True

    task_a = asyncio.create_task(paced_login())
    await asyncio.sleep(0.06)  # let A settle into its paced wait

    t0 = time.monotonic()
    async with gw.acquire():   # B: plain non-login tap
        pass
    b_elapsed = time.monotonic() - t0

    assert b_elapsed < 0.15, f"B queued behind A's pacing ({b_elapsed:.3f}s)!"
    assert not a_done["flag"], "A finished before B — slot was NOT released"

    await task_a               # A completes after its full paced wait

    assert gw.metrics.active_requests == 0, "slot counter leaked"
    assert client.login.await_count == 2


@pytest.mark.asyncio
async def test_m3_slot_accounting_returns_to_baseline_under_load():
    """Release/reacquire bookkeeping must stay perfectly balanced under
    concurrent paced logins."""
    gw = NitrisGateway(max_concurrent=3, min_login_interval=0.02)

    async def one(i: int):
        client = SimpleNamespace(login=AsyncMock())
        async with gw.acquire():
            await gw.login_through_gateway(client, "r", "p", user_id=i)

    await asyncio.gather(*(one(i) for i in range(6)))

    assert gw.metrics.active_requests == 0, "slot counter leaked under load"
    assert gw.metrics.total_logins == 6
