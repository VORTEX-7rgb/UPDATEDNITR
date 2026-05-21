import logging
import re
from datetime import datetime, timezone
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
from app.db.models import User, SyncState

logger = logging.getLogger(__name__)


class Registration(StatesGroup):
    waiting_for_roll = State()      # Waiting for 9-char NITRIS Roll Number
    waiting_for_password = State()  # Waiting for NITRIS Password


class Deregistration(StatesGroup):
    waiting_for_confirm = State()   # Waiting for the user to type DELETE


dp = Dispatcher()


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
        return True  # Suppress the error from crashing the dispatcher or printing raw tracebacks
    return False



def format_attendance_message(data) -> str:
    d = data.to_dict()
    msg = f"🧑‍🎓 <b>{d['student_info']}</b>\n\n<b>📊 Attendance Summary:</b>\n"
    for rec in d["records"]:
        name = rec.get("subject_name") or rec.get("subject_code", "Unknown")
        msg += f"🔸 <b>{name}</b>\n"
        msg += f"   TC: {rec['tc']} | OA: {rec['oa']} | UA: {rec['ua']} | LE: {rec['le']}\n\n"
    return msg


def format_dashboard_text(user: User) -> str:
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
            status_text = f"Sync Delay (failures: {sync_state.failure_count})"
            if sync_state.last_error:
                status_text += f"\n<i>Error: {sync_state.last_error[:100]}</i>"
        else:
            status_icon = "❌"
            status_text = "Outage / Connection Error"
            if sync_state.last_error:
                status_text += f"\n<i>Error: {sync_state.last_error[:100]}</i>"
                
        if sync_state.last_sync:
            last_synced_str = sync_state.last_sync.strftime("%d %b %H:%M")
            
    msg = (
        f"👋 <b>Welcome back to NitrClaw!</b>\n\n"
        f"🧑‍🎓 <b>Student:</b> <code>{roll}</code>\n"
        f"📅 <b>Last Synced:</b> {last_synced_str}\n"
        f"📊 <b>Status:</b> {status_icon} {status_text}\n\n"
        f"Choose an action from the options below:"
    )
    return msg


def get_dashboard_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Get Latest Attendance", callback_data="db_attendance"))
    builder.row(
        types.InlineKeyboardButton(text="🔄 Update Credentials", callback_data="db_update"),
        types.InlineKeyboardButton(text="❌ Deregister", callback_data="db_deregister")
    )
    return builder.as_markup()


