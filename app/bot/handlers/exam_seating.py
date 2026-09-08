"""Telegram bot handlers for the Examination Seating Chart & Room Visual Map."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Router, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.middlewares import note_registered_user
from app.config import config
from app.db.database import get_db_session
from app.db.repositories.user_repository import UserRepository
from app.nitris.job_queue import nitris_job_queue, Priority
from app.nitris.rate_limiter import operation_cooldown
from app.services.exam_seating_service import (
    get_user_schedule_state,
    get_cached_user_schedule,
    get_cached_room_layout,
    make_room_cache_key,
    _compute_student_view_from_cached_grid,
    render_exam_schedule_message,
    render_room_seating_message,
)
from app.ui import theme
from app.ui.surface import Surface
from app.utils import esc

logger = logging.getLogger(__name__)

router = Router(name="exam_seating_router")

COOLDOWN_EXAM_SEATING = config.COOLDOWN_EXAM_SEATING


def _kb_loading() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(theme.home_button())
    return builder.as_markup()


async def _enqueue_schedule_fetch(
    user_id: int,
    chat_id: int,
    message_id: int,
    surf: Surface,
    exam_type: str = "mid_sem",
    force_refresh: bool = False,
) -> None:
    token = getattr(surf, "owner_token", None)
    dedup_key = f"exam_seating_schedule:{user_id}:{exam_type}"
    try:
        await nitris_job_queue.enqueue(
            job_type="exam_seating_schedule_fetch",
            user_id=user_id,
            priority=Priority.HIGH,
            dedup_key=dedup_key,
            payload={
                "callback_chat_id": chat_id,
                "callback_message_id": message_id,
                "interaction_token": token,
                "exam_type": exam_type,
                "force_refresh": force_refresh,
            },
        )
    except Exception as e:
        logger.warning("Exam schedule fetch enqueue failed: %r", e)
        await surf.final(
            f"❌ <b>Could not start exam schedule fetch:</b>\n\n{esc(str(e))}",
            _kb_loading(),
        )


async def _enqueue_room_layout_fetch(
    user_id: int,
    chat_id: int,
    message_id: int,
    surf: Surface,
    exam_type: str = "mid_sem",
    item_num: int = 1,
    force_refresh: bool = False,
) -> None:
    token = getattr(surf, "owner_token", None)
    dedup_key = f"exam_room_layout:{user_id}:{exam_type}:{item_num}"
    try:
        await nitris_job_queue.enqueue(
            job_type="exam_room_layout_fetch",
            user_id=user_id,
            priority=Priority.HIGH,
            dedup_key=dedup_key,
            payload={
                "callback_chat_id": chat_id,
                "callback_message_id": message_id,
                "interaction_token": token,
                "exam_type": exam_type,
                "item_num": item_num,
                "force_refresh": force_refresh,
            },
        )
    except Exception as e:
        logger.warning("Exam room layout fetch enqueue failed: %r", e)
        await surf.final(
            f"❌ <b>Could not start room seating fetch:</b>\n\n{esc(str(e))}",
            _kb_loading(),
        )


# ── Commands ─────────────────────────────────────────────────────────────────

@router.message(Command("exams", "seating"), StateFilter(None))
async def cmd_exams(message: types.Message):
    """Command /exams or /seating: Display exam seating schedule."""
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

    note_registered_user(telegram_id, user.id)

    # Fast-path: check persistent or in-memory cache (0ms response)
    cached_sched = get_cached_user_schedule(user.id, "mid_sem")
    if cached_sched:
        res = {
            "success": True,
            "exam_type": "mid_sem",
            "items": cached_sched["items"],
            "roll_number": user.roll_number,
        }
        text, kb = render_exam_schedule_message(res)
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    cached_state = get_user_schedule_state(user.id)
    if cached_state:
        subpage_url, _, items_dicts = cached_state
        exam_type = "mid_sem" if "Mid_Semester.aspx" in str(subpage_url) else "end_sem"
        res = {
            "success": True,
            "exam_type": exam_type,
            "items": items_dicts,
            "roll_number": user.roll_number,
        }
        text, kb = render_exam_schedule_message(res)
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    status_msg = await message.answer(
        "⏳ <b>Loading NITRIS exam seating schedule…</b>",
        reply_markup=_kb_loading(),
        parse_mode=ParseMode.HTML,
    )
    surf = Surface(status_msg)
    await _enqueue_schedule_fetch(
        user_id=user.id,
        chat_id=message.chat.id,
        message_id=status_msg.message_id,
        surf=surf,
        exam_type="mid_sem",
    )


# ── Dashboard & Callback Queries ─────────────────────────────────────────────

@router.callback_query(F.data == "db_exams")
async def cb_dashboard_exams(callback: types.CallbackQuery):
    """Dashboard button: '💺 Exam Seating'."""
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

    note_registered_user(telegram_id, user.id)
    surf = Surface(callback.message)

    # Fast-path: check persistent or in-memory cache (0ms response)
    cached_sched = get_cached_user_schedule(user.id, "mid_sem")
    if cached_sched:
        res = {
            "success": True,
            "exam_type": "mid_sem",
            "items": cached_sched["items"],
            "roll_number": user.roll_number,
        }
        text, kb = render_exam_schedule_message(res)
        await surf.edit(text, kb)
        await callback.answer()
        return

    cached_state = get_user_schedule_state(user.id)
    if cached_state:
        subpage_url, _, items_dicts = cached_state
        exam_type = "mid_sem" if "Mid_Semester.aspx" in str(subpage_url) else "end_sem"
        res = {
            "success": True,
            "exam_type": exam_type,
            "items": items_dicts,
            "roll_number": user.roll_number,
        }
        text, kb = render_exam_schedule_message(res)
        await surf.edit(text, kb)
        await callback.answer()
        return

    await surf.edit("⏳ <b>Fetching exam schedule from NITRIS…</b>", _kb_loading())
    await _enqueue_schedule_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
        exam_type="mid_sem",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exams_type:"))
async def cb_exams_switch_type(callback: types.CallbackQuery):
    """Switch exam type view (Mid Sem <-> End Sem)."""
    parts = callback.data.split(":")
    exam_type = parts[1] if len(parts) > 1 else "mid_sem"

    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user or not user.credentials_valid:
        await callback.answer("User session invalid.", show_alert=True)
        return

    note_registered_user(telegram_id, user.id)
    surf = Surface(callback.message)

    # Fast-path: check persistent cache for switched exam_type (0ms response)
    cached_sched = get_cached_user_schedule(user.id, exam_type)
    if cached_sched:
        res = {
            "success": True,
            "exam_type": exam_type,
            "items": cached_sched["items"],
            "roll_number": user.roll_number,
        }
        text, kb = render_exam_schedule_message(res)
        await surf.edit(text, kb)
        await callback.answer()
        return

    type_name = "End Semester" if exam_type == "end_sem" else "Mid Semester"
    await surf.edit(f"⏳ <b>Loading {type_name} schedule…</b>", _kb_loading())
    await _enqueue_schedule_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
        exam_type=exam_type,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exams_refresh:"))
async def cb_exams_refresh(callback: types.CallbackQuery):
    """Force-refresh exam schedule from NITRIS."""
    parts = callback.data.split(":")
    exam_type = parts[1] if len(parts) > 1 else "mid_sem"

    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user or not user.credentials_valid:
        await callback.answer("User session invalid.", show_alert=True)
        return

    note_registered_user(telegram_id, user.id)

    # Enforce cooldown on manual refresh
    allowed, wait = operation_cooldown.check_and_update(user.id, "exam_seating_refresh", COOLDOWN_EXAM_SEATING)
    if not allowed:
        await callback.answer(f"⏳ Please wait {int(wait)}s before refreshing again.", show_alert=True)
        return

    surf = Surface(callback.message)
    await surf.edit("⏳ <b>Re-fetching fresh schedule from NITRIS…</b>", _kb_loading())
    await _enqueue_schedule_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
        exam_type=exam_type,
        force_refresh=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exams_seat:"))
async def cb_exams_seat_details(callback: types.CallbackQuery):
    """View seating layout & 360-degree neighbours for a specific exam item."""
    parts = callback.data.split(":")
    item_num = int(parts[1]) if len(parts) > 1 else 1
    exam_type = parts[2] if len(parts) > 2 else "mid_sem"

    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user or not user.credentials_valid:
        await callback.answer("User session invalid.", show_alert=True)
        return

    note_registered_user(telegram_id, user.id)
    surf = Surface(callback.message)

    # Check composite cache if schedule item is known (from memory state or disk cache)
    item = None
    state = get_user_schedule_state(user.id)
    if state:
        _, _, items_dicts = state
        item = next((it for it in items_dicts if it["num"] == item_num), None)
    if not item:
        cached_sched = get_cached_user_schedule(user.id, exam_type)
        if cached_sched:
            item = next((it for it in cached_sched.get("items", []) if it["num"] == item_num), None)

    if item:
        cache_key = make_room_cache_key(
            academic_year="2026-27",
            exam_type=exam_type,
            subject=item["subject"],
            date_str=item["date_str"],
            room_no=item["room_no"],
        )
        cached_layout = get_cached_room_layout(cache_key)
        if cached_layout:
            computed = _compute_student_view_from_cached_grid(cached_layout, user.roll_number)
            res = {
                "success": True,
                "cached": True,
                "item": item,
                "layout": computed,
                "roll_number": user.roll_number,
                "exam_type": exam_type,
            }
            text, kb = render_room_seating_message(res)
            await surf.edit(text, kb)
            await callback.answer()
            return

    await surf.edit("⏳ <b>Locating your seat in room chart…</b>", _kb_loading())
    await _enqueue_room_layout_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
        exam_type=exam_type,
        item_num=item_num,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exams_seat_refresh:"))
async def cb_exams_seat_refresh(callback: types.CallbackQuery):
    """Force-refresh room seating layout from NITRIS."""
    parts = callback.data.split(":")
    item_num = int(parts[1]) if len(parts) > 1 else 1
    exam_type = parts[2] if len(parts) > 2 else "mid_sem"

    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user or not user.credentials_valid:
        await callback.answer("User session invalid.", show_alert=True)
        return

    note_registered_user(telegram_id, user.id)

    allowed, wait = operation_cooldown.check_and_update(user.id, "exam_seat_refresh", COOLDOWN_EXAM_SEATING)
    if not allowed:
        await callback.answer(f"⏳ Please wait {int(wait)}s before refreshing.", show_alert=True)
        return

    surf = Surface(callback.message)
    await surf.edit("⏳ <b>Re-fetching room chart from NITRIS…</b>", _kb_loading())
    await _enqueue_room_layout_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
        exam_type=exam_type,
        item_num=item_num,
        force_refresh=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("exams_back:"))
async def cb_exams_back_to_schedule(callback: types.CallbackQuery):
    """Return from room seating view back to the schedule overview."""
    parts = callback.data.split(":")
    exam_type = parts[1] if len(parts) > 1 else "mid_sem"

    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

    if not user or not user.credentials_valid:
        await callback.answer("User session invalid.", show_alert=True)
        return

    note_registered_user(telegram_id, user.id)
    surf = Surface(callback.message)

    # Fast-path: check persistent or in-memory cache (0ms response)
    cached_sched = get_cached_user_schedule(user.id, exam_type)
    if cached_sched:
        res = {
            "success": True,
            "exam_type": exam_type,
            "items": cached_sched["items"],
            "roll_number": user.roll_number,
        }
        text, kb = render_exam_schedule_message(res)
        await surf.edit(text, kb)
        await callback.answer()
        return

    cached_state = get_user_schedule_state(user.id)
    if cached_state:
        _, _, items_dicts = cached_state
        res = {
            "success": True,
            "exam_type": exam_type,
            "items": items_dicts,
            "roll_number": user.roll_number,
        }
        text, kb = render_exam_schedule_message(res)
        await surf.edit(text, kb)
        await callback.answer()
        return

    await surf.edit("⏳ <b>Loading exam schedule…</b>", _kb_loading())
    await _enqueue_schedule_fetch(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        surf=surf,
        exam_type=exam_type,
    )
    await callback.answer()
