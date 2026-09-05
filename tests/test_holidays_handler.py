"""Unit tests for holidays Telegram bot handler."""
import pytest
from app.bot.handlers.holidays import render_holidays_message, get_holidays_keyboard


def test_render_holidays_message_empty():
    result = {
        "month_label": "September 2026",
        "month": 9,
        "year": 2026,
        "holidays": [],
        "prev_available": True,
        "next_available": True,
    }
    text, kb = render_holidays_message(result)
    assert "NITR Academic Calendar · September 2026" in text
    assert "No official institute holidays" in text
    assert kb is not None
    # Check buttons
    buttons = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "◀️ Previous" in buttons
    assert "Next ▶️" in buttons
    assert "🔄 Refresh" in buttons
    assert "🏠 Home" in buttons


def test_render_holidays_message_with_holidays():
    result = {
        "month_label": "September 2026",
        "month": 9,
        "year": 2026,
        "holidays": [
            {
                "day": 4,
                "name": "Janmashtami",
                "month": 9,
                "year": 2026,
                "is_trailing": False,
            },
            {
                "day": 2,
                "name": "Mahatma Gandhis Birthday",
                "month": 9,
                "year": 2026,
                "is_trailing": True,
            },
        ],
        "prev_available": True,
        "next_available": False,
    }
    text, kb = render_holidays_message(result)
    assert "NITR Academic Calendar · September 2026" in text
    assert "Janmashtami" in text
    assert "Mahatma Gandhis Birthday" in text
    assert "(next month)" in text

    buttons = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "◀️ Previous" in buttons
    assert "Next ▶️" not in buttons  # next_available is False
    assert "🔄 Refresh" in buttons
    assert "🏠 Home" in buttons