# --- Global Command Overrides with high priority (StateFilter("*")) ---

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
        
    if user:
        await state.clear()
        text = format_dashboard_text(user)
        await message.answer(text, reply_markup=get_dashboard_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await state.clear()
        await state.set_state(Registration.waiting_for_roll)
        await message.answer("👋 Welcome to NitrClaw!\n\nPlease enter your NITRIS Roll Number (e.g. 125AI0003):")


# --- FSM Command Shielding ---

@dp.message(Registration.waiting_for_roll, F.text.startswith("/"))
@dp.message(Registration.waiting_for_password, F.text.startswith("/"))
@dp.message(Deregistration.waiting_for_confirm, F.text.startswith("/"))
async def registration_command_shield(message: types.Message):
    await message.answer(
        "⚠️ <b>Registration or update is in progress.</b>\n\n"
        "Please complete the active steps, or send /cancel to abort the process before running other commands.",
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
        
    try:
        data = await get_attendance_data(roll, password)
    except LoginError as e:
        logger.error("Login verification failed for %s: %s", roll, e)
        await status_msg.edit_text(
            f"❌ <b>Login failed: Invalid credentials.</b>\n\n"
            f"Please enter your NITRIS password again (or send /cancel to abort):",
            parse_mode=ParseMode.HTML
        )
        return
    except Exception as e:
        logger.error("Portal error during verification for %s: %r", roll, e)
        await status_msg.edit_text(
            f"❌ <b>Portal connection issue.</b>\n\n"
            f"Could not reach or parse the NITRIS portal: {str(e)}\n\n"
            f"Please check portal availability and enter your password again, or send /cancel to abort:",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        async with get_db_session() as session:
            async with session.begin():
                user_repo = UserRepository(session)
                existing_user = await user_repo.get_by_telegram_id(telegram_id)
                if existing_user:
                    await user_repo.update_credentials(existing_user.id, roll, password)
                    user_id = existing_user.id
                else:
                    new_user = await user_repo.create_user(telegram_id, roll, password)
                    user_id = new_user.id
                    
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
                
        await status_msg.edit_text(
            "✅ <b>Registration complete!</b>\n\n"
            "Initial attendance fetched successfully. Rendering your dashboard...",
            parse_mode=ParseMode.HTML
        )
        
        async with get_db_session() as session:
            stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            
        if user:
            text = format_dashboard_text(user)
            await message.answer(text, reply_markup=get_dashboard_keyboard(), parse_mode=ParseMode.HTML)
            
        await state.clear()
        
    except Exception as e:
        logger.error("Failed to complete database updates during registration: %r", e)
        await status_msg.edit_text("❌ A database error occurred during registration. Please use /start to retry.")
        await state.clear()


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
            
        if user:
            text = format_dashboard_text(user)
            await message.answer(text, reply_markup=get_dashboard_keyboard(), parse_mode=ParseMode.HTML)
        else:
            await message.answer("⚠️ You are not registered. Please use /start to register.")


# --- Dashboard Inline Callbacks ---

@dp.callback_query(F.data.startswith("db_"))
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
        
    if user:
        text = format_dashboard_text(user)
        await callback.message.answer(text, reply_markup=get_dashboard_keyboard(), parse_mode=ParseMode.HTML)
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


# --- Inline Helpers ---

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
    try:
        plaintext_password = decrypt_password(user.encrypted_password)
    except Exception as e:
        logger.error("Failed to decrypt password for telegram_id=%s: %r", user.telegram_id, e)
        await callback.message.answer("❌ Error decrypting credentials. Please update them using /forgot.")
        return
        
    status_msg = await callback.message.answer("⏳ Fetching attendance from NITRIS...")
    
    try:
        data = await get_attendance_data(user.roll_number, plaintext_password)
        
        try:
            async with get_db_session() as session:
                async with session.begin():
                    snapshot_service = SnapshotService(session)
                    await snapshot_service.create_snapshot_if_changed(
                        user_id=user.id,
                        module_name="attendance",
                        attendance_result=data
                    )
        except Exception as e:
            logger.error("Failed to update snapshot/events in database for user_id=%s: %r", user.id, e)
            
        await status_msg.edit_text(format_attendance_message(data), parse_mode=ParseMode.HTML)
        
    except LoginError as e:
        logger.error("Login failed for %s: %s", user.telegram_id, e)
        await status_msg.edit_text(f"❌ Login failed: {e}\n\nPlease try updating your credentials.")
    except SessionExpiredError:
        logger.error("Session expired for %s", user.telegram_id)
        await status_msg.edit_text("❌ Session expired. Please try again.")
    except AttendanceParseError as e:
        logger.error("Parse error for %s: %s", user.telegram_id, e)
        await status_msg.edit_text(f"❌ Parse error: {e}")
    except NitrisError as e:
        logger.error("NITRIS error for %s: %s", user.telegram_id, e)
        await status_msg.edit_text("❌ Could not fetch attendance. The portal might be down.")
    except Exception as e:
        logger.error("Unexpected error for %s: %r", user.telegram_id, e)
        await status_msg.edit_text("❌ An unexpected error occurred. Please try again later.")


# --- Command Handlers (Restricted to empty FSM state only for watertight shielding) ---

@dp.message(Command("attendance"), StateFilter(None))
async def cmd_attendance(message: types.Message):
    telegram_id = message.from_user.id
    
    async with get_db_session() as session:
        stmt = select(User).options(selectinload(User.sync_state)).where(User.telegram_id == telegram_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        
    if not user:
        await message.answer("⚠️ You haven't registered yet! Please use /start to register.")
        return
        
    status_msg = await message.answer("⏳ Fetching attendance from NITRIS...")
    try:
        plaintext_password = decrypt_password(user.encrypted_password)
        data = await get_attendance_data(user.roll_number, plaintext_password)
        
        try:
            async with get_db_session() as session:
                async with session.begin():
                    snapshot_service = SnapshotService(session)
                    await snapshot_service.create_snapshot_if_changed(
                        user_id=user.id,
                        module_name="attendance",
                        attendance_result=data
                    )
        except Exception as e:
            logger.error("Failed to update snapshot/events in database for user_id=%s: %r", user.id, e)
            
        await status_msg.edit_text(format_attendance_message(data), parse_mode=ParseMode.HTML)
    except LoginError as e:
        logger.error("Login failed for %s: %s", telegram_id, e)
        await status_msg.edit_text(f"❌ Login failed: {e}\n\nPlease try updating your credentials with /forgot.")
    except SessionExpiredError:
        logger.error("Session expired for %s", telegram_id)
        await status_msg.edit_text("❌ Session expired. Please try again.")
    except AttendanceParseError as e:
        logger.error("Parse error for %s: %s", telegram_id, e)
        await status_msg.edit_text(f"❌ Parse error: {e}")
    except NitrisError as e:
        logger.error("NITRIS error for %s: %s", telegram_id, e)
        await status_msg.edit_text("❌ Could not fetch attendance. The portal might be down.")
    except Exception as e:
        logger.error("Unexpected error for %s: %r", telegram_id, e)
        await status_msg.edit_text("❌ An unexpected error occurred. Please try again later.")


@dp.message(Command("help"), StateFilter(None))
async def cmd_help(message: types.Message):
    msg = (
        "🤖 <b>NitrClaw Help Menu</b>\n\n"
        "Here are the available commands:\n"
        "• /start - View dashboard or start registration\n"
        "• /register - Register / update your credentials\n"
        "• /forgot - Shortcut to update your credentials\n"
        "• /attendance - Fetch current attendance statistics\n"
        "• /cancel - Cancel the active process\n"
        "• /help - Display this help menu"
    )
    await message.answer(msg, parse_mode=ParseMode.HTML)
