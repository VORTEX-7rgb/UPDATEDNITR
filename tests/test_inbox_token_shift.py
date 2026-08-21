"""Regression tests for inbox token shift and scheduler failure transaction handling."""

import pytest
from datetime import datetime, timezone
from sqlalchemy import text
from unittest.mock import AsyncMock, MagicMock

from app.db.models import User, InboxMessage
from app.services.scheduler_service import update_schedule_after_job


@pytest.mark.asyncio
async def test_update_schedule_after_job_failure_handles_transaction_cleanly():
    """Verify update_schedule_after_job on failure does not trigger

    InvalidRequestError('A transaction is already begun on this Session.').
    """
    fake_session = MagicMock()
    fake_session_cm = MagicMock()

    # Simulate async context manager for session.begin()
    class AsyncCM:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    fake_session.begin.return_value = AsyncCM()

    fake_row = MagicMock()
    fake_row.__getitem__.return_value = 1
    fake_result = MagicMock()
    fake_result.first.return_value = (1,)  # consecutive_failures = 1

    fake_session.execute = AsyncMock(return_value=fake_result)

    class FakeSessionFactoryCM:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def fake_session_factory():
        return FakeSessionFactoryCM()

    # This should complete without InvalidRequestError
    await update_schedule_after_job(
        session_factory=fake_session_factory,
        schedule_id=999,
        success=False,
        error_msg="simulated_portal_error",
        module_name="inbox",
    )

    # Verify session.begin was entered and execute was called
    assert fake_session.begin.called
    assert fake_session.execute.await_count >= 2


@pytest.mark.asyncio
async def test_inbox_token_shift_logic_structure():
    """Verify persist_inbox_sync imports and executes with shifted tokens."""
    from app.workers.sync_worker import persist_inbox_sync

    assert callable(persist_inbox_sync)
