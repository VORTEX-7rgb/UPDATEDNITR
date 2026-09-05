"""Telegram bot handlers for the NITRIS Academic Holiday Calendar."""
from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Optional

from aiogram import Router, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import config, IST
from app.db.database import get_db_session
from app.db.repositories.user_repository import UserRepository
from app.nitris.job_queue import nitris_job_queue, Priority
from app.nitris.rate_limiter import operation_cooldown
from app.services.holidays_service import (
    get_cached_holidays,
    get_cached_user_page,
)
from app.ui import copy, theme
from app.ui.surface import Surface, show
from app.utils import esc

logger = logging.getLogger(__name__)

router = Router(name="holidays_router")

COOLDOWN_HOLIDAYS = config.COOLDOWN_HOLIDAYS


# ── Keyboards ────────────────────────────────────────────────────────────────

def get_holidays_keyboard(
    prev_available: bool = True,
    next_available: bool = True,
    year: int = 2026,
    month: int = 9,
) -> types.InlineKeyboardMarkup:
    """Build navigation & action keyboard for the holiday calendar."""
    builder = InlineKeyboardBuilder()

    nav_row = []
    if prev_available:
        nav_row.append(types.InlineKeyboardButton(text="◀️ Previous", callback_data=f"holidays_nav:prev:{year}:{month}"))
    if next_available:
        nav_row.append(types.InlineKeyboardButton(text="Next ▶️", callback_data=f"holidays_nav:next:{year}:{month}"))
    if nav_row:
        builder.row(*nav_row)

    builder.row(
        types.InlineKeyboardButton(text="🔄 Refresh", callback_data="holidays_refresh"),
        types.InlineKeyboardButton(text="🏠 Home", callback_data="inbox_back_dashboard"),
    )
    return builder.as_markup()


def _kb_loading() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(theme.home_button())
    return builder.as_markup()


# ── Rendering ────────────────────────────────────────────────────────────────

