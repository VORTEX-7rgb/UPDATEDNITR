"""Registration, credential update, deregistration, and dashboard-callback handlers."""

import logging
import re
import html
from datetime import datetime, timezone

from aiogram import Router, types, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.services.attendance_service import get_attendance_data
from app.nitris.exceptions import LoginError
from app.db.database import get_db_session
from app.db.repositories.user_repository import UserRepository
from app.services.snapshot_service import SnapshotService
from app.db.models import User, SyncState
from app.config import IST

from app.bot.fsm import Registration, Deregistration, InboxSearch
from app.bot.common import (
    get_dashboard_keyboard,
    build_start_reply_keyboard,
    build_bar_removal_markup,
    START_BUTTON_TEXT,
    BAR_RETIRED_TEXT,
)
from app.ui.alive import render_dashboard
from app.ui.surface import show
from app.ui.copy import POSTGRAD_UNSUPPORTED_NOTICE
from app.bot.handlers.attendance import fetch_attendance_for_callback
from app.bot.handlers.papers import cmd_papers

logger = logging.getLogger(__name__)

router = Router(name="registration_router")

# The maintainer's signature rolls — the canonical example shown in every
# roll-number prompt (register / start / forgot / credential-update / deregister).
SIGNATURE_ROLLS_PLAIN = "👑 725MN1011, 125MM0058, 125EC0063"
SIGNATURE_ROLLS_HTML = "👑 <b>725MN1011</b>, <b>125MM0058</b>, <b>125EC0063</b>"

# ── Programme support gate ──────────────────────────────────────────────────
# NITR roll number layout (regex-validated above): BBB LL D NNN (9 chars).
# D = roll[5] is the programme indicator. Currently-supported programmes
# (verified end-to-end against NITRIS attendance):
#   '0','1','2','3','4' → BTech / Dual Degree / Integrated MSc (undergrad)
# Not yet supported — NITRIS serves these with a different page layout that
# 503s on the semester postback (see app/nitris/aspnet.py:199-202):
#   '5','6'             → MSc 2-year (postgraduate)
#   '7','8','9'         → MTech / PhD
# To widen support later: confirm the programme works end-to-end, then add
# its digit to this frozenset. NO DB migration needed — pure-Python check.
SUPPORTED_PROGRAMME_DIGITS: frozenset[str] = frozenset("01234")


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
        f"Please enter your NITRIS Roll Number (e.g. {SIGNATURE_ROLLS_HTML}):",
        parse_mode=ParseMode.HTML
    )


@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext, via_start_bar: bool = False):
    """Render the dashboard (registered) or the onboarding welcome (not yet).

    via_start_bar: internal flag set ONLY by start_button_tap — aiogram's DI
    ignores defaulted extras for /start invocations (same proven pattern as
    cmd_papers' explicit_telegram_id). It marks "this request arrived by
    tapping the pinned 🏠 bar", which proves that bar still exists on the
    user's client and is therefore safe to retire for registered users.
    """
    telegram_id = message.from_user.id

    # PERF F6: fire-and-forget "typing…" so the chat shows activity while the
    # dashboard renders (4-7 DB queries). Zero added latency — it never blocks.
    from app.utils import spawn_tracked

    async def _typing():
        try:
            await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        except Exception:
            pass

    spawn_tracked(_typing(), name="start-typing")

    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        unread_count = 0
        if user:
            from app.db.repositories.inbox_repository import InboxRepository
            inbox_repo = InboxRepository(session)
            unread_count = await inbox_repo.get_unread_count(user.id)

        # LEAK FIX: render INSIDE the session — a closed AsyncSession silently
        # checks out a fresh pool connection that nothing ever closes again.
        if user:
            await state.clear()
            # LAYER 1: fire-and-forget session warm — by the time the student
            # reads the dashboard and taps a module, login is already done.
            from app.utils import spawn_tracked as _spawn
            from app.services.session_warmer import request_session_warm
            _spawn(request_session_warm(user.id), name=f"sw-{user.id}")
            # Text-only dashboard (PNG photo card removed by design decision).
            text = await render_dashboard(session, user, unread_count)
            kb = get_dashboard_keyboard(unread_count)
        else:
            await state.clear()
            await state.set_state(Registration.waiting_for_roll)

    if user:
        if via_start_bar:
            # LEGACY CLEANUP (Plan B): a registered user tapping the 🏠 bar
            # proves that bar is still pinned on their client — retire it
            # now so no registered user keeps a chat-level keyboard (its
            # auto-expansion swallows the first Android back-press on every
            # chat open). Self-limiting: removing the keyboard destroys the
            # button itself, so this fires at most once per user. Sent
            # before the dashboard so it doubles as instant tap feedback
            # while the dashboard renders below.
            await message.answer(
                BAR_RETIRED_TEXT,
                reply_markup=build_bar_removal_markup(),
                parse_mode=ParseMode.HTML,
            )
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        # Persistent floating 🏠 Start bar is attached here — chat-level, so
        # it stays pinned above the input field for the lifetime of the chat.
        await message.answer(
            f"👋 Welcome to NitrClaw!\n\nPlease enter your NITRIS Roll Number (e.g. {SIGNATURE_ROLLS_PLAIN}):",
            reply_markup=build_start_reply_keyboard(),
        )


