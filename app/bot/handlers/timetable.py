"""Telegram bot handlers for Timetable & 'Now & Next Class' features."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from aiogram import Router, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter

from app.config import config, IST
from app.db.database import get_db_session
from app.db.repositories.user_repository import UserRepository
from app.db.repositories.timetable_repository import TimetableRepository
from app.nitris.job_queue import nitris_job_queue, Priority
from app.nitris.rate_limiter import operation_cooldown
from app.services.now_next_service import (
    resolve_now_and_next,
    format_now_next_message,
    format_day_schedule,
    WEEKDAY_LABELS,
)
from app.utils import esc
from app.ui.surface import show

logger = logging.getLogger(__name__)

router = Router(name="timetable_router")

COOLDOWN_TIMETABLE_SYNC = config.COOLDOWN_TIMETABLE_SYNC  # seconds (env-tunable)


def _fmt_wait(seconds: int) -> str:
    """Humanize a cooldown remainder — '45s', '12m', '3h 05m'."""
    if seconds < 90:
        return f"{seconds}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"


# ── Keyboards ────────────────────────────────────────────────────────────────

def get_now_next_keyboard() -> types.InlineKeyboardMarkup:
    """Keyboard attached to the Now & Next Class message."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="🔄 Refresh", callback_data="tt_now_next"),
                types.InlineKeyboardButton(text="📅 Full Timetable", callback_data="tt_view_full"),
            ],
            [
                types.InlineKeyboardButton(text="⚡ Sync from NITRIS", callback_data="tt_sync"),
            ],
            [
                types.InlineKeyboardButton(text="🏠 Home", callback_data="inbox_back_dashboard"),
            ],
        ]
    )


def get_not_synced_keyboard() -> types.InlineKeyboardMarkup:
    """Keyboard shown when user has no timetable synced yet."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🔄 Sync Timetable Now", callback_data="tt_sync"
                )
            ],
            [
                types.InlineKeyboardButton(text="🏠 Home", callback_data="inbox_back_dashboard"),
            ],
        ]
    )


def get_day_selector_keyboard(selected_day: int) -> types.InlineKeyboardMarkup:
    """Day selector bar for viewing weekly timetable (Mon-Fri)."""
    days = [("Mon", 0), ("Tue", 1), ("Wed", 2), ("Thu", 3), ("Fri", 4)]
    buttons = []
    for label, day_idx in days:
        text = f"• {label} •" if day_idx == selected_day else label
        buttons.append(
            types.InlineKeyboardButton(text=text, callback_data=f"tt_day_{day_idx}")
        )

    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            buttons,
            [
                types.InlineKeyboardButton(text="⏰ Now & Next", callback_data="tt_now_next"),
                types.InlineKeyboardButton(text="🔄 Sync from NITRIS", callback_data="tt_sync"),
            ],
            [
                types.InlineKeyboardButton(text="🏠 Home", callback_data="inbox_back_dashboard"),
            ],
        ]
    )


# ── Core Handlers ────────────────────────────────────────────────────────────

async def _handle_now_next_display(telegram_user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    """Load user timetable from DB, resolve against current IST time, and format response."""
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_user_id)
        if not user:
            return (
                "⚠️ <b>You are not registered yet.</b>\nPlease send /start to register.",
                types.InlineKeyboardMarkup(inline_keyboard=[]),
            )

        tt_repo = TimetableRepository(session)
        entries = await tt_repo.get_user_timetable(user.id)
        last_synced = max((e.synced_at for e in entries if e.synced_at is not None), default=None)

    if not entries:
        return (
            "📅 <b>Timetable Not Synced Yet</b>\n\n"
            "You haven't synced your class schedule from NITRIS yet.\n"
            "Tap the button below to fetch your timetable automatically!",
            get_not_synced_keyboard(),
        )

    result = resolve_now_and_next(entries, datetime.now(IST))
    text = format_now_next_message(result, last_synced)
    return text, get_now_next_keyboard()


async def _handle_day_display(telegram_user_id: int, weekday: int) -> tuple[str, types.InlineKeyboardMarkup]:
    """Format one day's timetable schedule."""
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_user_id)
        if not user:
            return (
                "⚠️ <b>You are not registered yet.</b>\nPlease send /start to register.",
                types.InlineKeyboardMarkup(inline_keyboard=[]),
            )

        # LAYER 1: keep the portal session warm while browsing the schedule.
        from app.utils import spawn_tracked
        from app.services.session_warmer import request_session_warm
        spawn_tracked(request_session_warm(user.id), name=f"sw-tt-{user.id}")

        tt_repo = TimetableRepository(session)
        entries = await tt_repo.get_user_timetable(user.id)
        last_synced = max((e.synced_at for e in entries if e.synced_at is not None), default=None)

    if not entries:
        return (
            "📅 <b>Timetable Not Synced Yet</b>\n\n"
            "You haven't synced your class schedule from NITRIS yet.\n"
            "Tap the button below to fetch your timetable automatically!",
            get_not_synced_keyboard(),
        )

    text = format_day_schedule(entries, weekday)
    if last_synced:
        synced_ist = last_synced.astimezone(IST) if last_synced.tzinfo else last_synced.replace(tzinfo=IST)
        text += f"\n\n🔄 <i>Last synced: {synced_ist.strftime('%d %b %Y, %I:%M %p IST')}</i>"

    return text, get_day_selector_keyboard(weekday)


