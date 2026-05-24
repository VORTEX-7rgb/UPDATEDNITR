import logging
import re
import asyncio
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
from app.db.models import User, SyncState, InboxMessage

logger = logging.getLogger(__name__)


class Registration(StatesGroup):
    waiting_for_roll = State()      # Waiting for 9-char NITRIS Roll Number
    waiting_for_password = State()  # Waiting for NITRIS Password
    verifying = State()             # Currently verifying with NITRIS


class Deregistration(StatesGroup):
    waiting_for_confirm = State()   # Waiting for the user to type DELETE


class InboxSearch(StatesGroup):
    waiting_for_query = State()     # Waiting for user to send search text


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
            
    unread_label = f"🔴 {unread_count} New Messages" if unread_count > 0 else "0"
    msg = (
        f"👋 <b>Welcome back to NitrClaw!</b>\n\n"
        f"🧑‍🎓 <b>Student:</b> <code>{roll}</code>\n"
        f"📅 <b>Last Synced:</b> {last_synced_str}\n"
        f"📊 <b>Status:</b> {status_icon} {status_text}\n"
        f"📩 <b>Unread:</b> {unread_label}\n\n"
        f"Choose an action from the options below:"
    )
    return msg


def get_dashboard_keyboard(unread_count: int = 0) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📊 Get Latest Attendance", callback_data="db_attendance"))
    
    inbox_text = f"📩 Inbox ({unread_count} new)" if unread_count > 0 else "📩 Inbox"
    builder.row(
        types.InlineKeyboardButton(text=inbox_text, callback_data="db_inbox"),
        types.InlineKeyboardButton(text="🔄 Update Credentials", callback_data="db_update")
    )
    builder.row(
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
        
    # Transition to verifying state to block multiple concurrent requests
    await state.set_state(Registration.verifying)
    
    try:
        data = await get_attendance_data(roll, password)
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


# --- Dashboard Inline Callbacks ---

@dp.callback_query(F.data.in_({"db_attendance", "db_update", "db_deregister"}))
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
        "• /inbox &lt;query&gt; - View inbox or search matching notices\n"
        "• /latest - Jump straight to the most recent notice\n"
        "• /cancel - Cancel the active process\n"
        "• /help - Display this help menu"
    )
    await message.answer(msg, parse_mode=ParseMode.HTML)


# --- NITRIS Inbox Handlers ---

