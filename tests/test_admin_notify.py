"""Tests for admin new-user notification system."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from app.bot.handlers.admin_notify import notify_admins_of_new_user


@pytest.mark.asyncio
async def test_notify_admins_success():
    """Test notifying all configured admins."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch("app.bot.handlers.admin_notify.config") as mock_config:
        mock_config.ADMIN_TELEGRAM_IDS = frozenset({111111, 222222})
        
        notified = await notify_admins_of_new_user(bot, "725MN1011")
        
        assert notified == 2
        assert bot.send_message.call_count == 2
        
        # Verify text format
        first_call = bot.send_message.call_args_list[0]
        text_sent = first_call.kwargs.get("text", "")
        assert "New user registered" in text_sent
        assert "725MN1011" in text_sent
        assert "password" not in text_sent.lower()


@pytest.mark.asyncio
async def test_notify_admins_empty_list():
    """Test that empty admin list returns 0 without crashing."""
    bot = AsyncMock()

    with patch("app.bot.handlers.admin_notify.config") as mock_config:
        mock_config.ADMIN_TELEGRAM_IDS = frozenset()
        
        notified = await notify_admins_of_new_user(bot, "725MN1011")
        assert notified == 0
        assert bot.send_message.call_count == 0


@pytest.mark.asyncio
async def test_notify_admins_partial_failure_blocked():
    """Test when one admin blocked the bot, other admin still receives notification."""
    bot = AsyncMock()
    
    async def fake_send(chat_id, text, parse_mode):
        if chat_id == 111111:
            raise TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
        return MagicMock()

    bot.send_message = AsyncMock(side_effect=fake_send)

    with patch("app.bot.handlers.admin_notify.config") as mock_config:
        mock_config.ADMIN_TELEGRAM_IDS = [111111, 222222]
        
        notified = await notify_admins_of_new_user(bot, "725MN1011")
        assert notified == 1
        assert bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_notify_admins_bad_request():
    """Test when Telegram returns bad request for an invalid ID."""
    bot = AsyncMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")
    )

    with patch("app.bot.handlers.admin_notify.config") as mock_config:
        mock_config.ADMIN_TELEGRAM_IDS = [999999]
        
        notified = await notify_admins_of_new_user(bot, "725MN1011")
        assert notified == 0


@pytest.mark.asyncio
async def test_notify_admins_html_escaped():
    """Verify roll number is properly HTML-escaped."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch("app.bot.handlers.admin_notify.config") as mock_config:
        mock_config.ADMIN_TELEGRAM_IDS = [111111]
        
        await notify_admins_of_new_user(bot, "<script>alert(1)</script>")
        call = bot.send_message.call_args
        text = call.kwargs.get("text", "")
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


@pytest.mark.asyncio
async def test_notify_admins_no_pii_leakage():
    """Verify that message body contains ONLY the roll number and no secrets."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch("app.bot.handlers.admin_notify.config") as mock_config:
        mock_config.ADMIN_TELEGRAM_IDS = [111111]
        
        await notify_admins_of_new_user(bot, "125CS0001")
        call = bot.send_message.call_args
        text = call.kwargs.get("text", "")
        
        assert "125CS0001" in text
        assert "telegram" not in text.lower()
        assert "password" not in text.lower()
        assert "secret" not in text.lower()
        assert "token" not in text.lower()