def render_holidays_message(result: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    """Render structured holiday data into a user-facing Telegram HTML message."""
    month_label = result.get("month_label", "Calendar")
    holidays = result.get("holidays", [])
    month = result.get("month", 1)
    year = result.get("year", 2026)
    prev_available = result.get("prev_available", False)
    next_available = result.get("next_available", False)

    text = f"🎉 <b>NITR Academic Calendar · {esc(month_label)}</b>\n\n"

    today = datetime.now(IST).date()
    nearest_holiday: Optional[tuple[date, str]] = None
    min_future_days = 9999

    if holidays:
        text += "🌴 <b>Holidays & Closures:</b>\n"
        for h in holidays:
            day = h["day"]
            name = h["name"]
            is_trailing = h.get("is_trailing", False)

            # Resolve real date for accurate weekday & countdown
            h_month = month
            h_year = year
            if is_trailing:
                if day < 15:
                    # Trailing into next month
                    h_month = month + 1 if month < 12 else 1
                    h_year = year if month < 12 else year + 1
                else:
                    # Leading from previous month
                    h_month = month - 1 if month > 1 else 12
                    h_year = year if month > 1 else year - 1

            try:
                dt = date(h_year, h_month, day)
                date_str = dt.strftime("%d %b (%a)")
                diff = (dt - today).days
                if 0 <= diff < min_future_days:
                    min_future_days = diff
                    nearest_holiday = (dt, name)
            except ValueError:
                date_str = f"{day:02d} {month_label[:3]}"

            trailing_tag = " <i>(next month)</i>" if (is_trailing and day < 15) else ""
            text += f"• <b>{date_str}</b> — {esc(name)}{trailing_tag}\n"

        if nearest_holiday:
            dt, h_name = nearest_holiday
            if min_future_days == 0:
                countdown = "Today! 🎊"
            elif min_future_days == 1:
                countdown = "Tomorrow!"
            else:
                countdown = f"in {min_future_days} days!"
            text += f"\n⏳ <b>Next Holiday:</b>\n{esc(h_name)} · <i>{countdown}</i>\n"
    else:
        text += "<i>No official institute holidays listed on NITRIS for this month.</i>\n"

    kb = get_holidays_keyboard(prev_available, next_available, year=year, month=month)
    return text, kb


# ── Flow Dispatcher ──────────────────────────────────────────────────────────

async def _enqueue_holidays_fetch(
    user_id: int,
    chat_id: int,
    message_id: int,
    direction: Optional[str] = None,
    surf: Optional[Surface] = None,
    force_refresh: bool = False,
) -> None:
    """Enqueue holidays_fetch job to NitrisJobQueue."""
    token = getattr(surf, "owner_token", None)
    dedup_key = f"holidays_fetch:user:{user_id}"

    try:
        await nitris_job_queue.enqueue(
            job_type="holidays_fetch",
            user_id=user_id,
            priority=Priority.HIGH,
            dedup_key=dedup_key,
            payload={
                "callback_chat_id": chat_id,
                "callback_message_id": message_id,
                "interaction_token": token,
                "direction": direction,
                "current_page": None,
                "force_refresh": force_refresh,
            },
        )
    except Exception as e:
        logger.warning("Holidays fetch enqueue failed: %r", e)
        if surf:
            await surf.final(
                f"❌ <b>Could not start calendar fetch:</b>\n\n{esc(str(e))}",
                _kb_loading(),
            )


# ── Bot Handlers ─────────────────────────────────────────────────────────────

@router.message(Command("holidays"), StateFilter(None))
async def cmd_holidays(message: types.Message):
    """Command /holidays: Fetch and display current month's holiday calendar."""
    telegram_id = message.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user:
        await message.answer(
            "⚠️ You are not registered yet. Please send /start to register.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not user.credentials_valid:
        await message.answer(
            "⚠️ Your credentials are marked invalid. Use /forgot to update them.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Cache-first: if current month is in global cache, render INSTANTLY (0ms)
    cached = get_cached_holidays()
    if cached:
        text, kb = render_holidays_message(cached)
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    status_msg = await message.answer(
        "⏳ <b>Loading NITRIS holiday calendar…</b>",
        reply_markup=_kb_loading(),
        parse_mode=ParseMode.HTML,
    )
    surf = Surface(status_msg)
    await _enqueue_holidays_fetch(
        user_id=user.id,
        chat_id=message.chat.id,
        message_id=status_msg.message_id,
        surf=surf,
    )


@router.callback_query(F.data == "db_holidays")
async def cb_dashboard_holidays(callback: types.CallbackQuery):
    """Dashboard button: '🎉 Holidays'."""
    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user:
        await callback.answer("Please register via /start first.", show_alert=True)
        return
    if not user.credentials_valid:
        await callback.answer("Credentials invalid. Use /forgot to update.", show_alert=True)
        return

    surf = Surface(callback.message)

    # Cache-first: if current month is in global cache, render INSTANTLY (0ms)
    cached = get_cached_holidays()
    if cached:
        text, kb = render_holidays_message(cached)
        await surf.edit(text, kb)
        await callback.answer()
        return

    await surf.edit("⏳ <b>Fetching holiday calendar from NITRIS…</b>", _kb_loading())
    await _enqueue_holidays_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
    )
    await callback.answer()


@router.callback_query(F.data == "holidays_refresh")
async def cb_holidays_refresh(callback: types.CallbackQuery):
    """Force-refresh current month holidays from NITRIS."""
    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user or not user.credentials_valid:
        await callback.answer("Please login via /start first.", show_alert=True)
        return

    allowed, wait = await operation_cooldown.check(
        user.id, "holidays", cooldown_seconds=COOLDOWN_HOLIDAYS
    )
    if not allowed:
        await callback.answer(f"⏳ Please wait {wait}s before refreshing again.", show_alert=True)
        return

    surf = Surface(callback.message)
    await surf.edit("🔄 <i>Refreshing holiday calendar from NITRIS…</i>", _kb_loading())

    await _enqueue_holidays_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
        force_refresh=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("holidays_nav:"))
async def cb_holidays_nav(callback: types.CallbackQuery):
    """Navigate calendar to previous or next month."""
    # Immediately acknowledge tap so Telegram button spinner stops instantly
    await callback.answer()

    parts = callback.data.split(":")
    direction = parts[1]  # "prev" or "next"
    curr_year = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 2026
    curr_month = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 9

    if direction == "next":
        t_month = curr_month + 1 if curr_month < 12 else 1
        t_year = curr_year if curr_month < 12 else curr_year + 1
    else:
        t_month = curr_month - 1 if curr_month > 1 else 12
        t_year = curr_year if curr_month > 1 else curr_year - 1

    surf = Surface(callback.message)

    # Fast-path: check global persistent cache directly (0ms, zero DB query)
    cached_target = get_cached_holidays(t_year, t_month)
    if cached_target:
        text, kb = render_holidays_message(cached_target)
        await surf.edit(text, kb)
        return

    # Cache miss: verify user credentials and scrape from NITRIS
    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user or not user.credentials_valid:
        await surf.edit(
            "⚠️ Session invalid. Please login via /start first.",
            _kb_loading(),
        )
        return

    nav_label = "previous" if direction == "prev" else "next"
    await surf.edit(f"⏳ <i>Loading {nav_label} month from NITRIS…</i>", _kb_loading())

    await _enqueue_holidays_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        direction=direction,
        surf=surf,
    )
