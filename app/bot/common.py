"""Shared formatting + keyboard builders used across multiple bot handlers.

These are pure helpers (no Dispatcher/Router dependency) so they can be imported
from any handler module and from the non-bot layers (e.g. job_handlers) without
pulling in the whole bot wiring.
"""

from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.utils import esc
from app.config import IST


def format_attendance_message(data) -> str:
    d = data.to_dict()
    msg = f"🧑‍🎓 <b>{esc(d['student_info'])}</b>\n\n<b>📊 Attendance Summary:</b>\n"
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

    msg = f"🧑‍🎓 <b>{esc(student_info)}</b>{cached_time}\n\n<b>📊 Attendance Summary:</b>\n"
    for rec in records:
        name = rec.get("subject_name") or rec.get("subject_code", "Unknown")
        msg += f"🔸 <b>{esc(name)}</b>\n"
        msg += f"   TC: {esc(rec.get('tc', '0'))} | OA: {esc(rec.get('oa', '0'))} | UA: {esc(rec.get('ua', '0'))} | LE: {esc(rec.get('le', '0'))}\n\n"
    return msg


def format_dashboard_text(user, unread_count: int = 0) -> str:
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
            last_synced_str = sync_state.last_sync.astimezone(IST).strftime("%d %b %H:%M")

    unread_label = f"🔴 {esc(unread_count)} New Messages" if unread_count > 0 else "0"
    msg = (
        f"👋 <b>Welcome back to NitrClaw!</b>\n\n"
        f"🧑‍🎓 <b>Student:</b> <code>{esc(roll)}</code>\n"
        f"📅 <b>Last Synced:</b> {last_synced_str}\n"
        f"📊 <b>Status:</b> {status_icon} {status_text}\n"
        f"📩 <b>Unread:</b> {unread_label}\n\n"
        f"Choose an action from the options below:"
    )
    return msg


# ── Persistent floating Start button (chat-level reply keyboard) ────────────
START_BUTTON_TEXT = "🏠 Start"

# Shown whenever the bot retires the floating bar for a registered user —
# right after registration completes, or on their first 🏠 tap as a registered
# user. Doubles as the carrier text for ReplyKeyboardRemove.
BAR_RETIRED_TEXT = (
    "🧹 <b>Quick-action bar retired!</b> Your dashboard is right below.\n\n"
    "ℹ️ Reach it anytime via the ☰ Menu or /start — without the pinned bar "
    "your phone's back button works normally in this chat again."
)


def build_start_reply_keyboard() -> types.ReplyKeyboardMarkup:
    """Always-visible 🏠 Start bar pinned above the text input.

    Reply keyboards are CHAT-LEVEL: once set, the bar persists across every
    later message (even ones carrying inline keyboards) until the user
    collapses it. is_persistent=True keeps it expanded permanently.
    """
    builder = ReplyKeyboardBuilder()
    builder.row(types.KeyboardButton(text=START_BUTTON_TEXT))
    return builder.as_markup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap 🏠 Start for your dashboard",
    )


def build_bar_removal_markup() -> types.ReplyKeyboardRemove:
    """Remove the chat-level 🏠 reply bar for this chat.

    sendMessage-only: editMessageText rejects ReplyKeyboardRemove with the
    same pydantic ValidationError that ReplyKeyboardMarkup triggered in
    incident 2026-08-24. Only ever attach this to message.answer().
    """
    return types.ReplyKeyboardRemove()


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
        types.InlineKeyboardButton(text="📚 Previous Papers", callback_data="db_papers"),
    )
    builder.row(
        types.InlineKeyboardButton(text="🎉 Holidays", callback_data="db_holidays"),
    )
    builder.row(
        types.InlineKeyboardButton(text="🔄 Update Credentials", callback_data="db_update"),
        types.InlineKeyboardButton(text="❌ Deregister", callback_data="db_deregister")
    )
    return builder.as_markup()
