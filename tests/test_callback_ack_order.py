"""Tests for Phase 1: Instant callback acknowledgement order."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ENCRYPTION_KEY"] = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
os.environ["BOT_TOKEN"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5432/test"


@pytest.mark.asyncio
async def test_dashboard_callback_answers_before_db_query():
    """handle_dashboard_callbacks must answer callback before querying DB."""
    from app.bot.handlers.registration import handle_dashboard_callbacks

    call_order = []

    callback = AsyncMock()
    callback.data = "db_attendance"
    callback.from_user.id = 123456789
    callback.message = AsyncMock()

    async def mock_answer(*args, **kwargs):
        call_order.append("callback_answer")

    callback.answer.side_effect = mock_answer

    state = AsyncMock()

    async def mock_execute(*args, **kwargs):
        call_order.append("db_query")
        res = MagicMock()
        user_mock = MagicMock()
        user_mock.id = 1
        user_mock.telegram_id = 123456789
        res.scalar_one_or_none.return_value = user_mock
        return res

    mock_session = AsyncMock()
    mock_session.execute.side_effect = mock_execute

    with patch("app.bot.handlers.registration.get_db_session") as mock_db_ctx, \
         patch("app.bot.handlers.registration.fetch_attendance_for_callback") as mock_fetch:
        mock_db_ctx.return_value.__aenter__.return_value = mock_session

        await handle_dashboard_callbacks(callback, state)

    assert "callback_answer" in call_order
    assert "db_query" in call_order
    assert call_order.index("callback_answer") < call_order.index("db_query"), (
        "callback.answer() MUST be called before executing DB queries"
    )
