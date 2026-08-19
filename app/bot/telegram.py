import logging
import re
import asyncio
import html
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import ErrorEvent
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.services.attendance_service import get_attendance_data
from app.nitris.exceptions import LoginError, AttendanceParseError, SessionExpiredError, NitrisError
from app.db.database import get_db_session, is_db_connection_error
from app.db.repositories.user_repository import UserRepository
from app.services.snapshot_service import SnapshotService
from app.db.crypto import decrypt_password, encrypt_password
from app.db.models import User, SyncState, InboxMessage
from app.services.lock_service import user_lock

logger = logging.getLogger(__name__)


from app.utils import esc, safe_truncate


class Registration(StatesGroup):
    waiting_for_roll = State()      # Waiting for 9-char NITRIS Roll Number
    waiting_for_password = State()  # Waiting for NITRIS Password
    verifying = State()             # Currently verifying with NITRIS


class Deregistration(StatesGroup):
    waiting_for_confirm = State()   # Waiting for the user to type DELETE


class InboxSearch(StatesGroup):
    waiting_for_query = State()     # Waiting for user to send search text


class QuestionPaperFlow(StatesGroup):
    waiting_for_subject = State()       # Selecting subject from lists
    waiting_for_search_query = State()  # Waiting for search query
    waiting_for_year = State()          # Waiting for year selection


from app.services.qpaper_service import QPaperService, QPResult

dp = Dispatcher()

from app.bot.handlers.timetable import router as timetable_router
dp.include_router(timetable_router)

qpaper_service: Optional[QPaperService] = None

async def init_qpaper_service(bot: Bot) -> None:
    """Initialize the singleton QPaperService on startup."""
    global qpaper_service
    from app.db.database import async_session_factory
    from app.db.crypto import decrypt_password
    from app.db.models import User, SyncState
    from sqlalchemy import select, or_, func

    async def creds_provider():
        """Return a list of (roll, password, user_id) candidates for QP acquisition."""
        async with async_session_factory() as s:
            stmt = (
                select(User.id, User.roll_number, User.encrypted_password)
                .outerjoin(SyncState, User.id == SyncState.user_id)
                .where(User.credentials_valid == True)
                .where(
                    or_(
                        User.qp_cooldown_until.is_(None),
                        User.qp_cooldown_until < func.now()
                    )
                )
                .order_by(SyncState.last_success.desc().nulls_last(), User.id.desc())
                .limit(5)
            )
            rows = (await s.execute(stmt)).all()
            
            if not rows:
                logger.warning(
                    "No healthy QP credential candidates with sync history — "
                    "falling back to any user with credentials_valid=TRUE"
                )
                stmt = (
                    select(User.id, User.roll_number, User.encrypted_password)
                    .where(User.credentials_valid == True)
                    .order_by(User.id.desc())
                    .limit(5)
                )
                rows = (await s.execute(stmt)).all()
            
            if not rows:
                logger.warning(
                    "No users with credentials_valid=TRUE — falling back to "
                    "any registered user as last resort"
                )
                stmt = (
                    select(User.id, User.roll_number, User.encrypted_password)
                    .order_by(User.id.desc())
                    .limit(5)
                )
                rows = (await s.execute(stmt)).all()
            
            if not rows:
                raise RuntimeError(
                    "No registered users — cannot acquire QP. "
                    "Register at least one student before downloading papers."
                )
            
            # Return (roll, user_id, encrypted_password) — NOT decrypted plaintext.
            # The caller (_nitris_download) decrypts one at a time inside acquire().
            candidates = [(r.roll_number, r.id, r.encrypted_password) for r in rows]
            
            logger.info(
                "creds_provider: returning %d candidate(s) for QP acquisition "
                "(passwords NOT decrypted — will be decrypted one-at-a-time inside gateway)",
                len(candidates),
            )
            return candidates

    qpaper_service = QPaperService(
        bot=bot,
        session_factory=async_session_factory,
        creds_provider=creds_provider,
    )
    qpaper_service.start_reaper()

async def shutdown_qpaper_service() -> None:
    """Cleanly stop the QPaperService reaper on shutdown."""
    global qpaper_service
    if qpaper_service is not None:
        await qpaper_service.stop_reaper()


@dp.errors()
async def db_error_handler(event: ErrorEvent):
    """Global error handler for database connection/offline issues."""
    exception = event.exception
    if is_db_connection_error(exception):
        logger.error("Database offline error intercepted: %r", exception)
        update = event.update
        try:
            if update.message:
                await update.message.answer(
                    "⚠️ <b>System Offline</b>\n\n"
                    "The database is currently undergoing brief maintenance or is temporarily unreachable. "
                    "We are already reconnecting! Please try again in a few moments.",
                    parse_mode=ParseMode.HTML
                )
            elif update.callback_query:
                try:
                    await update.callback_query.answer(
                        "⚠️ Database is currently offline. Reconnecting...",
                        show_alert=True
                    )
                except Exception:
                    await update.callback_query.message.answer(
                        "⚠️ <b>System Offline</b>\n\n"
                        "The database is currently undergoing brief maintenance. Please try again shortly.",
                        parse_mode=ParseMode.HTML
                    )
        except Exception as send_err:
            logger.error("Failed to notify user of DB offline status: %r", send_err)
        return True
    return False


def format_attendance_message(data) -> str:
    d = data.to_dict()
    msg = f"🧑🎓 <b>{esc(d['student_info'])}</b>\n\n<b>📊 Attendance Summary:</b>\n"
    for rec in d["records"]:
        name = rec.get("subject_name") or rec.get("subject_code", "Unknown")
        msg += f"🔸 <b>{esc(name)}</b>\n"
        msg += f"   TC: {esc(rec['tc'])} | OA: {esc(rec['oa'])} | UA: {esc(rec['ua'])} | LE: {esc(rec['le'])}\n\n"
    return msg


def format_attendance_message_from_snapshot(snapshot) -> str:
    """Format attendance message from a cached Snapshot DB row.
    
    This is the cache-first path — we render the stored snapshot_json
    directly without touching NITRIS.
    """
    if not snapshot or not getattr(snapshot, "snapshot_json", None):
        return "<i>No cached attendance data available.</i>"
    
    data = snapshot.snapshot_json
    student_info = data.get("student_info", "Unknown Student")
    records = data.get("records", [])
    
    cached_time = ""
    if getattr(snapshot, "created_at", None):
        cached_time = f" <i>(cached {snapshot.created_at.strftime('%d %b %H:%M')})</i>"
    
    msg = f"🧑🎓 <b>{esc(student_info)}</b>{cached_time}\n\n<b>📊 Attendance Summary:</b>\n"
    for rec in records:
        name = rec.get("subject_name") or rec.get("subject_code", "Unknown")
        msg += f"🔸 <b>{esc(name)}</b>\n"
        msg += f"   TC: {esc(rec.get('tc', '0'))} | OA: {esc(rec.get('oa', '0'))} | UA: {esc(rec.get('ua', '0'))} | LE: {esc(rec.get('le', '0'))}\n\n"
    return msg


