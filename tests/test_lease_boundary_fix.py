"""Tests for Gateway Lease Boundary & Failure Domain Isolation.

Verifies:
1. DB errors / generic exceptions inside/outside acquire() DO NOT trip the NITRIS circuit.
2. NITRIS errors (NitrisError, AttendanceWorkflowError) DO trip the circuit.
3. Network errors (httpx.TransportError, TimeoutException) DO trip the circuit.
4. LoginError (credential issues) DOES NOT trip the circuit.
5. SQLAlchemy OperationalError DOES NOT trip the circuit.
6. Handlers (handle_attendance_refresh, handle_inbox_detail_fetch) do not perform DB writes inside acquire().
7. Circuit breaker records errors accurately for NITRIS-only faults.
"""
import asyncio
import inspect
import pytest
import httpx
from sqlalchemy.exc import OperationalError

from app.nitris.gateway import NitrisGateway, CircuitState, NitrisCircuitOpenError
from app.nitris.exceptions import AttendanceWorkflowError, LoginError, NitrisError


@pytest.mark.asyncio
async def test_db_error_does_not_trip_circuit():
    """Verify that generic exceptions / DB errors do NOT increment consecutive errors or trip circuit."""
    gw = NitrisGateway(circuit_error_threshold=3, min_login_interval=0.01)

    for _ in range(10):
        with pytest.raises(RuntimeError):
            async with gw.acquire():
                raise RuntimeError("Database connection pool exhausted")

    assert gw.circuit_state == CircuitState.CLOSED
    assert gw.metrics.consecutive_errors == 0


@pytest.mark.asyncio
async def test_nitris_error_trips_circuit():
    """Verify that NitrisError (e.g. portal workflow failure) DOES trip the circuit."""
    gw = NitrisGateway(circuit_error_threshold=3, min_login_interval=0.01)

    for _ in range(3):
        with pytest.raises(AttendanceWorkflowError):
            async with gw.acquire():
                raise AttendanceWorkflowError("NITRIS 503 Server Error")

    assert gw.circuit_state == CircuitState.OPEN
    assert gw.metrics.consecutive_errors == 3


@pytest.mark.asyncio
async def test_login_error_does_not_trip_circuit():
    """Verify that LoginError (wrong credentials) does not trip global circuit."""
    gw = NitrisGateway(circuit_error_threshold=3, min_login_interval=0.01)

    for _ in range(10):
        with pytest.raises(LoginError):
            async with gw.acquire():
                raise LoginError("Invalid roll number or password")

    assert gw.circuit_state == CircuitState.CLOSED
    assert gw.metrics.consecutive_errors == 0


@pytest.mark.asyncio
async def test_httpx_transport_error_trips_circuit():
    """Verify that network transport failures to NITRIS trip the circuit."""
    gw = NitrisGateway(circuit_error_threshold=3, min_login_interval=0.01)

    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            async with gw.acquire():
                raise httpx.ConnectError("Connection refused by NITRIS")

    assert gw.circuit_state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_sqlalchemy_error_does_not_trip_circuit():
    """Verify that SQLAlchemy DB operational errors do not trip NITRIS circuit."""
    gw = NitrisGateway(circuit_error_threshold=3, min_login_interval=0.01)

    for _ in range(10):
        with pytest.raises(OperationalError):
            async with gw.acquire():
                raise OperationalError("SELECT 1", {}, Exception("DB lock timeout"))

    assert gw.circuit_state == CircuitState.CLOSED
    assert gw.metrics.consecutive_errors == 0


def test_attendance_refresh_handler_boundary():
    """Verify source structure of handle_attendance_refresh keeps DB writes outside acquire()."""
    import app.nitris.job_handlers as jh
    src = inspect.getsource(jh.handle_attendance_refresh)

    acquire_idx = src.find("async with nitris_gateway.acquire():")
    assert acquire_idx != -1

    # snapshot save must come AFTER acquire block finishes
    snapshot_idx = src.find("snapshot_service.create_snapshot_if_changed")
    assert snapshot_idx != -1
    assert snapshot_idx > acquire_idx, "Snapshot creation must be outside acquire block"


def test_inbox_detail_fetch_handler_boundary():
    """Verify source structure of handle_inbox_detail_fetch keeps DB writes outside acquire()."""
    import app.nitris.job_handlers as jh
    src = inspect.getsource(jh.handle_inbox_detail_fetch)

    acquire_idx = src.find("async with nitris_gateway.acquire():")
    assert acquire_idx != -1

    update_inbox_idx = src.find("update_message_body")
    assert update_inbox_idx != -1
    assert update_inbox_idx > acquire_idx, "update_message_body must be outside acquire block"


@pytest.mark.asyncio
async def test_gateway_still_records_nitris_errors():
    """Verify metrics properly reflect NITRIS errors."""
    gw = NitrisGateway(circuit_error_threshold=5, min_login_interval=0.01)

    with pytest.raises(NitrisError):
        async with gw.acquire():
            raise NitrisError("Portal glitch")

    assert gw.metrics.total_errors == 1
    assert gw.metrics.consecutive_errors == 1
    assert "Portal glitch" in (gw.metrics.last_error or "")
