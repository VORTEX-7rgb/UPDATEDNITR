import os
import sys
import asyncio
from unittest.mock import AsyncMock

# Add project root to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Set the selector event loop policy on Windows to avoid WinError 10054/121/64 during sockets disposal
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram.types import ErrorEvent, Update, Message, CallbackQuery
from app.bot.telegram import db_error_handler

async def test_db_error_handler():
    # Make sure stdout is flushed immediately
    sys.stdout.reconfigure(write_through=True)
    print("Starting unit tests for db_error_handler...")

    # 1. Test connection reset error for a Message update
    mock_message = AsyncMock(spec=Message)
    mock_message.answer = AsyncMock()  # explicit async mock for answer
    mock_update = AsyncMock(spec=Update)
    mock_update.message = mock_message
    mock_update.callback_query = None
    
    exc = ConnectionResetError("[WinError 10054] An existing connection was forcibly closed by the remote host")
    event = ErrorEvent(update=mock_update, exception=exc)
    
    result = await db_error_handler(event)
    assert result is True, "Should handle and intercept connection reset errors"
    mock_message.answer.assert_called_once()
    print("Test 1 Passed: Intercepted connection reset error in Message update and answered gracefully.")
    
    # 2. Test connection reset error for a CallbackQuery update
    mock_callback_query = AsyncMock(spec=CallbackQuery)
    mock_callback_query.answer = AsyncMock()  # explicit async mock for callback answer
    mock_update_cb = AsyncMock(spec=Update)
    mock_update_cb.message = None
    mock_update_cb.callback_query = mock_callback_query
    
    event_cb = ErrorEvent(update=mock_update_cb, exception=exc)
    result_cb = await db_error_handler(event_cb)
    assert result_cb is True, "Should handle and intercept connection reset errors in callback query"
    mock_callback_query.answer.assert_called_once()
    print("Test 2 Passed: Intercepted connection reset error in CallbackQuery and answered gracefully.")

    # 3. Test non-db error
    non_db_exc = ValueError("Some standard value error")
    event_non_db = ErrorEvent(update=mock_update, exception=non_db_exc)
    result_non_db = await db_error_handler(event_non_db)
    assert result_non_db is False, "Should NOT intercept non-database errors"
    print("Test 3 Passed: Standard exceptions are ignored and propagated.")
    
    print("\nSUCCESS: All database error handler unit tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_db_error_handler())
