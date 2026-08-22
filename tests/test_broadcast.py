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


@pytest.mark.asyncio
async def test_broadcast_one_pin_ok():
    bot = MagicMock()
    sent = MagicMock()
    sent.message_id = 123
    bot.send_message = AsyncMock(return_value=sent)
    bot.pin_chat_message = AsyncMock()

    assert await _send_broadcast_one(bot, 1, "hello", pin=True) == "ok"
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1, message_id=123, disable_notification=True
    )


@pytest.mark.asyncio
async def test_broadcast_one_pin_fails_but_delivered():
    bot = MagicMock()
    sent = MagicMock()
    sent.message_id = 123
    bot.send_message = AsyncMock(return_value=sent)
    bot.pin_chat_message = AsyncMock(
        side_effect=TelegramAPIError(method="pinChatMessage", message="Bad Request")
    )

    # Message is still delivered; only the pin failed.
    assert await _send_broadcast_one(bot, 1, "hello", pin=True) == "pin_failed"

# ── /unpin — remove last pinned broadcast from every user's chat ────────────

import pytest as _pytest
from app.bot.handlers.admin import (
    _unpin_one,
    _run_unpin_all,
    _last_pinned_by_chat,
)


@_pytest.fixture(autouse=True)
def _clean_pin_map():
    _last_pinned_by_chat.clear()
    yield
    _last_pinned_by_chat.clear()


def _unpin_not_found():
    return TelegramAPIError(
        method="unpinChatMessage", message="Bad Request: message to unpin not found"
    )


@_pytest.mark.asyncio
async def test_unpin_one_ok_with_recorded_message():
    bot = MagicMock()
    bot.unpin_chat_message = AsyncMock()
    _last_pinned_by_chat[1] = 555

    assert await _unpin_one(bot, 1) == "ok"
    # Targets the EXACT message we pinned earlier.
    bot.unpin_chat_message.assert_awaited_once_with(chat_id=1, message_id=555)
    assert 1 not in _last_pinned_by_chat, "record must be consumed"


@_pytest.mark.asyncio
async def test_unpin_one_cold_map_falls_back_to_latest_pinned():
    bot = MagicMock()
    bot.unpin_chat_message = AsyncMock()

    assert await _unpin_one(bot, 2) == "ok"
    # No recorded id → Telegram unpins the most recent pinned message.
    bot.unpin_chat_message.assert_awaited_once_with(chat_id=2)


@_pytest.mark.asyncio
async def test_unpin_one_nothing_pinned():
    bot = MagicMock()
    bot.unpin_chat_message = AsyncMock(side_effect=_unpin_not_found())
    assert await _unpin_one(bot, 3) == "no_pin"


@_pytest.mark.asyncio
async def test_unpin_one_blocked():
    bot = MagicMock()
    bot.unpin_chat_message = AsyncMock(
        side_effect=TelegramForbiddenError(
            method="unpinChatMessage", message="Forbidden: bot was blocked by the user"
        )
    )
    assert await _unpin_one(bot, 4) == "blocked"


@_pytest.mark.asyncio
async def test_unpin_one_inactive():
    bot = MagicMock()
    bot.unpin_chat_message = AsyncMock(
        side_effect=TelegramAPIError(method="unpinChatMessage", message="Bad Request: chat not found")
    )
    assert await _unpin_one(bot, 5) == "inactive"


@_pytest.mark.asyncio
async def test_unpin_one_floodwait_exhausted(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.asyncio.sleep", AsyncMock())
    bot = MagicMock()
    bot.unpin_chat_message = AsyncMock(
        side_effect=TelegramRetryAfter(method="unpinChatMessage", message="Flood", retry_after=0.01)
    )
    assert await _unpin_one(bot, 6) == "failed"
    assert bot.unpin_chat_message.await_count == BROADCAST_MAX_RETRIES


@_pytest.mark.asyncio
async def test_run_unpin_all_summary_counts():
    bot = MagicMock()

    async def mixed_unpin(chat_id, message_id=None):
        if chat_id == 1:
            return None                      # ok
        if chat_id == 2:
            raise _unpin_not_found()         # no_pin
        raise TelegramForbiddenError(        # blocked
            method="unpinChatMessage", message="Forbidden"
        )

    bot.unpin_chat_message = AsyncMock(side_effect=mixed_unpin)
    bot.edit_message_text = AsyncMock()

    await _run_unpin_all(bot, [1, 2, 3], status_chat_id=99, status_message_id=7)

    summary = bot.edit_message_text.await_args.kwargs.get("text", "")
    assert "Unpin complete" in summary
    assert "✅ Unpinned: <b>1</b>" in summary
    assert "⚪ Nothing pinned: <b>1</b>" in summary
    assert "🚫 Blocked the bot: <b>1</b>" in summary
