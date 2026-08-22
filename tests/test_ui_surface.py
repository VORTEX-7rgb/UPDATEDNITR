"""Tests for the UI surface layer — edit-what-you-tapped with safe fallbacks."""
from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from app.ui.surface import Surface, show


class FakeMsg:
    """Scriptable stand-in for aiogram Message."""

    def __init__(self, *, edit_error: Exception | None = None):
        self.edits: list[str] = []
        self.sent: list[str] = []
        self.chat = type("C", (), {"id": 42})()
        self._edit_error = edit_error

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        if self._edit_error is not None:
            raise self._edit_error
        self.edits.append(text)
        return self

    async def answer(self, text, reply_markup=None, parse_mode=None):
        # Real aiogram returns a NEW Message here.
        fresh = FakeMsg()
        fresh.sent = self.sent  # share the recorder for assertions
        fresh.sent.append(text)
        return fresh


def _bad(msg: str) -> TelegramBadRequest:
    # aiogram 3.x signature: (method, message)
    return TelegramBadRequest(method="editMessageText", message=msg)


KB = InlineKeyboardMarkup(inline_keyboard=[])


@pytest.mark.asyncio
async def test_show_edits_in_place():
    m = FakeMsg()
    out = await show(m, "hello", KB)  # type: ignore[arg-type]
    assert m.edits == ["hello"]
    assert not m.sent
    assert out is m


@pytest.mark.asyncio
async def test_show_swallows_message_not_modified():
    m = FakeMsg(edit_error=_bad("Bad Request: message is not modified"))
    out = await show(m, "same", KB)  # type: ignore[arg-type]
    # Idempotent re-render must NOT crash and must NOT spawn a duplicate bubble.
    assert m.edits == []
    assert not m.sent
    assert out is m


@pytest.mark.asyncio
async def test_show_falls_back_to_send_when_undeletable():
    m = FakeMsg(edit_error=_bad("Bad Request: message can't be edited"))
    out = await show(m, "fresh", KB)  # type: ignore[arg-type]
    assert m.edits == []
    assert m.sent == ["fresh"]
    assert out is not None and out is not m


@pytest.mark.asyncio
async def test_show_falls_back_on_any_edit_failure():
    m = FakeMsg(edit_error=RuntimeError("boom"))
    out = await show(m, "still delivered", KB)  # type: ignore[arg-type]
    assert m.sent == ["still delivered"]


@pytest.mark.asyncio
async def test_surface_final_cancels_pending_pokes():
    import asyncio

    m = FakeMsg()
    surf = Surface(m)  # type: ignore[arg-type]

    task = surf.poke_later(0.05, "SLOW")
    await asyncio.sleep(0.01)
    await surf.final("DONE")

    # Poke is cancelled (or gen-invalidated) — it must never render.
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "SLOW" not in m.edits
    assert m.edits[-1] == "DONE"


@pytest.mark.asyncio
async def test_surface_poke_fires_when_no_final_arrives():
    import asyncio

    m = FakeMsg()
    surf = Surface(m)  # type: ignore[arg-type]
    await surf.edit("working")
    task = surf.poke_later(0.02, "slow persona")
    await asyncio.wait_for(task, timeout=1.0)
    assert "slow persona" in m.edits


@pytest.mark.asyncio
async def test_surface_navigation_race_drops_stale_final():
    """If user navigates away to a new screen (via show()), a slow in-flight Surface.final()
    from the previous interaction must NOT overwrite the newer screen."""
    m = FakeMsg()
    surf_attendance = Surface(m)  # User tapped Attendance (interaction 1)
    await surf_attendance.edit("Updating attendance...")
    assert m.edits[-1] == "Updating attendance..."

    # User clicks 'Home' before attendance finishes -> show() renders Home (interaction 2)
    await show(m, "🏠 Home Dashboard", KB)
    assert m.edits[-1] == "🏠 Home Dashboard"

    # Slow attendance job finally finishes and tries to call final()
    result = await surf_attendance.final("📊 85.5% Attendance")

    # The final edit MUST be dropped because user moved to Home
    assert result is None
    assert m.edits[-1] == "🏠 Home Dashboard"
    assert "📊 85.5% Attendance" not in m.edits
