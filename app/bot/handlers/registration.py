"""Registration, credential update, deregistration, and dashboard-callback handlers."""

import logging
import re
import html
from datetime import datetime, timezone

from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.services.attendance_service import get_attendance_data
from app.nitris.exceptions import LoginError
from app.db.database import get_db_session
from app.db.repositories.user_repository import UserRepository
from app.services.snapshot_service import SnapshotService
from app.db.models import User, SyncState

from app.bot.fsm import Registration, Deregistration, InboxSearch
from app.bot.common import format_dashboard_text, get_dashboard_keyboard
from app.bot.handlers.attendance import fetch_attendance_for_callback
from app.bot.handlers.papers import cmd_papers

logger = logging.getLogger(__name__)

router = Router(name="registration_router")


# --- Global Command Overrides ---

@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("❌ No active process to cancel.")
        return
    await state.clear()
    await message.answer("❌ Process cancelled.")


@router.message(Command("forgot"), StateFilter("*"))
@router.message(Command("register"), StateFilter("*"))
async def cmd_forgot(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(Registration.waiting_for_roll)
    await message.answer(
        "🔄 <b>Credential Update / Registration</b>\n\n"
        "Please enter your NITRIS Roll Number (e.g. <b>125AI0003</b>):",
        parse_mode=ParseMode.HTML
    )


@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        unread_count = 0
        if user:
            from app.db.repositories.inbox_repository import InboxRepository
            inbox_repo = InboxRepository(session)
            unread_count = await inbox_repo.get_unread_count(user.id)

    if user:
        await state.clear()
        text = format_dashboard_text(user, unread_count)
        await message.answer(text, reply_markup=get_dashboard_keyboard(unread_count), parse_mode=ParseMode.HTML)
    else:
        await state.clear()
        await state.set_state(Registration.waiting_for_roll)
        await message.answer("👋 Welcome to NitrClaw!\n\nPlease enter your NITRIS Roll Number (e.g. 125AI0003):")


# --- FSM Command Shielding ---

@router.message(Registration.waiting_for_roll, F.text.startswith("/"))
@router.message(Registration.waiting_for_password, F.text.startswith("/"))
@router.message(Registration.verifying, F.text.startswith("/"))
@router.message(Deregistration.waiting_for_confirm, F.text.startswith("/"))
@router.message(InboxSearch.waiting_for_query, F.text.startswith("/"))
async def registration_command_shield(message: types.Message):
    await message.answer(
        "⚠️ <b>Registration or active process is in progress.</b>\n\n"
        "Please complete the active steps, or send /cancel to abort the process before running other commands.",
        parse_mode=ParseMode.HTML
    )


@router.message(Registration.verifying)
async def verification_shield(message: types.Message):
    await message.answer(
        "⏳ <b>Verification with NITRIS is currently in progress.</b>\n\n"
        "Please wait a few seconds for the current request to complete.",
        parse_mode=ParseMode.HTML
    )


# --- FSM State Input Handlers ---

@router.message(Registration.waiting_for_roll)
async def process_roll(message: types.Message, state: FSMContext):
    roll = message.text.strip().upper()

    if not re.match(r"^\d{3}[A-Z]{2}\d{4}$", roll):
        await message.answer(
            "❌ <b>Invalid Roll Number format.</b>\n\n"
            "The expected format is strictly 9 characters (e.g., <b>125AI0003</b>).\n\n"
            "Please try entering your roll number again, or send /cancel to abort:",
            parse_mode=ParseMode.HTML
        )
        return

    await state.update_data(roll=roll)
    await message.answer("🔑 Format accepted! Now, please enter your NITRIS Password:")
    await state.set_state(Registration.waiting_for_password)


@router.message(Registration.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    telegram_id = message.from_user.id

    if len(password) > 100:
        await message.answer("❌ Password is too long. Please enter your password again (or send /cancel):")
        try:
            await message.delete()
        except Exception:
            pass
        return

    user_data = await state.get_data()
    roll = user_data.get("roll")

    status_msg = await message.answer("⏳ Verifying credentials with NITRIS portal...")

    try:
        await message.delete()
    except Exception:
        pass

    await state.set_state(Registration.verifying)

    try:
        # Route registration verification through the NITRIS gateway.
        from app.nitris.gateway import nitris_gateway, NitrisCircuitOpenError
        from app.nitris.client import NitrisClient as _NitrisClient

        async with nitris_gateway.acquire():
            _reg_client = _NitrisClient()
            try:
                # Explicit verification path — no user_id, bypasses the quarantine
                # guard by design (this is the ONLY path that may login while
                # credentials are quarantined).
                await nitris_gateway.verify_credentials(_reg_client, roll, password)
                data = await get_attendance_data(roll, password, client=_reg_client)
            finally:
                await _reg_client.close()
    except NitrisCircuitOpenError as e:
        logger.warning("Registration blocked — NITRIS circuit open: %s", e)
        await state.set_state(Registration.waiting_for_password)
        await status_msg.edit_text(
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "The system is protecting the portal from overload. "
            "Please try registering again in ~60 seconds.",
            parse_mode=ParseMode.HTML
        )
        return
    except LoginError as e:
        logger.error("Login verification failed for %s: %s", roll, e)
        await state.set_state(Registration.waiting_for_password)
        await status_msg.edit_text(
            f"❌ <b>Login failed: Invalid credentials.</b>\n\n"
            f"Please enter your NITRIS password again (or send /cancel to abort):",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        logger.error("Portal error during verification for %s: %r", roll, e)
        await state.set_state(Registration.waiting_for_password)
        await status_msg.edit_text(
            f"❌ <b>Portal connection issue.</b>\n\n"
            f"Could not reach or parse the NITRIS portal: {html.escape(str(e))}\n\n"
            f"Please check portal availability and enter your password again, or send /cancel to abort:",
            parse_mode=ParseMode.HTML
        )
        return

    is_new_user = False

    try:
        async with get_db_session() as session:
            async with session.begin():
                user_repo = UserRepository(session)
                existing_user = await user_repo.get_by_telegram_id(telegram_id)
                if existing_user:
                    await user_repo.update_credentials(existing_user.id, roll, password)
                    user_id = existing_user.id
                    is_new_user = False
                else:
                    new_user = await user_repo.create_user(telegram_id, roll, password)
                    user_id = new_user.id
                    is_new_user = True

                snapshot_service = SnapshotService(session)
                await snapshot_service.create_snapshot_if_changed(
                    user_id=user_id,
                    module_name="attendance",
                    attendance_result=data
                )

                stmt = select(SyncState).where(SyncState.user_id == user_id)
                res = await session.execute(stmt)
                sync_state = res.scalar_one_or_none()
                if not sync_state:
                    sync_state = SyncState(user_id=user_id, failure_count=0)
                    session.add(sync_state)
                sync_state.last_sync = datetime.now(timezone.utc)
                sync_state.last_success = datetime.now(timezone.utc)
                sync_state.last_error = None
                sync_state.failure_count = 0

            # Create module_sync_schedule rows for the user
            from app.services.scheduler_service import ensure_schedule_exists
            from app.db.database import async_session_factory
            for module_name in ("attendance", "inbox"):
                await ensure_schedule_exists(async_session_factory, user_id, module_name)

            # Re-enable logins after a successful explicit verification.
            # Bumps credentials_version, clears any prior quarantine, and resets
            # the failure counters + gateway in-memory guard.
            from app.nitris.auth_gate import on_credentials_updated
            await on_credentials_updated(user_id)

            # Kick off a SILENT baseline sync (inbox + timetable) on a single
            # background login, so the user's first tap on inbox/timetable is
            # instant and their historical inbox doesn't spam "new message"
            # notifications. Fire-and-forget — the dashboard shows immediately.
            from app.nitris.job_queue import nitris_job_queue, Priority
            await nitris_job_queue.enqueue(
                job_type="sync_onboarding",
                user_id=user_id,
                priority=Priority.HIGH,
                dedup_key=f"onboarding:user:{user_id}",
                payload={},
            )

        await status_msg.edit_text(
            "✅ <b>Registration complete!</b>\n\n"
            "Initial attendance fetched successfully. Rendering your dashboard...",
            parse_mode=ParseMode.HTML
        )

        async with get_db_session() as session:
            stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()

            unread_count = 0
            if user:
                from app.db.repositories.inbox_repository import InboxRepository
                inbox_repo = InboxRepository(session)
                unread_count = await inbox_repo.get_unread_count(user.id)

        if user:
            text = format_dashboard_text(user, unread_count)
            await message.answer(text, reply_markup=get_dashboard_keyboard(unread_count), parse_mode=ParseMode.HTML)

        await state.clear()

    except Exception as e:
        logger.error("Failed to complete database updates during registration: %r", e)
        await status_msg.edit_text("❌ A database error occurred during registration. Please use /start to retry.")
        await state.clear()

    if is_new_user and roll:
        try:
            from app.bot.handlers.admin_notify import notify_admins_of_new_user
            await notify_admins_of_new_user(message.bot, roll)
        except Exception as notify_err:
            logger.warning(
                "Admin new-user notification failed (registration succeeded for roll=%s): %r",
                roll, notify_err,
            )


@router.message(Deregistration.waiting_for_confirm)
async def process_delete_confirm(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    input_text = message.text.strip()

    if input_text == "DELETE":
        try:
            async with get_db_session() as session:
                async with session.begin():
                    user_repo = UserRepository(session)
                    user = await user_repo.get_by_telegram_id(telegram_id)
                    if user:
                        await user_repo.delete_user(user.id)

            await state.clear()
            await message.answer("✅ <b>Account successfully deregistered.</b> All your records have been purged from our databases.", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Failed to delete user %d: %r", telegram_id, e)
            await message.answer("❌ A database error occurred during deregistration. Please try again.")
            await state.clear()
    else:
        await state.clear()
        await message.answer("🚫 <b>Deregistration cancelled.</b> Returning to your Dashboard.", parse_mode=ParseMode.HTML)

        async with get_db_session() as session:
            stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()

            unread_count = 0
            if user:
                from app.db.repositories.inbox_repository import InboxRepository
                inbox_repo = InboxRepository(session)
                unread_count = await inbox_repo.get_unread_count(user.id)

        if user:
            text = format_dashboard_text(user, unread_count)
            await message.answer(text, reply_markup=get_dashboard_keyboard(unread_count), parse_mode=ParseMode.HTML)
        else:
            await message.answer("⚠️ You are not registered. Please use /start to register.")


# --- Dashboard Callbacks ---

@router.callback_query(F.data.in_({"db_attendance", "db_update", "db_deregister", "db_papers"}))
async def handle_dashboard_callbacks(callback: types.CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id

    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

    if not user:
        try:
            await callback.answer("⚠️ You are not registered. Use /start to register.", show_alert=True)
        except Exception as e:
            logger.warning("Failed to answer unregistered callback: %r", e)
        return

    if callback.data == "db_attendance":
        try:
            await callback.answer("⏳ Requesting attendance...")
        except Exception as e:
            logger.warning("Failed to answer db_attendance callback: %r", e)
        await fetch_attendance_for_callback(callback, user)
    elif callback.data == "db_update":
        try:
            await callback.answer()
        except Exception as e:
            logger.warning("Failed to answer db_update callback: %r", e)
        await start_credential_update_from_cb(callback.message, state)
    elif callback.data == "db_deregister":
        try:
            await callback.answer()
        except Exception as e:
            logger.warning("Failed to answer db_deregister callback: %r", e)
        await start_deregistration_flow(callback.message, state)
    elif callback.data == "db_papers":
        try:
            await callback.answer()
        except Exception as e:
            logger.warning("Failed to answer db_papers callback: %r", e)
        await cmd_papers(callback.message, state, explicit_telegram_id=telegram_id)


@router.callback_query(F.data == "cancel_deregister")
async def handle_cancel_deregister(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()

    try:
        await callback.answer()
    except Exception as e:
        logger.warning("Failed to answer cancel_deregister callback: %r", e)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer("🚫 <b>Deregistration cancelled.</b> Returning to your Dashboard.", parse_mode=ParseMode.HTML)

    telegram_id = callback.from_user.id
    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

        unread_count = 0
        if user:
            from app.db.repositories.inbox_repository import InboxRepository
            inbox_repo = InboxRepository(session)
            unread_count = await inbox_repo.get_unread_count(user.id)

    if user:
        text = format_dashboard_text(user, unread_count)
        await callback.message.answer(text, reply_markup=get_dashboard_keyboard(unread_count), parse_mode=ParseMode.HTML)
    else:
        await callback.message.answer("⚠️ You are not registered. Please use /start to register.")


@router.callback_query(F.data == "confirm_deregister")
async def handle_confirm_deregister(callback: types.CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    await state.clear()

    try:
        await callback.answer("⏳ Processing deregistration...")
    except Exception as e:
        logger.warning("Failed to answer confirm_deregister callback: %r", e)

    try:
        async with get_db_session() as session:
            async with session.begin():
                user_repo = UserRepository(session)
                user = await user_repo.get_by_telegram_id(telegram_id)
                if user:
                    await user_repo.delete_user(user.id)

        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer("✅ <b>Account successfully deregistered.</b> All your records have been purged from our databases.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("Failed to delete user %d from callback: %r", telegram_id, e)
        await callback.message.answer("❌ A database error occurred during deregistration. Please try again.")


async def start_credential_update_from_cb(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(Registration.waiting_for_roll)
    await message.answer(
        "🔄 <b>Credential Update / Registration</b>\n\n"
        "Please enter your NITRIS Roll Number (e.g. <b>125AI0003</b>):",
        parse_mode=ParseMode.HTML
    )


async def start_deregistration_flow(message: types.Message, state: FSMContext):
    await state.set_state(Deregistration.waiting_for_confirm)

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🗑️ Permanently Delete", callback_data="confirm_deregister"))
    builder.row(types.InlineKeyboardButton(text="🚫 Cancel & Return", callback_data="cancel_deregister"))

    msg = (
        "⚠️ <b>WARNING: DESTRUCTIVE ACTION</b>\n\n"
        "Are you absolutely sure you want to deregister from NitrClaw?\n\n"
        "This action will permanently delete:\n"
        "• Your saved credentials\n"
        "• All academic attendance snapshots\n"
        "• All change event logs\n\n"
        "<b>To confirm, please click the button below, or type and send the word DELETE in the chat:</b>"
    )
    await message.answer(msg, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@router.message(Command("help"), StateFilter(None))
async def cmd_help(message: types.Message):
    msg = (
        "🤖 <b>NitrClaw Help Menu</b>\n\n"
        "Here are the available commands:\n"
        "• /start - View dashboard or start registration\n"
        "• /register - Register / update your credentials\n"
        "• /forgot - Shortcut to update your credentials\n"
        "• /attendance - Fetch current attendance statistics\n"
        "• /papers - Access previous year question papers\n"
        "• /inbox &lt;query&gt; - View inbox or search matching notices\n"
        "• /latest - Jump straight to the most recent notice\n"
        "• /cancel - Cancel the active process\n"
        "• /help - Display this help menu"
    )
    await message.answer(msg, parse_mode=ParseMode.HTML)