async def render_single_message(event, user: User, msg: InboxMessage, session) -> None:
    """Helper to load notice body (with lazy fetching if needed) and render single notice detail card."""
    # 1. Mark as read in DB if it was unread
    if not msg.is_read:
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        await inbox_repo.mark_as_read(msg.id)
        await session.commit()
        
    # 2. Check if body is empty (needs lazy loading)
    if msg.body is None:
        # Show a temporary loading alert or message in the chat
        if isinstance(event, types.CallbackQuery):
            status_msg = await event.message.answer("⏳ Fetching notice body from NITRIS portal...")
        else:
            status_msg = await event.answer("⏳ Fetching notice body from NITRIS portal...")
        
        try:
            password = decrypt_password(user.encrypted_password)
            
            from app.nitris.client import NitrisClient
            from app.nitris.parser import parse_message_detail_html
            
            client = NitrisClient()
            await client.login(user.roll_number, password)
            
            if msg.token.startswith("postback:"):
                # Submit postback to resolve redirects to direct Message.aspx?i=TOKEN URL
                event_target = msg.token.split("postback:")[1]
                real_token, detail_html = await client.submit_message_postback(event_target)
                detail_data = parse_message_detail_html(detail_html)
                
                from app.nitris.parser import extract_message_id
                portal_id = extract_message_id(real_token)
                
                # Update token, portal_message_id (healing), and body/attachments in DB
                async with get_db_session() as update_session:
                    async with update_session.begin():
                        from sqlalchemy import update as sqlalchemy_update
                        update_values = {
                            "token": real_token, 
                            "body": detail_data["body"], 
                            "attachment_url": detail_data["attachment_url"]
                        }
                        if portal_id:
                            update_values["portal_message_id"] = portal_id
                            
                        stmt = (
                            sqlalchemy_update(InboxMessage)
                            .where(InboxMessage.id == msg.id)
                            .values(**update_values)
                        )
                        await update_session.execute(stmt)
            else:
                detail_html = await client.fetch_message_detail(msg.token)
                detail_data = parse_message_detail_html(detail_html)
                
                # Update the message in DB
                async with get_db_session() as update_session:
                    async with update_session.begin():
                        from app.db.repositories.inbox_repository import InboxRepository
                        up_inbox_repo = InboxRepository(update_session)
                        await up_inbox_repo.update_message_body(
                            message_id=msg.id,
                            body=detail_data["body"],
                            attachment_url=detail_data["attachment_url"]
                        )
            
            # Reload message inside the current active session
            stmt = select(InboxMessage).where(InboxMessage.id == msg.id)
            res = await session.execute(stmt)
            msg = res.scalar_one_or_none()
            
            await client.close()
            try:
                await status_msg.delete()
            except Exception:
                pass
            
        except Exception as e:
            logger.error("Failed lazy-loading message body for message ID %s: %r", msg.id, e)
            await status_msg.edit_text(f"❌ Failed to fetch message detail from NITRIS: {str(e)}")
            return

    # Render detail card
    sent_str = msg.sent_on.strftime("%d %b %Y")
    body_text = msg.body or "<i>(No content)</i>"
    if len(body_text) > 3000:
        body_text = body_text[:3000] + "\n\n<i>[Content truncated due to Telegram size limits]</i>"
        
    card_text = (
        f"📩 <b>NITRIS Notice</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>From:</b> {msg.sender}\n"
        f"📅 <b>Date:</b> {sent_str}\n"
        f"📌 <b>Subject:</b> {msg.subject}\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"{body_text}\n"
    )
    
    builder = InlineKeyboardBuilder()
    
    # Attachment download button
    if msg.attachment_url:
        builder.row(types.InlineKeyboardButton(text="📎 Download PDF Attachment", callback_data=f"dl_{msg.id}"))
        
    # Navigation utilities
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
            f"<b>{idx}.</b> {status_icon} <b>{sent_str}</b> | <i>{sender_clean}</i>\n"
            f"   <b>Subject:</b> {subject_clean}\n\n"
        )
        
        select_buttons.append(
            types.InlineKeyboardButton(text=str(idx), callback_data=f"msg_{msg.id}")
        )
        
    # Selection row
    builder.row(*select_buttons)
    
    # Read Latest shortcut row
    builder.row(types.InlineKeyboardButton(text="📬 Read Latest Message", callback_data="inbox_latest"))
    
    # Navigation row
    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="◀️ Prev", callback_data=f"inbox_page_{page - 1}"))
    if has_next:
        nav_buttons.append(types.InlineKeyboardButton(text="⏩ More", callback_data=f"inbox_page_{page + 1}"))
        
    if nav_buttons:
        builder.row(*nav_buttons)
        
    # Utility rows
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
    telegram_id = callback.from_user.id
    
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
            
        password = decrypt_password(user.encrypted_password)
        
        # Live sync
        from app.workers.sync_worker import sync_messages_for_user
        await sync_messages_for_user(user.id, user.roll_number, password, callback.bot)
        
        await status_msg.edit_text("✅ Inbox sync completed successfully!")
        await asyncio.sleep(1)
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        # Trigger page 1 list view
        callback.data = "inbox_page_1"
        await handle_inbox_list(callback, state)
        
    except Exception as e:
        logger.error("Failed live inbox refresh for telegram_id %s: %r", telegram_id, e)
        await status_msg.edit_text(f"❌ Refresh failed: {str(e)}")


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
            
        # 1. Check if telegram_file_id is cached
        if msg.telegram_file_id:
            try:
                await callback.bot.send_document(chat_id=telegram_id, document=msg.telegram_file_id)
                return
            except Exception as e:
                logger.warning("Cached telegram_file_id failed for message ID %s: %r. Re-downloading...", msg.id, e)
                
        # 2. Live download from portal
        status_msg = await callback.message.answer("⏳ Fetching attachment from NITRIS portal...")
        
        try:
            password = decrypt_password(user.encrypted_password)
            
            from app.nitris.client import NitrisClient
            client = NitrisClient()
            await client.login(user.roll_number, password)
            
            file_bytes = await client.download_attachment(msg.attachment_url)
            await client.close()
            
            # Check 50MB limit
            MAX_FILE_SIZE = 50 * 1024 * 1024
            if len(file_bytes) > MAX_FILE_SIZE:
                from app.config import config
                direct_url = f"{config.NITRIS_BASE_URL}{msg.attachment_url}"
                await status_msg.edit_text(
                    f"⚠️ <b>Attachment is too large (&gt;50MB) for Telegram upload.</b>\n\n"
                    f"You can download it directly from the secure portal link below:\n"
                    f"🔗 <a href='{direct_url}'>Direct Download Link</a>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
                return
                
            # Sanitize filename
            import re
            sanitized_subject = re.sub(r'[^a-zA-Z0-9_\- ]', '', msg.subject)
            sanitized_subject = re.sub(r'\s+', ' ', sanitized_subject).strip()
            if not sanitized_subject:
                sanitized_subject = f"notice_attachment_{msg.id}"
            filename = f"{sanitized_subject[:50]}.pdf"
            
            from aiogram.types import BufferedInputFile
            input_file = BufferedInputFile(file_bytes, filename=filename)
            
            sent_message = await callback.bot.send_document(chat_id=telegram_id, document=input_file)
            
            if sent_message.document:
                file_id = sent_message.document.file_id
                async with get_db_session() as update_session:
                    async with update_session.begin():
                        from app.db.repositories.inbox_repository import InboxRepository
                        up_inbox_repo = InboxRepository(update_session)
                        await up_inbox_repo.update_telegram_file_id(msg.id, file_id)
                        
            try:
                await status_msg.delete()
            except Exception:
                pass
            
        except Exception as e:
            logger.error("Failed to download and send attachment for message ID %s: %r", msg.id, e)
            await status_msg.edit_text(f"❌ Failed to download attachment: {str(e)}")


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
    
    # Instantly clear state to avoid FSM trapping!
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
                f"<b>{idx}.</b> {status_icon} <b>{sent_str}</b> | <i>{sender_clean}</i>\n"
                f"   <b>Subject:</b> {subject_clean}\n\n"
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
            f"No matching notices found for: \"<b>{query}</b>\".\n\n"
            f"Please check your spelling or try search terms with different keywords.",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        return
        
    text = f"🔍 <b>Search Results for \"{query}\"</b>\n\n"
    builder = InlineKeyboardBuilder()
    select_buttons = []
    
    for idx, msg in enumerate(results, start=1):
        status_icon = "🔴" if not msg.is_read else "⚪"
        sent_str = msg.sent_on.strftime("%d %b")
        sender_clean = msg.sender[:30] + "..." if len(msg.sender) > 30 else msg.sender
        subject_clean = msg.subject[:40] + "..." if len(msg.subject) > 40 else msg.subject
        
        text += (
            f"<b>{idx}.</b> {status_icon} <b>{sent_str}</b> | <i>{sender_clean}</i>\n"
            f"   <b>Subject:</b> {subject_clean}\n\n"
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