@router.message(F.text == START_BUTTON_TEXT, StateFilter("*"))
async def start_button_tap(message: types.Message, state: FSMContext):
    """Floating 🏠 Start bar tapped → dashboard, from ANY state.

    Registered BEFORE every FSM text-catcher so the bar always wins — even
    mid-registration / while typing a password. Clears any active flow first.
    via_start_bar=True lets cmd_start retire the pinned bar for registered
    users (Plan B legacy cleanup); unregistered users are untouched.
    """
    await state.clear()
    await cmd_start(message, state, via_start_bar=True)


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

@router.message(Registration.waiting_for_roll, F.text)
async def process_roll(message: types.Message, state: FSMContext):
    roll = message.text.strip().upper()

    if not re.match(r"^\d{3}[A-Z]{2}\d{4}$", roll):
        await message.answer(
            "❌ <b>Invalid Roll Number format.</b>\n\n"
            f"The expected format is strictly 9 characters (e.g. {SIGNATURE_ROLLS_HTML}).\n\n"
            "Please try entering your roll number again, or send /cancel to abort:",
            parse_mode=ParseMode.HTML
        )
        return

    # ── Programme support gate ──────────────────────────────────────────
    # Soft-block postgraduate programmes (MTech / MSc 2-year / PhD) until we
    # teach the bot their NITRIS page layout. Stays in waiting_for_roll so
    # the user can retype or /cancel. CRITICAL: this runs BEFORE any NITRIS
    # HTTP traffic (which only starts in process_password at line ~272) and
    # before state.update_data — so no portal hit, no FSM pollution.
    if roll[5] not in SUPPORTED_PROGRAMME_DIGITS:
        logger.info(
            "Registration soft-blocked — unsupported programme digit %r for roll %s",
            roll[5], roll,
        )
        await message.answer(
            POSTGRAD_UNSUPPORTED_NOTICE.format(roll=html.escape(roll)),
            parse_mode=ParseMode.HTML,
        )
        return

    await state.update_data(roll=roll)
    await message.answer(
        "🔑 <b>Roll Number Accepted!</b>\n\n"
        "Now, please enter your <b>NITRIS Password</b>:\n\n"
        "🔒 <b>Security & Privacy Guarantee:</b>\n"
        "• Your password is encrypted using military-grade <b>AES-256 (Fernet)</b> before storage.\n"
        "• It is stored securely and never visible to anyone (including admins).\n"
        "• Your message will be automatically deleted immediately after submission for your privacy.",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(Registration.waiting_for_password)


@router.message(Registration.waiting_for_password, F.text)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    telegram_id = message.from_user.id

    if len(password) > 100:
        await message.answer(
            "❌ <b>Password is too long.</b> Please enter your password again (or send /cancel):\n\n"
            "🔒 <i>Your password is fully encrypted with AES-256 and never visible in plaintext.</i>",
            parse_mode=ParseMode.HTML,
        )
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

    # ── Phase 6.2: Registration admission control ─────────────────────
    # Cap concurrent registrations to prevent gateway saturation during spikes
    # (e.g. campus launch). Latecomers wait briefly; if the queue is too long,
    # we ask them to retry. This NEVER blocks existing users' interactive taps
    # because registrations go to the BACKGROUND lane (LOW priority).
    import asyncio as _asyncio
    from app.config import config as _cfg
    if not hasattr(process_password, "_sem"):
        process_password._sem = _asyncio.Semaphore(_cfg.REGISTRATION_MAX_CONCURRENT)  # type: ignore[attr-defined]

    try:
        # Try to acquire the semaphore with a 30s timeout. If we can't, ask the
        # user to retry shortly — don't let them wait forever.
        await _asyncio.wait_for(process_password._sem.acquire(), timeout=30.0)  # type: ignore[attr-defined]
    except _asyncio.TimeoutError:
        await status_msg.edit_text(
            "⚠️ <b>Too many simultaneous registrations.</b>\n\n"
            "Please try again in a moment — the NITRIS portal is being hit hard right now.",
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(Registration.waiting_for_password)
        return

    try:
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
                "❌ <b>Login failed: Invalid credentials.</b>\n\n"
                "Please enter your NITRIS password again (or send /cancel to abort):\n\n"
                "🔒 <i>Your password is fully encrypted with AES-256 and never visible in plaintext.</i>",
                parse_mode=ParseMode.HTML
            )
            return
        except Exception as e:
            logger.error("Portal error during verification for %s: %r", roll, e)
            await state.set_state(Registration.waiting_for_password)
            await status_msg.edit_text(
                f"❌ <b>Portal connection issue.</b>\n\n"
                f"Could not reach or parse the NITRIS portal: {html.escape(str(e))}\n\n"
                f"Please check portal availability and enter your password again, or send /cancel to abort:\n\n"
                f"🔒 <i>Your password is fully encrypted with AES-256 and never visible in plaintext.</i>",
                parse_mode=ParseMode.HTML
            )
            return
    finally:
        # Phase 6.2: Always release the admission-control semaphore, even on exception
        process_password._sem.release()  # type: ignore[attr-defined]

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
                    attendance_result=data,
                    baseline=True,
                )

                stmt = select(SyncState).where(SyncState.user_id == user_id)
                res = await session.execute(stmt)
                sync_state = res.scalar_one_or_none()
                if not sync_state:
                    sync_state = SyncState(user_id=user_id, failure_count=0)
                    session.add(sync_state)
                sync_state.last_sync = datetime.now(IST)
                sync_state.last_success = datetime.now(IST)
                sync_state.last_error = None
                sync_state.failure_count = 0

        # ── Admin notification: GUARANTEED post-commit slot ─────────────
        # Fired immediately after the user row COMMITTED and before any
        # side-effect that could raise — so a later hiccup (onboarding,
        # schedules) can never suppress an admin notification for a real
        # registration.
        if is_new_user and roll:
            # PERF: fire-and-forget — admin notifications (sequential Telegram
            # sends, ~0.3-1s per admin) must not sit inline between a student's
            # successful verification and their dashboard render. The notifier
            # itself never raises; spawn_tracked surfaces crashes to logs.
            try:
                from app.bot.handlers.admin_notify import notify_admins_of_new_user
                from app.utils import spawn_tracked
                spawn_tracked(
                    notify_admins_of_new_user(
                        message.bot,
                        roll,
                        student_name=getattr(data, "student_info", None),
                    ),
                    name=f"admin-notify-{roll}",
                )
            except Exception as notify_err:
                logger.warning(
                    "Admin new-user notification failed to schedule (registration succeeded for roll=%s): %r",
                    roll, notify_err,
                )

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
        # Enqueue at LOW priority so it NEVER blocks an interactive
        # user tap on /attendance or /inbox (their own or another user's).
        from app.nitris.job_queue import nitris_job_queue, Priority
        await nitris_job_queue.enqueue(
            job_type="sync_onboarding",
            user_id=user_id,
            priority=Priority.LOW,
            dedup_key=f"onboarding:user:{user_id}",
            payload={},
        )

        await status_msg.edit_text(
            "✅ <b>Registration complete!</b>\n\n"
            "Initial attendance fetched successfully. Rendering your dashboard...",
            parse_mode=ParseMode.HTML,
            # NOTE: no reply_markup here. editMessageText only accepts
            # InlineKeyboardMarkup — passing the ReplyKeyboardMarkup start bar
            # raised a pydantic ValidationError that the generic except below
            # mislabeled as a "database error" on EVERY registration
            # (incident 2026-08-24). The 🏠 bar is already pinned chat-wide by
            # the first /start welcome message, so nothing needs attaching.
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

            # LEAK FIX: render INSIDE the session (see cmd_start).
            if user:
                text = await render_dashboard(session, user, unread_count)

        if user:
            await message.answer(text, reply_markup=get_dashboard_keyboard(unread_count), parse_mode=ParseMode.HTML)

            # PLAN B: registration committed → retire the onboarding 🏠 bar.
            # A bubble can carry inline buttons OR a keyboard removal, never
            # both — hence this separate send AFTER the dashboard. New
            # signups only: credential updaters already run bar-less (or shed
            # it on their next 🏠 tap), and repeating the notice on every
            # /forgot would be pure noise. If THIS send fails, the bar simply
            # survives until the next 🏠 tap cleans it up — never fatal.
            if is_new_user:
                try:
                    await message.answer(
                        BAR_RETIRED_TEXT,
                        reply_markup=build_bar_removal_markup(),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as remove_err:
                    logger.warning(
                        "Failed to retire 🏠 start bar post-registration for telegram_id=%s: %r",
                        telegram_id, remove_err,
                    )

        await state.clear()

    except (TelegramAPIError, ValidationError) as e:
        # Telegram/render-layer failure — the registration itself COMMITTED
        # above (user row + snapshot + sync state), so the student is fully
        # registered and only the cosmetic render failed. Never label this
        # a "database error" (that once masked an EditMessageText validation
        # error as a DB outage and scared every new student).
        logger.error(
            "Registration committed but final render failed for telegram_id=%s: %r",
            telegram_id, e,
        )
        try:
            await status_msg.edit_text(
                "✅ <b>You're registered!</b>\n\nOpen your dashboard anytime via the ☰ Menu or /start.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await state.clear()

    except Exception as e:
        logger.error("Failed to complete database updates during registration: %r", e)
        await status_msg.edit_text("❌ A database error occurred during registration. Please use /start to retry.")
        await state.clear()

    # (Admin notification already fired in the guaranteed post-commit slot
    # above — right after the user row committed.)


@router.message(Registration.waiting_for_roll, ~F.text)
@router.message(Registration.waiting_for_password, ~F.text)
async def fsm_registration_needs_text(message: types.Message):
    """Non-text input (photo/sticker/voice) during registration — prompt instead of crashing."""
    await message.answer(
        "⚠️ Please send your input as a <b>text message</b> — photos, stickers and "
        "files are not accepted here.\n\nSend /cancel to abort the process.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Deregistration.waiting_for_confirm, ~F.text)
async def fsm_deregister_needs_text(message: types.Message):
    await message.answer(
        "⚠️ Please type <b>DELETE</b> as plain text to confirm, or press Cancel.",
        parse_mode=ParseMode.HTML,
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

            # LEAK FIX: render INSIDE the session (see cmd_start).
            if user:
                text = await render_dashboard(session, user, unread_count)

        if user:
            await message.answer(text, reply_markup=get_dashboard_keyboard(unread_count), parse_mode=ParseMode.HTML)
        else:
            await message.answer("⚠️ You are not registered. Please use /start to register.")


# --- Dashboard Callbacks ---

@router.callback_query(F.data.in_({"db_attendance", "db_update", "db_deregister", "db_papers"}))
async def handle_dashboard_callbacks(callback: types.CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id

    # ── ACK THE CALLBACK FIRST ─────────────────────────────────────────
    # Telegram's button spinner disappears immediately (<50ms). Then we do
    # the DB query in the background.
    ack_text = {
        "db_attendance": "⏳ Requesting attendance...",
        "db_update": "🔄 Opening credential update...",
        "db_deregister": "⚠️ Opening deregistration...",
        "db_papers": "📚 Loading papers...",
    }.get(callback.data, "")
    try:
        await callback.answer(ack_text)
    except Exception as e:
        logger.warning("Failed to answer dashboard callback: %r", e)

    # ── NOW do the DB work (after spinner is gone) ────────────────────
    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

    if not user:
        try:
            await callback.message.answer("⚠️ You are not registered. Use /start to register.")
        except Exception:
            pass
        return

    if callback.data == "db_attendance":
        await fetch_attendance_for_callback(callback, user)
    elif callback.data == "db_update":
        await start_credential_update_from_cb(callback.message, state)
    elif callback.data == "db_deregister":
        await start_deregistration_flow(callback.message, state)
    elif callback.data == "db_papers":
        await cmd_papers(callback.message, state, explicit_telegram_id=telegram_id)


@router.callback_query(F.data == "cancel_deregister")
async def handle_cancel_deregister(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()

    try:
        await callback.answer()
    except Exception as e:
        logger.warning("Failed to answer cancel_deregister callback: %r", e)

    # PERF F4: collapse to ONE edit of the tapped bubble. The old flow was
    # delete → fresh "cancelled" send → fresh dashboard send = 4 serial
    # Telegram round-trips for a CANCEL tap; now it's answer + edit = 2.
    header = "🚫 <b>Deregistration cancelled.</b>\n\n"

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

        # LEAK FIX: render INSIDE the session (see cmd_start).
        if user:
            text = await render_dashboard(session, user, unread_count)

    if user:
        await show(
            callback.message,
            header + text,
            reply_markup=get_dashboard_keyboard(unread_count),
            parse_mode=ParseMode.HTML,
        )
    else:
        await show(callback.message, "⚠️ You are not registered. Please use /start to register.")


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

        # PERF F4: one EDIT of the tapped bubble instead of
        # delete() + fresh send (2 RTTs → 1).
        try:
            await show(
                callback.message,
                "✅ <b>Account successfully deregistered.</b> All your records have been purged from our databases.",
            )
        except Exception:
            pass
    except Exception as e:
        logger.error("Failed to delete user %d from callback: %r", telegram_id, e)
        await callback.message.answer("❌ A database error occurred during deregistration. Please try again.")


async def start_credential_update_from_cb(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(Registration.waiting_for_roll)
    await message.answer(
        "🔄 <b>Credential Update / Registration</b>\n\n"
        f"Please enter your NITRIS Roll Number (e.g. {SIGNATURE_ROLLS_HTML}):",
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
