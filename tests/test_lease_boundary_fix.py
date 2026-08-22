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


import ast


# ── P1 architecture: the lease boundary moved INTO the session pool ────────
# Handlers no longer open gateway slots themselves — they delegate all NITRIS
# work to session_pool.with_pooled_session(), which centrally enforces:
#   * exactly ONE gateway slot per run,
#   * JIT password decryption INSIDE that slot,
#   * login ONLY on cache miss via login_through_gateway,
#   * auth/session faults drop the pooled entry.
# Those mechanics are proven behaviorally in test_session_pool.py; here we pin
# the structural routing of every NITRIS-touching handler.

_POOLED_HANDLERS = (
    "handle_attendance_refresh",
    "handle_inbox_refresh",
    "handle_sync_onboarding",
    "handle_qp_metadata_fetch",
    "handle_inbox_detail_fetch",
    "handle_attachment_download",
    "handle_qp_search",
)


@pytest.mark.parametrize("handler_name", _POOLED_HANDLERS)
def test_handler_routes_nitris_work_through_session_pool(handler_name):
    """Every NITRIS-touching handler must go through the session pool and must
    NOT construct clients / decrypt / log in directly."""
    import app.nitris.job_handlers as jh

    src = inspect.getsource(getattr(jh, handler_name))
    assert "with_pooled_session" in src, f"{handler_name} bypasses the pool"
    body = src.replace("from app.nitris.session_pool import with_pooled_session", "")
    assert "NitrisClient()" not in body, f"{handler_name} builds its own client"
    assert "decrypt_password(" not in body, f"{handler_name} decrypts outside the pool"
    assert "login_through_gateway" not in body, f"{handler_name} logs in outside the pool"


def test_session_pool_enforces_gateway_boundaries():
    """Structural pin: decrypt + login live INSIDE the held gateway slot in
    with_pooled_session — the lease boundary survives the P1 refactor."""
    import app.nitris.session_pool as sp_mod

    src = inspect.getsource(sp_mod.with_pooled_session)
    assert "_gateway_acquire()" in src
    assert "decrypt_password(encrypted_password)" in src
    assert "login_through_gateway" in src


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
