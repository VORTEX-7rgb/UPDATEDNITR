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


@pytest.mark.asyncio
async def test_broadcast_one_copy_message_ok():
    bot = MagicMock()
    bot.copy_message = AsyncMock(return_value=MagicMock())
    assert await _send_broadcast_one(bot, 1, copy_from_chat_id=100, copy_from_message_id=200) == "ok"
    bot.copy_message.assert_awaited_once_with(chat_id=1, from_chat_id=100, message_id=200)


@pytest.mark.asyncio
async def test_broadcast_one_copy_message_with_caption():
    from aiogram.enums import ParseMode
    bot = MagicMock()
    bot.copy_message = AsyncMock(return_value=MagicMock())
    assert (
        await _send_broadcast_one(
            bot, 1, copy_from_chat_id=100, copy_from_message_id=200, caption="campus notice"
        )
        == "ok"
    )
    bot.copy_message.assert_awaited_once_with(
        chat_id=1, from_chat_id=100, message_id=200, caption="campus notice", parse_mode=ParseMode.HTML
    )


@pytest.mark.asyncio
async def test_broadcast_one_copy_message_pin_ok():
    bot = MagicMock()
    sent = MagicMock()
    sent.message_id = 456
    bot.copy_message = AsyncMock(return_value=sent)
    bot.pin_chat_message = AsyncMock()

    assert (
        await _send_broadcast_one(
            bot, 1, pin=True, copy_from_chat_id=100, copy_from_message_id=200
        )
        == "ok"
    )
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1, message_id=456, disable_notification=True
    )


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


# ── _broadcast_common multi-mode tests ─────────────────────────────────────

from app.bot.handlers.admin import _broadcast_common, BROADCAST_MAX_CAPTION_LEN


@_pytest.mark.asyncio
async def test_broadcast_common_non_admin_ignored(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.is_admin", lambda uid: False)
    msg = MagicMock()
    msg.from_user.id = 9999
    msg.answer = AsyncMock()

    await _broadcast_common(msg, pin=False, command_name="broadcast")
    msg.answer.assert_not_called()


@_pytest.mark.asyncio
async def test_broadcast_common_direct_photo_with_caption(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.is_admin", lambda uid: True)
    spawn_mock = MagicMock(side_effect=lambda coro, **kw: coro.close())
    monkeypatch.setattr("app.utils.spawn_tracked", spawn_mock)

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [101, 102]
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=result_mock)

    class DummyDBCtx:
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    monkeypatch.setattr("app.bot.handlers.admin.get_db_session", lambda: DummyDBCtx())

    msg = MagicMock()
    msg.from_user.id = 123
    msg.chat.id = 555
    msg.message_id = 777
    msg.text = None
    msg.caption = "/broadcast New semester schedule released!"
    msg.photo = [MagicMock()]
    msg.video = None
    msg.document = None
    msg.animation = None
    msg.reply_to_message = None

    status_msg = MagicMock()
    status_msg.chat.id = 555
    status_msg.message_id = 888
    msg.answer = AsyncMock(return_value=status_msg)

    await _broadcast_common(msg, pin=False, command_name="broadcast")

    msg.answer.assert_awaited_once()
    assert "Broadcast (Photo) started" in msg.answer.await_args[0][0]
    spawn_mock.assert_called_once()


@_pytest.mark.asyncio
async def test_broadcast_common_reply_to_media(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.is_admin", lambda uid: True)
    spawn_mock = MagicMock(side_effect=lambda coro, **kw: coro.close())
    monkeypatch.setattr("app.utils.spawn_tracked", spawn_mock)

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [101]
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=result_mock)

    class DummyDBCtx:
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    monkeypatch.setattr("app.bot.handlers.admin.get_db_session", lambda: DummyDBCtx())

    replied = MagicMock()
    replied.message_id = 999
    replied.photo = None
    replied.video = MagicMock()  # video attachment
    replied.animation = None
    replied.document = None
    replied.audio = None
    replied.voice = None

    msg = MagicMock()
    msg.from_user.id = 123
    msg.chat.id = 555
    msg.message_id = 1000
    msg.text = "/broadcastpin Notice video"
    msg.caption = None
    msg.photo = None
    msg.video = None
    msg.document = None
    msg.animation = None
    msg.reply_to_message = replied

    status_msg = MagicMock()
    status_msg.chat.id = 555
    status_msg.message_id = 1001
    msg.answer = AsyncMock(return_value=status_msg)

    await _broadcast_common(msg, pin=True, command_name="broadcastpin")

    assert "Broadcast (Video) + Pin started" in msg.answer.await_args[0][0]
    spawn_mock.assert_called_once()


@_pytest.mark.asyncio
async def test_broadcast_common_caption_too_long(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.is_admin", lambda uid: True)
    msg = MagicMock()
    msg.from_user.id = 123
    msg.chat.id = 555
    msg.message_id = 777
    msg.text = None
    msg.caption = "/broadcast " + ("A" * (BROADCAST_MAX_CAPTION_LEN + 10))
    msg.photo = [MagicMock()]
    msg.video = None
    msg.document = None
    msg.animation = None
    msg.reply_to_message = None
    msg.answer = AsyncMock()

    await _broadcast_common(msg, pin=False, command_name="broadcast")

    msg.answer.assert_awaited_once()
    assert "Caption too long" in msg.answer.await_args[0][0]

