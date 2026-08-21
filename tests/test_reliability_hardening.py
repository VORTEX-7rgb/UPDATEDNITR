"""Phase 6 & 7 Reliability Hardening Tests.

Tests:
  - Admission control configuration
  - Job-level exponential backoff retry on transient errors
  - Immediate failure (no retry) on permanent LoginError
  - Max retries exhaustion
  - In-flight cancellation via cancel_dedup()
  - Relogin on SessionExpiredError routes through nitris_gateway._do_login
  - Relogin failure triggers quarantine
  - Scheduler backpressure configuration
  - Lane split & Phase 6 config verification
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ENCRYPTION_KEY"] = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
os.environ["BOT_TOKEN"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/test"


def test_phase6_and_lane_configs_present():
    """All Phase 6 admission control, retry, and lane split configs exist."""
    from app.config import config

    assert hasattr(config, "REGISTRATION_MAX_CONCURRENT")
    assert config.REGISTRATION_MAX_CONCURRENT == 4
    assert hasattr(config, "QP_METADATA_MAX_CONCURRENT")
    assert config.QP_METADATA_MAX_CONCURRENT == 3
    assert hasattr(config, "JOB_MAX_RETRIES")
    assert config.JOB_MAX_RETRIES == 3
    assert hasattr(config, "JOB_RETRY_BASE_DELAY")
    assert config.JOB_RETRY_BASE_DELAY == 2.0
    assert hasattr(config, "NITRIS_INTERACTIVE_WORKERS")
    assert config.NITRIS_INTERACTIVE_WORKERS == 4
    assert hasattr(config, "NITRIS_JOB_WORKERS")
    assert config.NITRIS_JOB_WORKERS >= 10
    assert hasattr(config, "SCHEDULER_MAX_QUEUE_DEPTH")
    assert config.SCHEDULER_MAX_QUEUE_DEPTH == 50


@pytest.mark.asyncio
async def test_job_retry_on_transient_error():
    """Job queue retries transient errors with backoff and succeeds when retry passes."""
    from app.nitris.job_queue import NitrisJobQueue, Priority

    queue = NitrisJobQueue(num_workers=1)
    call_count = 0

    @queue.handler("transient_job")
    async def handle_transient(job):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Temporary connection timeout")
        return {"success": True, "attempts": call_count}

    await queue.start()
    try:
        with patch("app.config.config.JOB_RETRY_BASE_DELAY", 0.01):
            future = await queue.enqueue("transient_job", user_id=1, priority=Priority.HIGH)
            result = await asyncio.wait_for(future, timeout=2.0)
            assert result["success"] is True
            assert result["attempts"] == 2
            assert call_count == 2
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_no_retry_on_login_error():
    """Job queue fails immediately without retry on LoginError."""
    from app.nitris.job_queue import NitrisJobQueue, Priority
    from app.nitris.exceptions import LoginError

    queue = NitrisJobQueue(num_workers=1)
    call_count = 0

    @queue.handler("auth_job")
    async def handle_auth(job):
        nonlocal call_count
        call_count += 1
        raise LoginError("Invalid roll or password")

    await queue.start()
    try:
        future = await queue.enqueue("auth_job", user_id=1, priority=Priority.HIGH)
        with pytest.raises(LoginError):
            await asyncio.wait_for(future, timeout=2.0)
        assert call_count == 1  # No retries on auth error!
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_max_retries_exhaustion():
    """Job fails permanently with exception once JOB_MAX_RETRIES is exhausted."""
    from app.nitris.job_queue import NitrisJobQueue, Priority

    queue = NitrisJobQueue(num_workers=1)
    call_count = 0

    @queue.handler("always_fail_job")
    async def handle_fail(job):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Permanent network down")

    await queue.start()
    try:
        with patch("app.config.config.JOB_MAX_RETRIES", 2), \
             patch("app.config.config.JOB_RETRY_BASE_DELAY", 0.01):
            future = await queue.enqueue("always_fail_job", user_id=1, priority=Priority.HIGH)
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(future, timeout=2.0)
            # Initial (0) + 2 retries (1, 2) = 3 total attempts
            assert call_count == 3
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_cancel_dedup_in_flight():
    """cancel_dedup() cancels the future of an in-flight job by its dedup_key."""
    from app.nitris.job_queue import NitrisJobQueue, Priority

    queue = NitrisJobQueue(num_workers=0)  # No workers running
    future = await queue.enqueue(
        "slow_job", user_id=1, priority=Priority.HIGH, dedup_key="cancel_test:1"
    )

    assert not future.done()
    cancelled = queue.cancel_dedup("cancel_test:1")
    assert cancelled is True
    assert future.cancelled()


def test_cancel_dedup_nonexistent():
    """cancel_dedup() returns False for nonexistent dedup keys."""
    from app.nitris.job_queue import NitrisJobQueue

    queue = NitrisJobQueue(num_workers=0)
    assert queue.cancel_dedup("nonexistent:999") is False


@pytest.mark.asyncio
async def test_attendance_relogin_uses_gateway():
    """On SessionExpiredError, get_attendance_data re-logins through nitris_gateway._do_login."""
    from app.services.attendance_service import get_attendance_data
    from app.nitris.exceptions import SessionExpiredError
    from app.nitris.parser import AttendanceResult

    mock_client = AsyncMock()
    # First fetch raises SessionExpiredError, second fetch returns mock HTML
    mock_client.fetch_attendance.side_effect = [
        SessionExpiredError("ASP.NET session expired"),
        "<html>valid attendance html</html>",
    ]

    mock_result = MagicMock(spec=AttendanceResult)

    with patch("app.nitris.gateway.nitris_gateway._do_login", new_callable=AsyncMock) as mock_do_login, \
         patch("app.services.attendance_service.parse_attendance_html", return_value=mock_result):
        result = await get_attendance_data("125MN1011", "secret", client=mock_client, user_id=10)

        assert mock_do_login.call_count == 1
        assert mock_do_login.call_args[0][1] == "125MN1011"
        assert mock_do_login.call_args[0][2] == "secret"
        assert mock_client.login.call_count == 0  # Bare client.login() must NOT be called directly!
        assert result == mock_result


@pytest.mark.asyncio
async def test_attendance_relogin_failure_quarantines_user():
    """If re-login fails with LoginError, on_login_failure is called for user_id."""
    from app.services.attendance_service import get_attendance_data
    from app.nitris.exceptions import SessionExpiredError, LoginError

    mock_client = AsyncMock()
    mock_client.fetch_attendance.side_effect = SessionExpiredError("Session dropped")

    with patch("app.nitris.gateway.nitris_gateway._do_login", side_effect=LoginError("Password changed")), \
         patch("app.nitris.auth_gate.on_login_failure", new_callable=AsyncMock) as mock_quarantine:
        with pytest.raises(LoginError):
            await get_attendance_data("125MN1011", "wrong_pass", client=mock_client, user_id=42)

        assert mock_quarantine.call_count == 1
        assert mock_quarantine.call_args[0][0] == 42
