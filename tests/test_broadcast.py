"""Unit tests for the admin /broadcast send path.

Focuses on the safety-critical classification: blocked users, deactivated chats,
FloodWait exhaustion, and recovery — without touching Telegram or the DB.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramAPIError,
    TelegramRetryAfter,
)

from app.bot.handlers.admin import _send_broadcast_one, BROADCAST_MAX_RETRIES


def _retry_after(retry_after: float = 0.01):
    return TelegramRetryAfter(
        method="sendMessage",
        message="Too Many Requests: retry later",
        retry_after=retry_after,
    )


@pytest.mark.asyncio
async def test_broadcast_one_ok():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    assert await _send_broadcast_one(bot, 1, "hello") == "ok"
    bot.send_message.assert_awaited_once_with(chat_id=1, text="hello")


@pytest.mark.asyncio
async def test_broadcast_one_blocked():
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramForbiddenError(
            method="sendMessage", message="Forbidden: bot was blocked by the user"
        )
    )
    assert await _send_broadcast_one(bot, 1, "hello") == "blocked"


@pytest.mark.asyncio
async def test_broadcast_one_inactive():
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramAPIError(method="sendMessage", message="Bad Request: chat not found")
    )
    assert await _send_broadcast_one(bot, 1, "hello") == "inactive"


@pytest.mark.asyncio
async def test_broadcast_one_floodwait_exhausted(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.asyncio.sleep", AsyncMock())
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=_retry_after())
    assert await _send_broadcast_one(bot, 1, "hello") == "failed"
    assert bot.send_message.await_count == BROADCAST_MAX_RETRIES


@pytest.mark.asyncio
async def test_broadcast_one_floodwait_then_ok(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.asyncio.sleep", AsyncMock())
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[_retry_after(), MagicMock()])
    assert await _send_broadcast_one(bot, 1, "hello") == "ok"
    assert bot.send_message.await_count == 2