def format_dashboard_text(user: User, unread_count: int = 0) -> str:
    roll = user.roll_number
    sync_state = user.sync_state
    
    status_icon = "🔄"
    status_text = "Pending Sync"
    last_synced_str = "Never"
    
    if sync_state:
        if sync_state.failure_count == 0:
            status_icon = "✅"
            status_text = "Healthy"
        elif sync_state.failure_count < 5:
            status_icon = "⚠️"
            status_text = f"Sync Delay (failures: {esc(sync_state.failure_count)})"
            if sync_state.last_error:
                status_text += f"\n<i>Error: {esc(sync_state.last_error[:100])}</i>"
        else:
            status_icon = "❌"
            status_text = "Outage / Connection Error"
            if sync_state.last_error:
                status_text += f"\n<i>Error: {esc(sync_state.last_error[:100])}</i>"
                
        if sync_state.last_sync:
            last_synced_str = sync_state.last_sync.strftime("%d %b %H:%M")
            
    unread_label = f"🔴 {esc(unread_count)} New Messages" if unread_count > 0 else "0"
    msg = (
        f"👋 <b>Welcome back to NitrClaw!</b>\n\n"
        f"🧑🎓 <b>Student:</b> <code>{esc(roll)}</code>\n"
        f"📅 <b>Last Synced:</b> {last_synced_str}\n"
        f"📊 <b>Status:</b> {status_icon} {status_text}\n"
        f"📩 <b>Unread:</b> {unread_label}\n\n"
        f"Choose an action from the options below:"
    )
    return msg


def get_dashboard_keyboard(unread_count: int = 0) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Get Latest Attendance", callback_data="db_attendance"))
    builder.row(
        types.InlineKeyboardButton(text="📅 Timetable", callback_data="tt_view_full"),
        types.InlineKeyboardButton(text="⏰ Now & Next", callback_data="tt_now_next"),
    )
    inbox_text = f"📩 Inbox ({unread_count})" if unread_count > 0 else "📩 Inbox"
    builder.row(
        types.InlineKeyboardButton(text=inbox_text, callback_data="db_inbox"),
        types.InlineKeyboardButton(text="📚 Previous Papers", callback_data="db_papers")
    )
    builder.row(
        types.InlineKeyboardButton(text="🔄 Update Credentials", callback_data="db_update"),
        types.InlineKeyboardButton(text="❌ Deregister", callback_data="db_deregister")
    )
    return builder.as_markup()


# --- Global Command Overrides ---

@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("❌ No active process to cancel.")
        return
    await state.clear()
    await message.answer("❌ Process cancelled.")