async def _enqueue_sync(
    telegram_user_id: int,
    callback_chat_id: int,
    callback_message_id: int,
) -> tuple[bool, str]:
    """Enqueue a timetable sync job to NitrisJobQueue."""
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_user_id)
        if not user:
            return False, "⚠️ You are not registered yet. Please send /start to register."
        if not user.credentials_valid:
            return False, "⚠️ Your credentials are marked invalid. Use /forgot to update them."
        user_id = user.id

    # Check anti-spam cooldown BEFORE enqueueing...
    allowed, wait = await operation_cooldown.check(
        user_id, "timetable_sync", cooldown_seconds=COOLDOWN_TIMETABLE_SYNC
    )
    if not allowed:
        return False, f"⏳ Please wait <b>{_fmt_wait(wait)}</b> before syncing timetable again."

    # ...but RELEASE it if the enqueue fails — a rejected job must never
    # leave the user locked out with nothing scheduled.
    dedup_key = f"{config.TIMETABLE_SYNC_DEDUP_PREFIX}:{user_id}"
    try:
        await nitris_job_queue.enqueue(
            job_type="timetable_sync",
            user_id=user_id,
            priority=Priority.HIGH,
            payload={
                "callback_chat_id": callback_chat_id,
                "callback_message_id": callback_message_id,
            },
            dedup_key=dedup_key,
        )
    except RuntimeError as e:
        await operation_cooldown.clear(user_id, "timetable_sync")
        logger.warning("Timetable sync enqueue rejected: %r", e)
        from app.ui.copy import QUEUE_BUSY
        return False, QUEUE_BUSY

    return True, "🔄 <b>Syncing timetable with NITRIS...</b>\n<i>Please wait a few seconds.</i>"


# ── Commands ─────────────────────────────────────────────────────────────────

@router.message(Command("now"), StateFilter("*"))
@router.message(Command("next"), StateFilter("*"))
@router.message(Command("class"), StateFilter("*"))
async def cmd_now_next(message: types.Message):
    """Command /now, /next, /class: Show current and next upcoming class."""
    text, kb = await _handle_now_next_display(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.message(Command("timetable"), StateFilter("*"))
@router.message(Command("schedule"), StateFilter("*"))
async def cmd_timetable(message: types.Message):
    """Command /timetable: Show timetable for today or full weekly schedule."""
    today_weekday = min(datetime.now(IST).weekday(), 4)  # Cap at Friday (4) if weekend (Sat/Sun)
    text, kb = await _handle_day_display(message.from_user.id, today_weekday)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.message(Command("timetablesync"), StateFilter("*"))
async def cmd_timetable_sync(message: types.Message):
    """Command /timetablesync: On-demand timetable sync from NITRIS."""
    status_msg = await message.answer(
        "🔄 <b>Starting timetable sync...</b>",
        parse_mode=ParseMode.HTML,
    )
    ok, text = await _enqueue_sync(
        message.from_user.id,
        message.chat.id,
        status_msg.message_id,
    )
    if not ok:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML)


# ── Callback Handlers ────────────────────────────────────────────────────────

@router.callback_query(F.data == "tt_now_next")
async def cb_now_next(callback: types.CallbackQuery):
    """Callback tt_now_next: Display current & next class."""
    try:
        await callback.answer()
    except Exception:
        pass
    text, kb = await _handle_now_next_display(callback.from_user.id)
    # PERF F1: not-modified-safe render with fresh-send fallback.
    await show(callback.message, text, reply_markup=kb)


@router.callback_query(F.data == "tt_view_full")
async def cb_view_full(callback: types.CallbackQuery):
    """Callback tt_view_full: Display full day timetable with selector."""
    try:
        await callback.answer()
    except Exception:
        pass
    today_weekday = min(datetime.now(IST).weekday(), 5)
    text, kb = await _handle_day_display(callback.from_user.id, today_weekday)
    await show(callback.message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("tt_day_"))
async def cb_select_day(callback: types.CallbackQuery):
    """Callback tt_day_{N}: Switch displayed day."""
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        weekday = int(callback.data.split("tt_day_")[-1])
    except ValueError:
        weekday = 0

    text, kb = await _handle_day_display(callback.from_user.id, weekday)
    # PERF F1: not-modified-safe render + fallback. The old silent
    # `except: pass` left the OLD day on screen with zero user feedback.
    await show(callback.message, text, reply_markup=kb)


@router.callback_query(F.data == "tt_sync")
async def cb_sync(callback: types.CallbackQuery):
    """Callback tt_sync: Trigger manual timetable sync from NITRIS."""
    try:
        await callback.answer("⏳ Requesting timetable from NITRIS...")
    except Exception:
        pass
    ok, text = await _enqueue_sync(
        callback.from_user.id,
        callback.message.chat.id,
        callback.message.message_id,
    )
    try:
        await show(callback.message, text)
    except Exception:
        await callback.message.answer(text, parse_mode=ParseMode.HTML)

