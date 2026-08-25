"""Floating 🏠 Start button: routing, persistence wiring, FSM precedence,
and Plan B lifecycle (the bar is pinned for onboarding, retired for every
registered user so it stops swallowing the Android back softkey)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENCRYPTION_KEY", "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")

from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

import app.utils as app_utils
from app.bot.common import (
    START_BUTTON_TEXT,
    BAR_RETIRED_TEXT,
    build_bar_removal_markup,
    build_start_reply_keyboard,
)
from app.bot.handlers import registration as reg_mod

REPO_ROOT = Path(__file__).resolve().parents[1]


def _kb_button_texts(kb) -> list[str]:
    return [btn.text for row in kb.keyboard for btn in row]


def test_reply_keyboard_is_persistent_start():
    kb = build_start_reply_keyboard()
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True
    assert _kb_button_texts(kb) == [START_BUTTON_TEXT]


async def test_tap_routes_to_dashboard_and_clears_state():
    """Tapping 🏠 mid-ANY-flow (even while typing a password) must clear the
    FSM and open the dashboard — never leak text into an active flow. The
    via_start_bar flag rides along so cmd_start can retire a legacy bar."""
    state = AsyncMock()
    message = AsyncMock()
    message.text = START_BUTTON_TEXT
    message.from_user.id = 42

    with patch.object(reg_mod, "cmd_start", AsyncMock()) as fake_start:
        await reg_mod.start_button_tap(message, state)

    state.clear.assert_awaited_once()
    fake_start.assert_awaited_once_with(message, state, via_start_bar=True)


def _cmd_start_session_ctx(user_or_none):
    """Mocked get_db_session() ctx yielding one execute() → scalar_one_or_none()."""
    user_res = MagicMock()
    user_res.scalar_one_or_none.return_value = user_or_none
    session = MagicMock()
    session.execute = AsyncMock(return_value=user_res)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.fixture(autouse=False)
def _quiet_background(monkeypatch):
    """Neutralise cmd_start's fire-and-forget spawns (typing indicator +
    session warm) so tests never touch the network/DB from a task."""
    def _no_spawn(coro, name=None):
        coro.close()

    monkeypatch.setattr(app_utils, "spawn_tracked", _no_spawn)


async def test_registered_bar_tap_retires_bar_before_dashboard(monkeypatch, _quiet_background):
    """Legacy cleanup: a REGISTERED user tapping the still-pinned 🏠 bar gets
    ReplyKeyboardRemove BEFORE the dashboard bubble — and the dashboard keeps
    its inline buttons (a bubble can carry removal OR inline keys, not both)."""
    user = MagicMock()
    user.id = 1000

    inbox_repo_inst = MagicMock()
    inbox_repo_inst.get_unread_count = AsyncMock(return_value=0)
    monkeypatch.setattr(
        "app.db.repositories.inbox_repository.InboxRepository",
        MagicMock(return_value=inbox_repo_inst),
    )
    monkeypatch.setattr(reg_mod, "get_db_session", lambda: _cmd_start_session_ctx(user))
    monkeypatch.setattr(reg_mod, "render_dashboard", AsyncMock(return_value="DASH"))

    message = AsyncMock()
    message.from_user.id = 7
    message.chat.id = 7

    await reg_mod.cmd_start(message, AsyncMock(), via_start_bar=True)

    calls = message.answer.await_args_list
    assert len(calls) == 2, "exactly [bar-retirement notice, dashboard] expected"

    first, second = calls[0], calls[1]
    assert first.args[0] == BAR_RETIRED_TEXT
    assert isinstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)

    assert second.args[0] == "DASH"
    assert isinstance(second.kwargs["reply_markup"], types.InlineKeyboardMarkup)


async def test_unregistered_bar_tap_welcome_only_no_removal(monkeypatch, _quiet_background):
    """Unregistered 🏠 tap must behave EXACTLY as before — ONE welcome bubble
    re-pinning the persistent bar. No retirement bubble, no keyboard flicker."""
    monkeypatch.setattr(
        reg_mod, "get_db_session", lambda: _cmd_start_session_ctx(None)
    )

    message = AsyncMock()
    message.from_user.id = 7
    message.chat.id = 7

    await reg_mod.cmd_start(message, AsyncMock(), via_start_bar=True)

    calls = message.answer.await_args_list
    assert len(calls) == 1, "welcome only — retirement must be gated to registered users"
    kb = calls[0].kwargs["reply_markup"]
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.is_persistent is True


def test_bar_removal_helper_returns_keyboard_remove():
    remove = build_bar_removal_markup()
    assert isinstance(remove, ReplyKeyboardRemove)
    assert remove.remove_keyboard is True


async def test_welcome_message_carries_persistent_bar(monkeypatch):
    """Unregistered /start → welcome bubble pins the floating bar."""
    user_res = MagicMock()
    user_res.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=user_res)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(reg_mod, "get_db_session", lambda: ctx)

    message = AsyncMock()
    message.from_user.id = 7
    message.chat.id = 7

    await reg_mod.cmd_start(message, AsyncMock())

    kwargs = message.answer.await_args.kwargs
    kb = kwargs["reply_markup"]
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.is_persistent is True
    assert START_BUTTON_TEXT in _kb_button_texts(kb)


def test_registration_complete_does_not_break_the_bar():
    """The completion bubble must NOT attach the reply keyboard.

    editMessageText only accepts InlineKeyboardMarkup — passing the reply
    keyboard there raised a pydantic ValidationError ("1 validation error for
    EditMessageText") that the generic handler mislabeled as "A database
    error occurred during registration" on EVERY registration
    (incident 2026-08-24, VM logs 06:21:22 UTC).

    The bar still reaches every signup: it is pinned chat-wide by the
    unregistered /start welcome (pinned by
    test_welcome_message_carries_persistent_bar), which every student passes
    through before registering, and reply keyboards persist for the chat's
    lifetime."""
    src = (REPO_ROOT / "app/bot/handlers/registration.py").read_text(encoding="utf-8")
    anchor = src.index("Registration complete!")
    window = src[anchor:anchor + 400]
    assert "reply_markup=build_start_reply_keyboard()" not in window, (
        "editMessageText rejects ReplyKeyboardMarkup — attaching the bar to "
        "the completion edit crashes every registration with a fake DB error"
    )
    # The bar's chat-wide entry point for the incoming cohort is intact and
    # lives upstream of the completion path.
    assert "reply_markup=build_start_reply_keyboard()" in src
    assert src.index("reply_markup=build_start_reply_keyboard()") < anchor


def test_button_handler_registered_before_fsm_text_catchers():
    """The 🏠 tap handler MUST appear in router order BEFORE any FSM handler
    that captures arbitrary text (password/roll/query), otherwise typing the
    emoji mid-flow would leak into those flows."""
    src = (REPO_ROOT / "app/bot/handlers/registration.py").read_text(encoding="utf-8")
    btn_idx = src.index("async def start_button_tap")
    pwd_idx = src.index("Registration.waiting_for_password, F.text)")
    roll_idx = src.index("Registration.waiting_for_roll, F.text)")
    assert btn_idx < pwd_idx and btn_idx < roll_idx


def test_startup_menu_includes_start_command():
    src = (REPO_ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'BotCommand(command="start", description="🏠 Open Dashboard")' in src


def test_registration_success_retires_bar_after_dashboard_for_new_users():
    """PLAN B: on first-time signup the dashboard renders FIRST, then a
    separate bubble carries ReplyKeyboardRemove (one reply_markup per
    message — inline buttons and keyboard removal can never merge), gated
    to is_new_user so /forgot credential updates never re-notify."""
    src = (REPO_ROOT / "app/bot/handlers/registration.py").read_text(encoding="utf-8")
    anchor = src.index("Registration complete!")
    tail = src[anchor:]

    dash_idx = tail.index("reply_markup=get_dashboard_keyboard(unread_count)")
    remove_idx = tail.index("build_bar_removal_markup()")
    assert dash_idx < remove_idx, "dashboard must render before the bar is retired"

    between = tail[dash_idx:remove_idx]
    assert "is_new_user" in between, (
        "bar retirement must be gated to first-time signups — credential "
        "updates must not re-send the retirement notice"
    )


def test_render_failure_fallback_does_not_depend_on_the_bar():
    """The render-failure fallback used to say 'Tap 🏠 Start below' — wrong
    once Plan B may already have retired the bar. It must stay bar-agnostic."""
    src = (REPO_ROOT / "app/bot/handlers/registration.py").read_text(encoding="utf-8")
    assert "Tap 🏠 Start below" not in src

    anchor = src.index("You're registered!")
    window = src[anchor:anchor + 200]
    assert "☰ Menu or /start" in window