@dp.message(Command("forgot"), StateFilter("*"))
@dp.message(Command("register"), StateFilter("*"))
async def cmd_forgot(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(Registration.waiting_for_roll)
    await message.answer(
        "🔄 <b>Credential Update / Registration</b>\n\n"
        "Please enter your NITRIS Roll Number (e.g. <b>125AI0003</b>):",
        parse_mode=ParseMode.HTML
    )


@dp.message(CommandStart(), StateFilter("*"))
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

@dp.message(Registration.waiting_for_roll, F.text.startswith("/"))
@dp.message(Registration.waiting_for_password, F.text.startswith("/"))
@dp.message(Registration.verifying, F.text.startswith("/"))
@dp.message(Deregistration.waiting_for_confirm, F.text.startswith("/"))
@dp.message(InboxSearch.waiting_for_query, F.text.startswith("/"))
async def registration_command_shield(message: types.Message):
    await message.answer(
        "⚠️ <b>Registration or active process is in progress.</b>\n\n"
        "Please complete the active steps, or send /cancel to abort the process before running other commands.",
        parse_mode=ParseMode.HTML
    )


@dp.message(Registration.verifying)
async def verification_shield(message: types.Message):
    await message.answer(
        "⏳ <b>Verification with NITRIS is currently in progress.</b>\n\n"
        "Please wait a few seconds for the current request to complete.",
        parse_mode=ParseMode.HTML
    )


# --- FSM State Input Handlers ---

@dp.message(Registration.waiting_for_roll)
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


@dp.message(Registration.waiting_for_password)
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
                await nitris_gateway.login_through_gateway(_reg_client, roll, password)
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

    is_new_user = False  # Track for admin notification (True ONLY in create_user branch)

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
            
            # Phase 5: Create module_sync_schedule rows for the user
            from app.services.scheduler_service import ensure_schedule_exists
            from app.db.database import async_session_factory
            for module_name in ("attendance", "inbox"):
                await ensure_schedule_exists(async_session_factory, user_id, module_name)
                
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

    # ── Admin notification (additive, fire-and-forget) ───────────────────
    # Fires ONLY when a brand-new user registers (not on /forgot or credential updates).
    # Wrapped in its own try/except so notification failure never affects user.
    if is_new_user and roll:
        try:
            from app.bot.handlers.admin_notify import notify_admins_of_new_user
            await notify_admins_of_new_user(message.bot, roll)
        except Exception as notify_err:
            logger.warning(
                "Admin new-user notification failed (registration succeeded for roll=%s): %r",
                roll, notify_err,
            )


@dp.message(Deregistration.waiting_for_confirm)
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

@dp.callback_query(F.data.in_({"db_attendance", "db_update", "db_deregister", "db_papers"}))
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


@dp.callback_query(F.data == "cancel_deregister")
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


@dp.callback_query(F.data == "confirm_deregister")
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


@dp.message(Command("attendance"), StateFilter(None))
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


@dp.message(Command("help"), StateFilter(None))
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


# --- NITRIS Inbox Handlers ---

async def render_single_message(event, user: User, msg: InboxMessage, session) -> None:
    """Helper to load notice body (with lazy fetching if needed) and render single notice detail card."""
    if not msg.is_read:
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        await inbox_repo.mark_as_read(msg.id)
        await session.commit()
        
    if msg.body is None:
        if isinstance(event, types.CallbackQuery):
            status_msg = await event.message.answer("⏳ Fetching notice body from NITRIS portal...")
        else:
            status_msg = await event.answer("⏳ Fetching notice body from NITRIS portal...")
        
        from app.nitris.job_queue import nitris_job_queue, Priority
        from app.nitris.gateway import NitrisCircuitOpenError
        
        try:
            future = await nitris_job_queue.enqueue(
                job_type="inbox_detail_fetch",
                user_id=user.id,
                priority=Priority.MEDIUM,
                payload={"message_id": msg.id},
            )
            
            try:
                result = await asyncio.wait_for(future, timeout=120.0)
                if not result.get("success"):
                    error = result.get("error", "Unknown error")
                    await status_msg.edit_text(
                        f"❌ Failed to fetch message detail from NITRIS: {html.escape(str(error)[:200])}"
                    )
                    return
                
                stmt = select(InboxMessage).where(InboxMessage.id == msg.id)
                res = await session.execute(stmt)
                msg = res.scalar_one_or_none()
                
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                
            except asyncio.TimeoutError:
                await status_msg.edit_text(
                    "⏳ <b>Fetch is taking longer than expected.</b>\n\n"
                    "NITRIS may be slow. Please try again in a moment.",
                    parse_mode=ParseMode.HTML,
                )
                return
        
        except NitrisCircuitOpenError:
            await status_msg.edit_text(
                "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
                "The system is protecting the portal from overload. "
                "Please try again in ~60 seconds.",
                parse_mode=ParseMode.HTML,
            )
            return

    sent_str = msg.sent_on.strftime("%d %b %Y")
    if msg.body is not None:
        body_esc = esc(msg.body)
        body_text = safe_truncate(body_esc, 3000)
        if len(body_esc) > 3000:
            body_text += "\n\n<i>[Content truncated due to Telegram size limits]</i>"
    else:
        body_text = "<i>(No content)</i>"
        
    card_text = (
        f"📩 <b>NITRIS Notice</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>From:</b> {esc(msg.sender)}\n"
        f"📅 <b>Date:</b> {sent_str}\n"
        f"📌 <b>Subject:</b> {esc(msg.subject)}\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"{body_text}\n"
    )
    
    builder = InlineKeyboardBuilder()
    if msg.attachment_url:
        builder.row(types.InlineKeyboardButton(text="📎 Download PDF Attachment", callback_data=f"dl_{msg.id}"))
        
    builder.row(
        types.InlineKeyboardButton(text="📬 Inbox Menu", callback_data="db_inbox"),
        types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard")
    )
    
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(
            card_text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
    else:
        await event.answer(
            card_text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )


@dp.callback_query(F.data == "db_inbox")
@dp.callback_query(F.data.startswith("inbox_page_"))
async def handle_inbox_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id
    
    page = 1
    if callback.data.startswith("inbox_page_"):
        try:
            page = int(callback.data.split("_")[-1])
        except ValueError:
            page = 1
            
    try:
        await callback.answer()
    except Exception:
        pass
        
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await callback.message.answer("⚠️ You are not registered. Use /start to register.")
            return
            
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        
        limit = 5
        offset = (page - 1) * limit
        messages = await inbox_repo.get_latest_messages(user.id, offset=offset, limit=limit + 1)
        
    if not messages:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔄 Refresh Now", callback_data="inbox_refresh"))
        builder.row(types.InlineKeyboardButton(text="🏠 Back to Dashboard", callback_data="inbox_back_dashboard"))
        
        await callback.message.edit_text(
            "📩 <b>Your NITRIS Inbox</b>\n\n"
            "Your inbox is currently empty. Run a sync or click Refresh below to retrieve messages from the portal.",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        return

    has_next = len(messages) > limit
    page_messages = messages[:limit]
    
    text = f"📩 <b>Your NITRIS Inbox</b> (Page {page})\n\n"
    
    builder = InlineKeyboardBuilder()
    select_buttons = []
    
    for idx, msg in enumerate(page_messages, start=1):
        status_icon = "🔴" if not msg.is_read else "⚪"
        sent_str = msg.sent_on.strftime("%d %b")
        sender_clean = msg.sender[:30] + "..." if len(msg.sender) > 30 else msg.sender
        subject_clean = msg.subject[:40] + "..." if len(msg.subject) > 40 else msg.subject
        
        text += (
            f"<b>{idx}.</b> {status_icon} <b>{sent_str}</b> | <i>{esc(sender_clean)}</i>\n"
            f"   <b>Subject:</b> {esc(subject_clean)}\n\n"
        )
        
        select_buttons.append(
            types.InlineKeyboardButton(text=str(idx), callback_data=f"msg_{msg.id}")
        )
        
    builder.row(*select_buttons)
    builder.row(types.InlineKeyboardButton(text="📬 Read Latest Message", callback_data="inbox_latest"))
    
    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="◀️ Prev", callback_data=f"inbox_page_{page - 1}"))
    if has_next:
        nav_buttons.append(types.InlineKeyboardButton(text="⏩ More", callback_data=f"inbox_page_{page + 1}"))
        
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(
        types.InlineKeyboardButton(text="🔄 Refresh Now", callback_data="inbox_refresh"),
        types.InlineKeyboardButton(text="🔍 Search Inbox", callback_data="inbox_search_prompt")
    )
    builder.row(
        types.InlineKeyboardButton(text="🏠 Back to Dashboard", callback_data="inbox_back_dashboard")
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data == "inbox_refresh")
async def handle_inbox_refresh(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Route inbox refresh through the gateway + job queue."""
    telegram_id = callback.from_user.id
    
    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_INBOX_REFRESH
    allowed, wait = await operation_cooldown.check(
        callback.from_user.id, "inbox_refresh", cooldown_seconds=COOLDOWN_INBOX_REFRESH
    )
    if not allowed:
        try:
            await callback.answer(f"⏳ Please wait {wait}s before refreshing again.", show_alert=True)
        except Exception:
            pass
        return
    
    try:
        await callback.answer("⏳ Connecting to NITRIS and refreshing inbox...", show_alert=False)
    except Exception:
        pass
        
    status_msg = await callback.message.answer("⏳ Logging into NITRIS and syncing your inbox...")
    
    try:
        async with get_db_session() as session:
            from app.db.repositories.user_repository import UserRepository
            user_repo = UserRepository(session)
            user = await user_repo.get_by_telegram_id(telegram_id)
            
        if not user:
            await status_msg.edit_text("❌ You are not registered. Use /start to register.")
            return
        
        from app.nitris.job_queue import nitris_job_queue, Priority
        from app.nitris.gateway import NitrisCircuitOpenError
        
        try:
            future = await nitris_job_queue.enqueue(
                job_type="inbox_refresh",
                user_id=user.id,
                priority=Priority.HIGH,
                payload={
                    "callback_chat_id": status_msg.chat.id,
                    "callback_message_id": status_msg.message_id,
                },
            )
            
            try:
                result = await asyncio.wait_for(future, timeout=120.0)
                if result.get("success"):
                    await status_msg.edit_text("✅ Inbox sync completed successfully!")
                    await asyncio.sleep(1)
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    
                    callback.data = "inbox_page_1"
                    await handle_inbox_list(callback, state)
                else:
                    error = result.get("error", "Unknown error")
                    await status_msg.edit_text(
                        f"❌ <b>Inbox sync failed.</b>\n\n"
                        f"<i>Error: {html.escape(str(error)[:200])}</i>",
                        parse_mode=ParseMode.HTML,
                    )
            except asyncio.TimeoutError:
                await status_msg.edit_text(
                    "⏳ <b>Inbox sync is taking longer than expected.</b>\n\n"
                    "NITRIS may be slow. Your inbox will update automatically when complete.",
                    parse_mode=ParseMode.HTML,
                )
        
        except NitrisCircuitOpenError:
            await status_msg.edit_text(
                "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
                "The system is protecting the portal from overload. "
                "Please try again in ~60 seconds.",
                parse_mode=ParseMode.HTML,
            )
        
    except Exception as e:
        logger.error("Failed live inbox refresh for telegram_id %s: %r", telegram_id, e)
        await status_msg.edit_text(f"❌ Refresh failed: {html.escape(str(e))}")


@dp.callback_query(F.data == "inbox_back_dashboard")
async def handle_inbox_back_dashboard(callback: types.CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id
    try:
        await callback.answer()
    except Exception:
        pass
        
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
        await callback.message.edit_text(
            text,
            reply_markup=get_dashboard_keyboard(unread_count),
            parse_mode=ParseMode.HTML
        )
    else:
        await callback.message.answer("⚠️ You are not registered. Please use /start to register.")


@dp.callback_query(F.data.startswith("msg_"))
async def handle_message_detail(callback: types.CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id
    msg_id = int(callback.data.split("_")[1])
    
    try:
        await callback.answer()
    except Exception:
        pass
        
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await callback.message.answer("⚠️ You are not registered. Use /start to register.")
            return
            
        stmt = select(InboxMessage).where(InboxMessage.id == msg_id, InboxMessage.user_id == user.id)
        res = await session.execute(stmt)
        msg = res.scalar_one_or_none()
        
        if not msg:
            await callback.message.answer("❌ Message not found.")
            return
            
        await render_single_message(callback, user, msg, session)


@dp.callback_query(F.data.startswith("dl_"))
async def handle_download_attachment(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Route attachment download through the gateway + job queue."""
    telegram_id = callback.from_user.id
    msg_id = int(callback.data.split("_")[1])
    
    try:
        await callback.answer("⏳ Processing attachment download...", show_alert=False)
    except Exception:
        pass
        
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await callback.message.answer("⚠️ You are not registered. Use /start to register.")
            return
            
        stmt = select(InboxMessage).where(InboxMessage.id == msg_id, InboxMessage.user_id == user.id)
        res = await session.execute(stmt)
        msg = res.scalar_one_or_none()
        
        if not msg or not msg.attachment_url:
            await callback.message.answer("❌ Attachment not found.")
            return
        
        if msg.user_id != user.id:
            await callback.message.answer("❌ Attachment not found.")
            logger.warning(
                "Cross-user access attempt: telegram_id=%d tried to access message_id=%d owned by user_id=%d",
                telegram_id, msg_id, msg.user_id,
            )
            return
            
        if msg.telegram_file_id:
            try:
                await callback.bot.send_document(chat_id=telegram_id, document=msg.telegram_file_id)
                return
            except Exception as e:
                logger.warning("Cached telegram_file_id failed for message ID %s: %r. Re-downloading...", msg.id, e)
        
        attachment_url = msg.attachment_url
    
    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTACHMENT_DOWNLOAD
    allowed, wait = await operation_cooldown.check(
        user.id, "attachment_download", key=str(msg_id),
        cooldown_seconds=COOLDOWN_ATTACHMENT_DOWNLOAD,
    )
    if not allowed:
        try:
            await callback.answer(f"⏳ Please wait {wait}s before retrying.", show_alert=True)
        except Exception:
            pass
        return
    
    status_msg = await callback.message.answer("⏳ Fetching attachment from NITRIS portal...")
    
    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError
    
    try:
        future = await nitris_job_queue.enqueue(
            job_type="attachment_download",
            user_id=user.id,
            priority=Priority.MEDIUM,
            payload={
                "message_id": msg_id,
                "callback_chat_id": telegram_id,
            },
        )
        
        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            if result.get("success"):
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            else:
                error = result.get("error", "Unknown error")
                if "too large" in error.lower():
                    from app.config import config
                    direct_url = f"{config.NITRIS_BASE_URL}{attachment_url}"
                    await status_msg.edit_text(
                        f"⚠️ <b>Attachment is too large (&gt;50MB) for Telegram upload.</b>\n\n"
                        f"You can download it directly from the secure portal link below:\n"
                        f"🔗 <a href='{direct_url}'>Direct Download Link</a>",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                else:
                    await status_msg.edit_text(
                        f"❌ Failed to download attachment: {html.escape(str(error)[:200])}",
                        parse_mode=ParseMode.HTML,
                    )
        except asyncio.TimeoutError:
            await status_msg.edit_text(
                "⏳ <b>Download is taking longer than expected.</b>\n\n"
                "NITRIS may be slow. Please try again in a moment.",
                parse_mode=ParseMode.HTML,
            )
    
    except NitrisCircuitOpenError:
        await status_msg.edit_text(
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "The system is protecting the portal from overload. "
            "Please try again in ~60 seconds.",
            parse_mode=ParseMode.HTML,
        )


@dp.callback_query(F.data == "inbox_search_prompt")
async def handle_search_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        pass
        
    await state.set_state(InboxSearch.waiting_for_query)
    await callback.message.answer(
        "🔍 <b>Search your NITRIS Inbox</b>\n\n"
        "Please type your search query (e.g., <i>chemistry</i>, <i>exam</i>, or a professor's name) and send it in the chat.\n\n"
        "<i>To abort, send /cancel.</i>",
        parse_mode=ParseMode.HTML
    )


@dp.message(InboxSearch.waiting_for_query)
async def process_search_query(message: types.Message, state: FSMContext) -> None:
    query = message.text.strip()
    telegram_id = message.from_user.id
    await state.clear()
    
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await message.answer("⚠️ You are not registered. Use /start to register.")
            return
            
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        results = await inbox_repo.search_messages(user.id, query, limit=5)
        
    await render_search_results(message, query, results)


@dp.message(Command("inbox"), StateFilter(None))
async def cmd_inbox(message: types.Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    args = message.text.strip().split(maxsplit=1)
    
    if len(args) < 2:
        async with get_db_session() as session:
            from app.db.repositories.user_repository import UserRepository
            user_repo = UserRepository(session)
            user = await user_repo.get_by_telegram_id(telegram_id)
            
            if not user:
                await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
                return
                
            from app.db.repositories.inbox_repository import InboxRepository
            inbox_repo = InboxRepository(session)
            messages = await inbox_repo.get_latest_messages(user.id, offset=0, limit=6)
            
        if not messages:
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(text="🔄 Refresh Now", callback_data="inbox_refresh"))
            builder.row(types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard"))
            await message.answer(
                "📩 <b>Your NITRIS Inbox</b>\n\n"
                "Your inbox is currently empty.",
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.HTML
            )
            return

        has_next = len(messages) > 5
        page_messages = messages[:5]
        
        text = "📩 <b>Your NITRIS Inbox</b>\n\n"
        builder = InlineKeyboardBuilder()
        select_buttons = []
        
        for idx, msg in enumerate(page_messages, start=1):
            status_icon = "🔴" if not msg.is_read else "⚪"
            sent_str = msg.sent_on.strftime("%d %b")
            sender_clean = msg.sender[:30] + "..." if len(msg.sender) > 30 else msg.sender
            subject_clean = msg.subject[:40] + "..." if len(msg.subject) > 40 else msg.subject
            
            text += (
                f"<b>{idx}.</b> {status_icon} <b>{sent_str}</b> | <i>{esc(sender_clean)}</i>\n"
                f"   <b>Subject:</b> {esc(subject_clean)}\n\n"
            )
            
            select_buttons.append(
                types.InlineKeyboardButton(text=str(idx), callback_data=f"msg_{msg.id}")
            )
            
        builder.row(*select_buttons)
        builder.row(types.InlineKeyboardButton(text="📬 Read Latest Message", callback_data="inbox_latest"))
        
        if has_next:
            builder.row(types.InlineKeyboardButton(text="⏩ More", callback_data="inbox_page_2"))
            
        builder.row(
            types.InlineKeyboardButton(text="🔄 Refresh Now", callback_data="inbox_refresh"),
            types.InlineKeyboardButton(text="🔍 Search Inbox", callback_data="inbox_search_prompt")
        )
        builder.row(
            types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard")
        )
        
        await message.answer(
            text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        return
        
    query = args[1].strip()
    
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
            return
            
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        results = await inbox_repo.search_messages(user.id, query, limit=5)
        
    await render_search_results(message, query, results)


@dp.message(Command("latest"), StateFilter(None))
async def cmd_latest(message: types.Message) -> None:
    telegram_id = message.from_user.id
    
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
            return
            
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        messages = await inbox_repo.get_latest_messages(user.id, offset=0, limit=1)
        
        if not messages:
            await message.answer("📩 Your inbox is currently empty. Run a sync first!")
            return
            
        msg = messages[0]
        await render_single_message(message, user, msg, session)


@dp.callback_query(F.data == "inbox_latest")
async def handle_inbox_latest(callback: types.CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id
    try:
        await callback.answer()
    except Exception:
        pass
        
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await callback.message.answer("⚠️ You are not registered. Use /start to register.")
            return
            
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        messages = await inbox_repo.get_latest_messages(user.id, offset=0, limit=1)
        
        if not messages:
            await callback.message.answer("📩 Your inbox is currently empty.")
            return
            
        msg = messages[0]
        await render_single_message(callback, user, msg, session)


async def render_search_results(message: types.Message, query: str, results: list[InboxMessage]) -> None:
    """Helper to render inbox search findings similarly to the inbox list menu."""
    if not results:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔍 Search Again", callback_data="inbox_search_prompt"))
        builder.row(
            types.InlineKeyboardButton(text="📬 Inbox Menu", callback_data="db_inbox"),
            types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard")
        )
        await message.answer(
            f"🔍 <b>Search Results</b>\n\n"
            f"No matching notices found for: \"<b>{esc(query)}</b>\".\n\n"
            f"Please check your spelling or try search terms with different keywords.",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        return
        
    text = f"🔍 <b>Search Results for \"{esc(query)}\"</b>\n\n"
    builder = InlineKeyboardBuilder()
    select_buttons = []
    
    for idx, msg in enumerate(results, start=1):
        status_icon = "🔴" if not msg.is_read else "⚪"
        sent_str = msg.sent_on.strftime("%d %b")
        sender_clean = msg.sender[:30] + "..." if len(msg.sender) > 30 else msg.sender
        subject_clean = msg.subject[:40] + "..." if len(msg.subject) > 40 else msg.subject
        
        text += (
            f"<b>{idx}.</b> {status_icon} <b>{sent_str}</b> | <i>{esc(sender_clean)}</i>\n"
            f"   <b>Subject:</b> {esc(subject_clean)}\n\n"
        )
        
        select_buttons.append(
            types.InlineKeyboardButton(text=str(idx), callback_data=f"msg_{msg.id}")
        )
        
    builder.row(*select_buttons)
    builder.row(types.InlineKeyboardButton(text="🔍 Search Again", callback_data="inbox_search_prompt"))
    builder.row(
        types.InlineKeyboardButton(text="📬 Inbox Menu", callback_data="db_inbox"),
        types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard")
    )
    
    await message.answer(
        text,
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )


# --- NITRIS Previous Year Question Papers Handlers ---

from app.services.examination_service import ExaminationService
from app.db.repositories.snapshot_repository import SnapshotRepository
from app.db.models import QuestionPaperCache

YEAR_MAP = {
    
    "2526S": "2025-26/Autumn",
    "2425A": "2024-25/Autumn",
    "2324A": "2023-24/Autumn",
    "2223A": "2022-23/Autumn"
}

REVERSE_YEAR_MAP = {v: k for k, v in YEAR_MAP.items()}


@dp.message(Command("papers"), StateFilter(None))
async def cmd_papers(message: types.Message, state: FSMContext, explicit_telegram_id: int | None = None) -> None:
    """Entry point for Previous Year Question Papers flow. Resolves current subjects automatically."""
    telegram_id = explicit_telegram_id or message.from_user.id
    await state.clear()
    
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
            return
            
        snapshot_repo = SnapshotRepository(session)
        snapshot = await snapshot_repo.get_latest_snapshot(user.id, "attendance")
        
    courses = []
    if snapshot and getattr(snapshot, "snapshot_json", None) and "records" in snapshot.snapshot_json:
        courses = snapshot.snapshot_json["records"]
        
    text = (
        "📚 <b>Previous Year Question Papers</b>\n\n"
        "Here are your registered courses for the current semester. "
        "Select one below to find historical exam papers, or search for other subjects:\n\n"
    )
    
    builder = InlineKeyboardBuilder()
    
    if courses:
        for idx, course in enumerate(courses, start=1):
            code = course.get("subject_code", "Unknown")
            name = course.get("subject_name", "Unknown")
            text += f"<b>{idx}.</b> <code>{esc(code)}</code> | <i>{esc(name)}</i>\n"
            builder.row(types.InlineKeyboardButton(text=f"📚 {code} - {name[:25]}...", callback_data=f"qp_sub_{code}"))
        text += "\n"
    else:
        text += "<i>No registered courses found in your attendance snapshot. Use /attendance to update them!</i>\n\n"
        
    builder.row(types.InlineKeyboardButton(text="🔍 Search Other Subjects", callback_data="qp_search_prompt"))
    if courses:
        builder.row(types.InlineKeyboardButton(text="📥 Download All Current Papers", callback_data="qp_dlall_prompt"))
    builder.row(types.InlineKeyboardButton(text="🏠 Back to Dashboard", callback_data="inbox_back_dashboard"))
    
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("qp_sub_"))
async def handle_subject_selected(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Callback triggered when a student selects a subject. Renders year selector."""
    subject_code = callback.data[7:]
    
    try:
        await callback.answer()
    except Exception:
        pass
        
    text = (
        f"📅 <b>Select Academic Year</b>\n\n"
        f"Subject: <b>{esc(subject_code)}</b>\n\n"
        f"Please select the historical exam year you want to retrieve papers for:"
    )
    
    builder = InlineKeyboardBuilder()
    for code, label in YEAR_MAP.items():
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"qp_yr_{subject_code}_{code}"))
        
    builder.row(types.InlineKeyboardButton(text="◀️ Back to Subjects", callback_data="qp_back_subjects"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "qp_back_subjects")
async def handle_qp_back_subjects(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        pass
        
    try:
        await callback.message.delete()
    except Exception:
        pass
        
    await cmd_papers(callback.message, state, explicit_telegram_id=callback.from_user.id)


@dp.callback_query(F.data.startswith("qp_yr_"))
async def handle_year_selected(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Triggered when user picks an academic year for a subject. Single-flight dedup enabled."""
    telegram_id = callback.from_user.id
    data = callback.data[6:]
    subject_code, year_code = data.rsplit("_", 1)
    full_year_str = YEAR_MAP.get(year_code, "2025-26/Spring")

    try:
        await callback.answer("⏳ Locating question papers...")
    except Exception:
        pass

    status_msg = await callback.message.answer("⏳ Querying question paper database cache...")

    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

        if not user:
            await status_msg.edit_text("❌ You are not registered. Use /start to register.")
            return

        exam_service = ExaminationService(session)
        mid_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "mid_sem")
        end_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "end_sem")

    if not mid_cache and not end_cache:
        from app.nitris.job_queue import nitris_job_queue, Priority
        from app.nitris.gateway import NitrisCircuitOpenError
        from app.services.examination_service import _clean_code
        
        clean_subj = _clean_code(subject_code)
        dedup_key = f"qp_metadata:{clean_subj}:{full_year_str}"
        
        await status_msg.edit_text(
            "⏳ <b>Fetching paper metadata from NITRIS...</b>\n\n"
            "<i>If other students are requesting the same paper, this request "
            "is being shared with them to avoid hammering the portal.</i>",
            parse_mode=ParseMode.HTML,
        )
        
        try:
            future = await nitris_job_queue.enqueue(
                job_type="qp_metadata_fetch",
                user_id=user.id,
                priority=Priority.MEDIUM,
                dedup_key=dedup_key,
                payload={
                    "academic_year": full_year_str,
                    "subject_code": subject_code,
                    "roll_number": user.roll_number,
                },
                timeout=90.0,
            )
            
            try:
                result = await asyncio.wait_for(future, timeout=90.0)
            except asyncio.TimeoutError:
                await status_msg.edit_text(
                    "⏳ <b>Metadata fetch is taking longer than expected.</b>\n\n"
                    "NITRIS may be slow. Please try again in a moment — your request "
                    "is queued and will complete shortly.",
                    parse_mode=ParseMode.HTML,
                )
                return
            
            if not result.get("success"):
                error = result.get("error", "Unknown error")
                await status_msg.edit_text(
                    f"❌ <b>Portal query failed</b>\n\n"
                    f"Couldn't reach NITRIS to check for papers.\n"
                    f"Error: <code>{html.escape(str(error)[:200])}</code>\n\n"
                    f"Please try again in a moment.",
                    parse_mode=ParseMode.HTML,
                )
                return
            
            parsed_records = result.get("parsed_records", [])
            
        except NitrisCircuitOpenError:
            await status_msg.edit_text(
                "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
                "The system is protecting the portal from overload. "
                "Please try again in ~60 seconds.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            async with get_db_session() as session:
                exam_service = ExaminationService(session)
                await exam_service.persist_subject_metadata(
                    parsed_records=parsed_records,
                    academic_year=full_year_str,
                    subject_code=subject_code,
                )
                await session.commit()
                mid_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "mid_sem")
                end_cache = await exam_service.get_cached_paper(subject_code, full_year_str, "end_sem")
        except Exception as e:
            logger.error("Failed persisting paper metadata: %r", e)
            await status_msg.edit_text(
                f"❌ <b>Failed to cache paper metadata</b>\n\n"
                f"Error: <code>{html.escape(str(e)[:200])}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    has_available = (
        (mid_cache and mid_cache.status != "paper_not_available") or
        (end_cache and end_cache.status != "paper_not_available")
    )
    if not has_available:
        await status_msg.edit_text(
            f"ℹ️ <b>No paper available</b>\n\n"
            f"📖 Subject: <b>{esc(subject_code)}</b>\n"
            f"📅 Year: <b>{esc(full_year_str)}</b>\n\n"
            f"NITRIS portal confirmed no question papers are uploaded for this "
            f"subject and year. This is normal for lab / 1-credit subjects.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    text = (
        f"📝 <b>Download Question Papers</b>\n\n"
        f"📖 Subject: <b>{esc(subject_code)}</b>\n"
        f"📅 Session: <b>{esc(full_year_str)}</b>\n\n"
        f"Tap a paper to download. Already-cached papers deliver instantly."
    )

    builder = InlineKeyboardBuilder()
    if mid_cache and mid_cache.status != "paper_not_available":
        mid_label = "📝 Download Mid Sem"
        if mid_cache.status == "paper_available" and mid_cache.telegram_file_id:
            mid_label += " 🚀"
        builder.row(types.InlineKeyboardButton(text=mid_label, callback_data=f"qp_dl_{mid_cache.id}"))
    if end_cache and end_cache.status != "paper_not_available":
        end_label = "📝 Download End Sem"
        if end_cache.status == "paper_available" and end_cache.telegram_file_id:
            end_label += " 🚀"
        builder.row(types.InlineKeyboardButton(text=end_label, callback_data=f"qp_dl_{end_cache.id}"))
    builder.row(
        types.InlineKeyboardButton(text="◀️ Select Year", callback_data=f"qp_sub_{subject_code}"),
        types.InlineKeyboardButton(text="🏠 Dashboard", callback_data="inbox_back_dashboard"),
    )

    await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("qp_dl_"))
async def handle_paper_download(callback: types.CallbackQuery, state: FSMContext) -> None:
    if qpaper_service is None:
        try:
            await callback.answer("❌ Service not initialized. Restart bot.", show_alert=True)
        except Exception:
            pass
        return

    telegram_id = callback.from_user.id
    cache_id = int(callback.data.split("_")[-1])

    # Check if paper is already available in cache for instant delivery
    snap = await qpaper_service._read_cache(cache_id)
    is_cached = snap and snap[0] == "paper_available" and snap[1]

    if is_cached:
        try:
            await callback.answer("🚀 Delivering cached paper...")
        except Exception:
            pass
        result: QPResult = await qpaper_service.deliver(cache_id, telegram_id)
        if not result.delivered:
            status_msg = await callback.message.answer("⚠️ Processing paper...")
            await _present_qp_result(status_msg, result)
        return

    try:
        await callback.answer("⏳ Fetching from portal...")
    except Exception:
        pass

    status_msg = await callback.message.answer("⏳ Acquiring paper from NITRIS portal...")
    result: QPResult = await qpaper_service.deliver(cache_id, telegram_id)
    await _present_qp_result(status_msg, result)


@dp.callback_query(F.data == "qp_dlall_prompt")
async def handle_qp_download_all_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

    text = (
        f"📅 <b>Select Academic Year for Batch Download</b>\n\n"
        f"Please select the historical exam year you want to retrieve papers for all your current courses:"
    )

    builder = InlineKeyboardBuilder()
    for code, label in YEAR_MAP.items():
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"qp_dlall_yr_{code}"))

    builder.row(types.InlineKeyboardButton(text="◀️ Back to Subjects", callback_data="qp_back_subjects"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("qp_dlall_yr_"))
async def handle_qp_download_all_year(callback: types.CallbackQuery, state: FSMContext) -> None:
    from app.db.repositories.snapshot_repository import SnapshotRepository

    if qpaper_service is None:
        try:
            await callback.answer("❌ Service not initialized. Restart bot.", show_alert=True)
        except Exception:
            pass
        return

    telegram_id = callback.from_user.id
    year_code = callback.data.split("_")[-1]
    selected_year = YEAR_MAP.get(year_code)

    try:
        await callback.answer()
    except Exception:
        pass

    if not selected_year:
        await callback.message.answer("❌ Invalid academic year selected.")
        return

    status_msg = await callback.message.answer("⏳ Resolving current semester courses...")

    async with get_db_session() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        if not user:
            await status_msg.edit_text("❌ You are not registered. Use /start to register.")
            return
        snapshot_repo = SnapshotRepository(session)
        snapshot = await snapshot_repo.get_latest_snapshot(user.id, "attendance")
        if not snapshot or not getattr(snapshot, "snapshot_json", None) or "records" not in snapshot.snapshot_json:
            await status_msg.edit_text(
                "❌ No registered subjects found in your latest attendance snapshot. "
                "Run /attendance first."
            )
            return
        courses = list(snapshot.snapshot_json["records"])
        user_id = user.id

    total_courses = len(courses)
    await status_msg.edit_text(
        f"⏳ Checking paper catalog for {total_courses} subjects..."
    )

    cache_ids_to_deliver: list[int] = []
    uncached_courses: list[dict] = []

    async with get_db_session() as session:
        exam_service = ExaminationService(session)
        for course in courses:
            sub_code = course.get("subject_code") or ""
            if not sub_code:
                continue
            mid_cache = await exam_service.get_cached_paper(sub_code, selected_year, "mid_sem")
            end_cache = await exam_service.get_cached_paper(sub_code, selected_year, "end_sem")
            if mid_cache and mid_cache.status != "paper_not_available":
                cache_ids_to_deliver.append(mid_cache.id)
            if end_cache and end_cache.status != "paper_not_available":
                cache_ids_to_deliver.append(end_cache.id)
            if not mid_cache and not end_cache:
                uncached_courses.append(course)

    if uncached_courses:
        await status_msg.edit_text(
            f"⏳ Syncing catalogs for {len(uncached_courses)} uncached subjects from NITRIS..."
        )
        all_parsed: list[tuple[str, list]] = []
        
        from app.nitris.job_queue import nitris_job_queue, Priority
        from app.nitris.gateway import NitrisCircuitOpenError
        from app.services.examination_service import _clean_code
        
        for course in uncached_courses:
            sub_code = course.get("subject_code") or ""
            if not sub_code:
                continue
            try:
                clean_subj = _clean_code(sub_code)
                dedup_key = f"qp_metadata:{clean_subj}:{selected_year}"
                
                future = await nitris_job_queue.enqueue(
                    job_type="qp_metadata_fetch",
                    user_id=user.id,
                    priority=Priority.MEDIUM,
                    dedup_key=dedup_key,
                    payload={
                        "academic_year": selected_year,
                        "subject_code": sub_code,
                        "roll_number": user.roll_number,
                    },
                    timeout=90.0,
                )
                
                try:
                    result = await asyncio.wait_for(future, timeout=90.0)
                    if result.get("success"):
                        records = result.get("parsed_records", [])
                        all_parsed.append((sub_code, records))
                    else:
                        logger.warning(
                            "Batch metadata fetch failed for %s %s: %s",
                            sub_code, selected_year, result.get("error", "unknown"),
                        )
                except asyncio.TimeoutError:
                    logger.warning("Metadata fetch timed out for %s %s", sub_code, selected_year)
            
            except NitrisCircuitOpenError:
                logger.warning("Circuit open during batch metadata fetch — stopping")
                break
            except Exception as e:
                logger.warning("Batch metadata fetch failed for %s %s: %r", sub_code, selected_year, e)

        if all_parsed:
            async with get_db_session() as session:
                exam_service = ExaminationService(session)
                for sub_code, records in all_parsed:
                    persisted = await exam_service.persist_subject_metadata(
                        parsed_records=records,
                        academic_year=selected_year,
                        subject_code=sub_code,
                    )
                    for rec in persisted:
                        if rec.status != "paper_not_available" and rec.id not in cache_ids_to_deliver:
                            cache_ids_to_deliver.append(rec.id)
                await session.commit()

    if not cache_ids_to_deliver:
        await status_msg.edit_text(
            "ℹ️ <b>No papers available</b> for any of your current subjects "
            f"in <b>{esc(selected_year)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    total = len(cache_ids_to_deliver)
    await status_msg.edit_text(f"⏳ Delivering {total} papers — cache hits are instant...")

    succeeded = 0
    not_available = 0
    failed = 0
    errors: list[str] = []

    for i, cache_id in enumerate(cache_ids_to_deliver, start=1):
        result: QPResult = await qpaper_service.deliver(cache_id, telegram_id)
        if result.delivered:
            succeeded += 1
        elif result.not_available:
            not_available += 1
        else:
            failed += 1
            if result.error:
                errors.append(f"Paper #{cache_id}: {result.error[:80]}")
        if i % 3 == 0 or i == total:
            try:
                await status_msg.edit_text(
                    f"⏳ Delivering papers — {i}/{total} done "
                    f"({succeeded}✓ {not_available}ℹ️ {failed}✗)"
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)

    summary = (
        f"📋 <b>Batch download complete</b>\n\n"
        f"📅 Year: <b>{esc(selected_year)}</b>\n"
        f"✅ Delivered: <b>{succeeded}</b>\n"
        f"ℹ️ No paper available: <b>{not_available}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
    )
    if errors:
        summary += "\n<b>Errors:</b>\n" + "\n".join(f"• {html.escape(e)}" for e in errors[:5])
        if len(errors) > 5:
            summary += f"\n... and {len(errors) - 5} more"
    await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)


async def _present_qp_result(status_msg: types.Message, result: QPResult) -> None:
    try:
        if result.delivered:
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        if result.not_available:
            await status_msg.edit_text(
                "ℹ️ <b>No paper available</b>\n\n"
                "NITRIS confirmed this subject has no question paper for this exam type.\n"
                "This is normal for lab / 1-credit subjects.",
                parse_mode=ParseMode.HTML,
            )
            return

        if result.in_progress:
            await status_msg.edit_text(
                "⏳ <b>Acquisition in progress</b>\n\n"
                "Another student is currently fetching this paper from NITRIS. "
                "Tap the button again in ~30 seconds — it will deliver instantly "
                "once cached.",
                parse_mode=ParseMode.HTML,
            )
            return

        if result.permanent:
            await status_msg.edit_text(
                f"❌ <b>Paper unavailable</b>\n\n"
                f"This paper could not be acquired after multiple attempts.\n"
                f"Reason: <code>{html.escape(result.error or 'unknown')[:300]}</code>\n\n"
                f"Contact support if this persists.",
                parse_mode=ParseMode.HTML,
            )
            return

        await status_msg.edit_text(
            f"⚠️ <b>Temporary error fetching paper</b>\n\n"
            f"The system failed to fetch this paper right now. Please try again.\n"
            f"Error: <code>{html.escape(result.error or 'unknown')[:300]}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("Failed to present QP result to user: %r", e)
        try:
            await status_msg.edit_text("❌ Internal error. Please try again.")
        except Exception:
            pass


@dp.callback_query(F.data == "qp_search_prompt")
async def handle_qp_search_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.answer()
    except Exception:
        pass
        
    await state.set_state(QuestionPaperFlow.waiting_for_search_query)
    
    text = (
        f"🔍 <b>Search Question Papers</b>\n\n"
        f"Please enter a subject code (e.g. <b>BM1002</b>) or a course name keyword (e.g. <b>Chemistry</b>):"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🚫 Cancel Search", callback_data="qp_back_subjects"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@dp.message(QuestionPaperFlow.waiting_for_search_query)
async def process_qp_search_query(message: types.Message, state: FSMContext) -> None:
    """Processes search queries via the job queue (Phase 6)."""
    query = message.text.strip()
    telegram_id = message.from_user.id
    
    if len(query) < 2:
        await message.answer("❌ Search query is too short. Please enter at least 2 characters:")
        return
        
    status_msg = await message.answer(f"🔍 Searching for <b>\"{esc(query)}\"</b> on NITRIS portal...")
    
    if query.startswith("/"):
        await state.clear()
        try:
            await status_msg.delete()
        except Exception:
            pass
        return
        
    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)
        
        if not user:
            await status_msg.edit_text("❌ You are not registered. Use /start to register.")
            await state.clear()
            return
    
    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError
    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_PAPERS_SEARCH
    
    allowed, wait = await operation_cooldown.check(
        user.id, "qp_search", key=query,
        cooldown_seconds=COOLDOWN_PAPERS_SEARCH,
    )
    if not allowed:
        await status_msg.edit_text(
            f"⏳ You just searched for this. Please wait {wait}s before trying again.",
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return
    
    try:
        future = await nitris_job_queue.enqueue(
            job_type="qp_search",
            user_id=user.id,
            priority=Priority.MEDIUM,
            dedup_key=f"qp_search:{query.lower()}",
            payload={"query": query},
        )
        
        try:
            result = await asyncio.wait_for(future, timeout=90.0)
        except asyncio.TimeoutError:
            await status_msg.edit_text(
                "⏳ <b>Search is taking longer than expected.</b>\n\n"
                "NITRIS may be slow. Please try again in a moment.",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return
        
        if not result.get("success"):
            error = result.get("error", "Unknown error")
            await status_msg.edit_text(
                f"❌ Portal query failed: {html.escape(str(error)[:200])}",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return
        
        records = result.get("records", [])
        parsed_records = records
    
    except NitrisCircuitOpenError:
        await status_msg.edit_text(
            "⚠️ <b>NITRIS is temporarily unavailable.</b>\n\n"
            "The system is protecting the portal from overload. "
            "Please try again in ~60 seconds.",
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return
            
    if not parsed_records:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔍 Search Again", callback_data="qp_search_prompt"))
        builder.row(types.InlineKeyboardButton(text="◀️ Back to Menu", callback_data="qp_back_subjects"))
        
        await status_msg.edit_text(
            f"🔍 <b>Search Results</b>\n\n"
            f"No matching subjects found on NITRIS for: \"<b>{esc(query)}</b>\".\n\n"
            f"Please verify the subject code or course spelling and try again.",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        await state.clear()
        return
        
    unique_subjects = {}
    for r in parsed_records:
        unique_subjects[r.subject_code] = r.subject_name
        
    await state.clear()
    
    if len(unique_subjects) == 1:
        subject_code = list(unique_subjects.keys())[0]
        try:
            await status_msg.delete()
        except Exception:
            pass
        
        builder = InlineKeyboardBuilder()
        for code, label in YEAR_MAP.items():
            builder.row(types.InlineKeyboardButton(text=label, callback_data=f"qp_yr_{subject_code}_{code}"))
        builder.row(types.InlineKeyboardButton(text="◀️ Search Menu", callback_data="qp_search_prompt"))
        
        await message.answer(
            f"📅 <b>Select Academic Year</b>\n\n"
            f"Subject: <b>{esc(subject_code)} - {esc(unique_subjects[subject_code])}</b>\n\n"
            f"Please select the historical exam year you want to retrieve papers for:",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        return
        
    try:
        await status_msg.delete()
    except Exception:
        pass
    
    text = f"🔍 <b>Search Results for \"{esc(query)}\"</b>\n\nSelect a subject from the matches below:\n\n"
    builder = InlineKeyboardBuilder()
    
    for idx, (code, name) in enumerate(unique_subjects.items(), start=1):
        text += f"<b>{idx}.</b> <code>{esc(code)}</code> | <i>{esc(name)}</i>\n"
        builder.row(types.InlineKeyboardButton(text=f"📚 {code} - {name[:25]}...", callback_data=f"qp_sub_{code}"))
        
    builder.row(types.InlineKeyboardButton(text="🔍 Search Again", callback_data="qp_search_prompt"))
    builder.row(types.InlineKeyboardButton(text="🏠 Back to Menu", callback_data="qp_back_subjects"))
    
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


# ── Admin Commands (Phase 0 + Phase 1 telemetry) ────────────────────

def is_admin(user_id: int) -> bool:
    """Check if a Telegram user ID is in the admin list."""
    from app.config import config
    return user_id in config.ADMIN_TELEGRAM_IDS


@dp.message(Command("status"), StateFilter("*"))
async def cmd_status(message: types.Message):
    """Admin command: show system health and NITRIS gateway metrics."""
    if not is_admin(message.from_user.id):
        return
    
    from app.nitris.gateway import nitris_gateway
    from app.nitris.job_queue import nitris_job_queue
    from app.nitris.rate_limiter import operation_cooldown
    from sqlalchemy import text as sql_text
    
    gw = nitris_gateway.get_metrics()
    queue_depth = nitris_job_queue.get_queue_depth()
    dedup_count = nitris_job_queue.get_active_dedup_count()
    cooldown_stats = operation_cooldown.get_stats()
    
    try:
        async with get_db_session() as session:
            user_count = (await session.execute(
                sql_text("SELECT COUNT(*) FROM users")
            )).scalar()
            valid_creds = (await session.execute(
                sql_text("SELECT COUNT(*) FROM users WHERE credentials_valid = TRUE")
            )).scalar()
            pending_events = (await session.execute(
                sql_text("SELECT COUNT(*) FROM events WHERE sent=false AND permanent_failure=false")
            )).scalar()
            stuck_qps = (await session.execute(
                sql_text("SELECT COUNT(*) FROM question_paper_caches WHERE status='fetch_in_progress' AND (lease_expires_at < NOW() OR (lease_expires_at IS NULL AND acquired_at < NOW() - INTERVAL '5 minutes'))")
            )).scalar()
            perm_failed = (await session.execute(
                sql_text("SELECT COUNT(*) FROM question_paper_caches WHERE status='permanent_failure'")
            )).scalar()
            available_qps = (await session.execute(
                sql_text("SELECT COUNT(*) FROM question_paper_caches WHERE status='paper_available'")
            )).scalar()

            # User activity stats (DAU/WAU/MAU, signup trend, sync health)
            from app.bot.handlers.admin_stats import fetch_user_activity_stats
            user_activity_stats = await fetch_user_activity_stats(session)
    except Exception as e:
        logger.error("Failed to fetch DB stats: %r", e)
        user_count = valid_creds = pending_events = stuck_qps = perm_failed = available_qps = "ERROR"
        user_activity_stats = {}

    from app.bot.handlers.admin_stats import format_user_activity_section
    user_activity_section = format_user_activity_section(
        user_activity_stats, user_count, valid_creds,
    )
    
    circuit_emoji = {
        "closed": "🟢",
        "open": "🔴",
        "half_open": "🟡",
    }.get(gw.get("circuit_state", ""), "⚪")
    
    await message.answer(
        f"📊 <b>NITRClaw System Status</b>\n\n"
        f"🔧 <b>NITRIS Gateway</b>\n"
        f"  Circuit: {circuit_emoji} <b>{gw.get('circuit_state', '?')}</b>\n"
        f"  Concurrency: {gw.get('current_max_concurrent', '?')}/{gw.get('configured_max_concurrent', '?')}\n"
        f"  Login interval: {gw.get('current_login_interval', '?')}s\n"
        f"  Active: {gw.get('active_requests', 0)} requests, {gw.get('active_logins', 0)} logins\n"
        f"  Errors: {gw.get('consecutive_errors', 0)} consecutive, {gw.get('total_errors', 0)} total\n"
        f"  Total requests: {gw.get('total_requests', 0)} (logins: {gw.get('total_logins', 0)})\n"
        f"  Last error: <code>{esc(str(gw.get('last_error') or 'none'))}</code>\n\n"
        f"📋 <b>Job Queue</b>\n"
        f"  Pending: {queue_depth}\n"
        f"  Active single-flight dedups: {dedup_count}\n"
        f"  Active cooldowns: {cooldown_stats.get('active_cooldowns', 0)}\n"
        f"  Handlers: {', '.join(nitris_job_queue.get_registered_handlers())}\n\n"
        f"👥 <b>Users</b>\n"
        f"  Total: {user_count}\n"
        f"  Valid creds: {valid_creds}\n\n"
        f"{user_activity_section}\n"
        f"📬 <b>Events</b>\n"
        f"  Pending dispatch: {pending_events}\n\n"
        f"📚 <b>QP Cache</b>\n"
        f"  Available: {available_qps}\n"
        f"  Stuck (lease expired): {stuck_qps}\n"
        f"  Permanently failed: {perm_failed}\n",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("admin_reset_qp"), StateFilter("*"))
async def cmd_admin_reset_qp(message: types.Message):
    """Admin command: reset a stuck or permanently-failed QP cache row."""
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Usage: <code>/admin_reset_qp &lt;cache_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    
    try:
        cache_id = int(args[1])
    except ValueError:
        await message.answer("❌ cache_id must be an integer.")
        return
    
    from sqlalchemy import text as sql_text
    try:
        async with get_db_session() as session:
            async with session.begin():
                result = await session.execute(
                    sql_text("""
                        UPDATE question_paper_caches
                        SET status = 'retryable_failure',
                            attempt_count = 0,
                            error_message = NULL,
                            acquired_by = NULL,
                            acquired_at = NULL,
                            lease_expires_at = NULL,
                            heartbeat_at = NULL,
                            pending_file_id = NULL
                        WHERE id = :id
                        RETURNING subject_code, academic_year, exam_type
                    """),
                    {"id": cache_id},
                )
                row = result.first()
        
        if row:
            await message.answer(
                f"✅ <b>Reset QP cache_id={cache_id}</b>\n\n"
                f"Subject: <code>{esc(row[0])}</code>\n"
                f"Year: <code>{esc(row[1])}</code>\n"
                f"Exam: <code>{esc(row[2])}</code>\n\n"
                f"Status → retryable_failure, attempt_count → 0.\n"
                f"Next request will re-acquire from NITRIS.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(f"❌ QP cache_id={cache_id} not found.")
    except Exception as e:
        logger.error("admin_reset_qp failed: %r", e)
        await message.answer(f"❌ Failed: {html.escape(str(e))}")
