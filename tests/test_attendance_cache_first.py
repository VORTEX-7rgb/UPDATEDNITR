"""CACHE-FIRST attendance taps (user contract).

Tapping Get Attendance — even DURING the anti-spam cooldown — must
immediately render the LATEST CACHED attendance into the tapped bubble,
with an inline countdown note. It must never:
  * depend on a second callback.answer() alert (the dashboard handler
    already consumes the one-shot answer), or
  * leave the bubble untouched.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.nitris.rate_limiter import operation_cooldown


def _records():
    return [{
        "subject_code": "CS2001", "subject_name": "Operating Systems",
        "faculty": "Prof X", "tc": "10", "ua": "2", "le": "0", "oa": "2",
        "ltp": "3-0-0",
    }]


class FakeMessage:
    def __init__(self):
        self.edits: list[str] = []
        self.markups: list = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)
        self.markups.append(reply_markup)
        return self


class FakeCallback:
    def __init__(self):
        self.message = FakeMessage()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return True


@pytest.fixture
def cached_summary():
    from app.services.attendance_health import summarize
    return summarize(_records())


@pytest.mark.asyncio
async def test_blocked_tap_still_renders_cached_attendance_with_countdown(
    monkeypatch, cached_summary
):
    """3rd/4th tap within 60s: bubble edits to cached data + countdown."""
    from app.bot.handlers.attendance import fetch_attendance_for_callback

    cb = FakeCallback()
    user = SimpleNamespace(id=42)

    # Force the cooldown into BLOCKED state for this user.
    operation_cooldown._cooldowns["42:attendance_refresh"] = time.monotonic() + 55

    async def fake_summary(uid):
        return cached_summary

    monkeypatch.setattr(
        "app.bot.handlers.attendance._load_summary", fake_summary
    )

    await fetch_attendance_for_callback(cb, user)

    assert len(cb.message.edits) == 1, "bubble must be edited exactly once"
    body = cb.message.edits[0]
    assert "ATTENDANCE" in body
    assert "CS2001" in body, "cached subject rows must be visible"
    assert "Next live refresh in" in body, "inline countdown must be shown"
    assert cb.message.markups[0] is not None, "navigation keyboard must stay"

    # No second answer attempt (dashboard handler owns the single ack).
    assert cb.answers == []


@pytest.mark.asyncio
async def test_blocked_tap_without_cache_shows_countdown_note(monkeypatch):
    """No snapshot yet + blocked tap → friendly note + countdown, no crash."""
    from app.bot.handlers.attendance import fetch_attendance_for_callback

    cb = FakeCallback()
    user = SimpleNamespace(id=43)
    operation_cooldown._cooldowns["43:attendance_refresh"] = time.monotonic() + 40

    async def fake_summary(uid):
        return None

    monkeypatch.setattr(
        "app.bot.handlers.attendance._load_summary", fake_summary
    )
    await fetch_attendance_for_callback(cb, user)

    body = cb.message.edits[0]
    assert "Next live refresh in" in body
    assert "No attendance on file yet" in body