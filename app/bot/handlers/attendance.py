"""Attendance command + cache-first attendance refresh helper."""

import logging
import asyncio

from aiogram import Router, types
from aiogram.filters import Command, StateFilter
from aiogram.enums import ParseMode
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import get_db_session
from app.db.models import User

from app.bot.common import format_attendance_message, format_attendance_message_from_snapshot

logger = logging.getLogger(__name__)

router = Router(name="attendance_router")


async def fetch_attendance_for_callback(callback: types.CallbackQuery, user: User):
    """Cache-first attendance fetch."""
    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTENDANCE_REFRESH
    allowed, wait = await operation_cooldown.check(
        user.id, "attendance_refresh", cooldown_seconds=COOLDOWN_ATTENDANCE_REFRESH
    )
    if not allowed:
        try:
            await callback.answer(
                f"⏳ Please wait {wait}s before refreshing again.", show_alert=True
            )
        except Exception:
            pass
        return

    from app.db.repositories.snapshot_repository import SnapshotRepository
    async with get_db_session() as session:
        snapshot_repo = SnapshotRepository(session)
        cached_snapshot = await snapshot_repo.get_latest_snapshot(user.id, "attendance")

    if cached_snapshot and getattr(cached_snapshot, "snapshot_json", None) and "records" in cached_snapshot.snapshot_json:
        status_msg = await callback.message.answer(
            format_attendance_message_from_snapshot(cached_snapshot)
            + "\n\n<i>🔄 Refreshing from NITRIS in background...</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        status_msg = await callback.message.answer(
            "⏳ <b>No cached data yet.</b>\n\nFetching attendance from NITRIS...",
            parse_mode=ParseMode.HTML,
        )

    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError

    try:
        future = await nitris_job_queue.enqueue(
            job_type="attendance_refresh",
            user_id=user.id,
            priority=Priority.HIGH,
            dedup_key=f"attendance_refresh:user:{user.id}",
            payload={
                "callback_chat_id": status_msg.chat.id,
                "callback_message_id": status_msg.message_id,
            },
        )

        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            if result.get("success") and result.get("data"):
                data = result["data"]
                await status_msg.edit_text(
                    format_attendance_message(data),
                    parse_mode=ParseMode.HTML,
                )
        except asyncio.TimeoutError:
            await status_msg.edit_text(
                "⏳ <b>Refresh is taking longer than expected.</b>\n\n"
                "NITRIS may be slow right now. Your attendance will update automatically "
                "when the refresh completes.",
                parse_mode=ParseMode.HTML,
            )

    except NitrisCircuitOpenError:
        await status_msg.edit_text(
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "The system is protecting the portal from overload. "
            "Please try again in ~60 seconds.\n\n"
            f"<i>Showing cached data from your last sync:</i>\n\n"
            + (format_attendance_message_from_snapshot(cached_snapshot)
               if cached_snapshot and getattr(cached_snapshot, "snapshot_json", None) and "records" in cached_snapshot.snapshot_json
               else "<i>No cached data available.</i>"),
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("attendance"), StateFilter(None))
async def cmd_attendance(message: types.Message):
    """Cache-first /attendance command."""
    telegram_id = message.from_user.id

    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

    if not user:
        await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
        return

    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTENDANCE_REFRESH
    allowed, wait = await operation_cooldown.check(
        user.id, "attendance_refresh", cooldown_seconds=COOLDOWN_ATTENDANCE_REFRESH
    )
    if not allowed:
        await message.answer(
            f"⏳ You just refreshed attendance. Please wait {wait}s before trying again.",
            parse_mode=ParseMode.HTML,
        )
        return

    from app.db.repositories.snapshot_repository import SnapshotRepository
    async with get_db_session() as session:
        snapshot_repo = SnapshotRepository(session)
        cached_snapshot = await snapshot_repo.get_latest_snapshot(user.id, "attendance")

    if cached_snapshot and getattr(cached_snapshot, "snapshot_json", None) and "records" in cached_snapshot.snapshot_json:
        status_msg = await message.answer(
            format_attendance_message_from_snapshot(cached_snapshot)
            + "\n\n<i>🔄 Refreshing from NITRIS in background...</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        status_msg = await message.answer(
            "⏳ <b>No cached data yet.</b>\n\nFetching attendance from NITRIS...",
            parse_mode=ParseMode.HTML,
        )

    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError

    try:
        future = await nitris_job_queue.enqueue(
            job_type="attendance_refresh",
            user_id=user.id,
            priority=Priority.HIGH,
            dedup_key=f"attendance_refresh:user:{user.id}",
            payload={
                "callback_chat_id": status_msg.chat.id,
                "callback_message_id": status_msg.message_id,
            },
        )

        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            if result.get("success") and result.get("data"):
                data = result["data"]
                await status_msg.edit_text(
                    format_attendance_message(data),
                    parse_mode=ParseMode.HTML,
                )
        except asyncio.TimeoutError:
            await status_msg.edit_text(
                "⏳ <b>Refresh is taking longer than expected.</b>\n\n"
                "NITRIS may be slow right now. Your attendance will update automatically "
                "when the refresh completes.",
                parse_mode=ParseMode.HTML,
            )

    except NitrisCircuitOpenError:
        await status_msg.edit_text(
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "The system is protecting the portal from overload. "
            "Please try again in ~60 seconds.\n\n"
            f"<i>Showing cached data from your last sync:</i>\n\n"
            + (format_attendance_message_from_snapshot(cached_snapshot)
               if cached_snapshot and getattr(cached_snapshot, "snapshot_json", None) and "records" in cached_snapshot.snapshot_json
               else "<i>No cached data available.</i>"),
            parse_mode=ParseMode.HTML,
        )
