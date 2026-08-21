"""Inbox, message detail, search, and attachment-download handlers."""

import logging
import asyncio
import html
from datetime import datetime, timezone

from aiogram import Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import config
from app.db.database import get_db_session
from app.db.models import User, InboxMessage
from app.utils import esc, safe_truncate

from app.bot.fsm import InboxSearch
from app.bot.common import format_dashboard_text, get_dashboard_keyboard

logger = logging.getLogger(__name__)

router = Router(name="inbox_router")


# --- NITRIS Inbox Handlers ---

async def render_single_message(event, user: User, msg: InboxMessage, session=None) -> None:
    """Helper to load notice body (with lazy fetching if needed) and render single notice detail card.

    Cache-first with TTL architecture:
      1. Mark the message as read (if not already) — short dedicated session.
      2. Check if the cached body is fresh enough:
          - If msg.body is None → never fetched → fetch (lazy).
          - If msg.body_fetched_at is None → fetch (defensive — old rows).
          - If (now - body_fetched_at) > INBOX_BODY_TTL_SECONDS → stale → fetch.
          - Otherwise → render cached body instantly (zero NITRIS traffic).
      3. On fetch, enqueue an inbox_detail_fetch job with a PER-USER-PER-MSG
         dedup_key so concurrent opens collapse into a single NITRIS fetch.

    SESSION POLICY (lease-boundary discipline):
      ``session`` is accepted for backward compatibility but is NEVER used and
      NEVER held across the NITRIS fetch. Mark-as-read runs in a short local
      transaction; the fetch job persists the body itself and its return
      payload is applied directly to the in-memory row for rendering. Callers
      MUST close their own sessions before invoking this helper.
    """
    del session  # legacy parameter — intentionally unused

    # ── Short transaction: mark read (no network I/O inside this block) ──
    if not msg.is_read:
        try:
            async with get_db_session() as mark_session:
                async with mark_session.begin():
                    from app.db.repositories.inbox_repository import InboxRepository
                    await InboxRepository(mark_session).mark_as_read(msg.id)
        except Exception as e:
            logger.warning("Failed marking message id=%s read: %r", msg.id, e)

    # ── CACHE-FIRST FOREVER ───────────────────────────────
    # A stored body is served as-is, permanently. Freshness is guaranteed by:
    #   1. Background sync edit-detection (persist_inbox_sync nulls body when
    #      subject/sent_on change -> next open refetches), and
    #   2. The explicit "Refresh Now" button (full portal sync).
    # Time-based refetching was removed: notices are effectively immutable and
    # the old 30-min TTL cost a full NITRIS login per tap per message.
    need_fetch = msg.body is None

    if need_fetch:
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
                dedup_key=f"inbox_detail:user:{user.id}:msg:{msg.id}",
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

                # Apply fetched fields straight onto the in-memory row for
                # rendering. The job handler already persisted exactly these
                # values, so this mirrors DB state with zero extra round-trips
                # — and no session was ever held across the fetch above.
                fetched_body = result.get("body")
                if fetched_body is not None:
                    msg.body = fetched_body
                    msg.body_fetched_at = datetime.now(timezone.utc)
                fetched_attachment = result.get("attachment_url")
                if fetched_attachment:
                    msg.attachment_url = fetched_attachment

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


@router.callback_query(F.data == "db_inbox")
@router.callback_query(F.data.startswith("inbox_page_"))
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


@router.callback_query(F.data == "inbox_refresh")
async def handle_inbox_refresh(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Cache-first Inbox Refresh."""
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
        await callback.answer("⏳ Refreshing inbox in background...", show_alert=False)
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
        cached_messages = await inbox_repo.get_latest_messages(user.id, offset=0, limit=5)

    refreshing_text = _render_inbox_list_text(cached_messages, page=1, refreshing=True)
    try:
        status_msg = await callback.message.edit_text(
            refreshing_text,
            reply_markup=_inbox_refreshing_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        status_msg = await callback.message.answer(
            refreshing_text,
            reply_markup=_inbox_refreshing_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    from app.nitris.job_queue import nitris_job_queue, Priority
    from app.nitris.gateway import NitrisCircuitOpenError

    try:
        future = await nitris_job_queue.enqueue(
            job_type="inbox_refresh",
            user_id=user.id,
            priority=Priority.HIGH,
            dedup_key=f"inbox_refresh:user:{user.id}",
            payload={
                "callback_chat_id": status_msg.chat.id,
                "callback_message_id": status_msg.message_id,
            },
        )

        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            if result.get("success"):
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                await _render_inbox_list_message(status_msg, user.id, page=1)
            else:
                error = result.get("error", "Unknown error")
                if "circuit" in error.lower() or "open" in error.lower():
                    await status_msg.edit_text(
                        refreshing_text.replace(
                            "🔄 <i>Refreshing from NITRIS in background...</i>",
                            "⚠️ <b>NITRIS temporarily unavailable.</b> Showing cached inbox.",
                        ),
                        reply_markup=_inbox_refreshing_keyboard(),
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await status_msg.edit_text(
                        f"⚠️ <b>Refresh failed:</b> {html.escape(str(error)[:200])}\n\n"
                        + _render_inbox_list_text(cached_messages, page=1, refreshing=False),
                        reply_markup=_inbox_refreshing_keyboard(),
                        parse_mode=ParseMode.HTML,
                    )
        except asyncio.TimeoutError:
            await status_msg.edit_text(
                refreshing_text.replace(
                    "🔄 <i>Refreshing from NITRIS in background...</i>",
                    "⏳ <i>Refresh still running in background. Your inbox will update shortly.</i>",
                ),
                reply_markup=_inbox_refreshing_keyboard(),
                parse_mode=ParseMode.HTML,
            )

    except NitrisCircuitOpenError:
        await status_msg.edit_text(
            refreshing_text.replace(
                "🔄 <i>Refreshing from NITRIS in background...</i>",
                "⚠️ <b>NITRIS temporarily unavailable.</b> Showing cached inbox.",
            ),
            reply_markup=_inbox_refreshing_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("Failed live inbox refresh for telegram_id %s: %r", telegram_id, e)
        await status_msg.edit_text(
            f"❌ Refresh failed: {html.escape(str(e))}\n\n"
            + _render_inbox_list_text(cached_messages, page=1, refreshing=False),
            reply_markup=_inbox_refreshing_keyboard(),
            parse_mode=ParseMode.HTML,
        )


def _render_inbox_list_text(messages: list, page: int = 1, refreshing: bool = False) -> str:
    """Render the inbox list text body (without keyboard) for cache-first display."""
    if not messages:
        text = "📩 <b>Your NITRIS Inbox</b>\n\nYour inbox is currently empty."
    else:
        text = f"📩 <b>Your NITRIS Inbox</b> (Page {page})\n\n"
        for idx, msg in enumerate(messages, start=1):
            status_icon = "🔴" if not msg.is_read else "⚪"
            sent_str = msg.sent_on.strftime("%d %b") if hasattr(msg, "sent_on") else ""
            sender_clean = (msg.sender[:30] + "...") if hasattr(msg, "sender") and len(msg.sender) > 30 else (msg.sender if hasattr(msg, "sender") else "")
            subject_clean = (msg.subject[:40] + "...") if hasattr(msg, "subject") and len(msg.subject) > 40 else (msg.subject if hasattr(msg, "subject") else "")
            text += (
                f"<b>{idx}.</b> {status_icon} <b>{esc(sent_str)}</b> | <i>{esc(sender_clean)}</i>\n"
                f"   <b>Subject:</b> {esc(subject_clean)}\n\n"
            )

    if refreshing:
        text += "🔄 <i>Refreshing from NITRIS in background...</i>"
    return text


def _inbox_refreshing_keyboard() -> types.InlineKeyboardMarkup:
    """Keyboard shown while a refresh is in progress."""
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🏠 Back to Dashboard", callback_data="inbox_back_dashboard"))
    return builder.as_markup()


async def _render_inbox_list_message(message, user_id: int, page: int = 1) -> None:
    """Edit/send message to show the inbox list page. Used after a refresh completes."""
    async with get_db_session() as session:
        from app.db.repositories.inbox_repository import InboxRepository
        inbox_repo = InboxRepository(session)
        limit = 5
        offset = (page - 1) * limit
        messages = await inbox_repo.get_latest_messages(user_id, offset=offset, limit=limit + 1)

    if not messages:
        try:
            await message.answer(
                "📩 <b>Your NITRIS Inbox</b>\n\nYour inbox is currently empty.",
                reply_markup=_inbox_refreshing_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    has_next = len(messages) > limit
    page_messages = messages[:limit]
    text = _render_inbox_list_text(page_messages, page=page, refreshing=False)

    builder = InlineKeyboardBuilder()
    select_buttons = []
    for idx, msg in enumerate(page_messages, start=1):
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

    try:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        pass


@router.callback_query(F.data == "inbox_back_dashboard")
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


@router.callback_query(F.data.startswith("msg_"))
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

    # Session closed BEFORE rendering/fetching (lease boundary) — render opens
    # only its own short sessions.
    await render_single_message(callback, user, msg)


@router.callback_query(F.data.startswith("dl_"))
async def handle_download_attachment(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Deliver an attachment via the GLOBAL AttachmentService."""
    telegram_id = callback.from_user.id
    msg_id = int(callback.data.split("_")[1])

    try:
        await callback.answer("⏳ Processing attachment...", show_alert=False)
    except Exception:
        pass

    async with get_db_session() as session:
        from app.db.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(telegram_id)

        if not user:
            await callback.message.answer("⚠️ You are not registered. Use /start to register.")
            return

        if not user.credentials_valid:
            await callback.message.answer(
                "⚠️ <b>Your NITRIS credentials are invalid.</b>\n\n"
                "Please use /forgot to update them before downloading attachments.",
                parse_mode=ParseMode.HTML,
            )
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

        attachment_url = msg.attachment_url
        subject = msg.subject
        encrypted_password = user.encrypted_password
        roll_number = user.roll_number
        source_user_id = user.id
        existing_cache_id = msg.attachment_cache_id

    from app.nitris.rate_limiter import operation_cooldown, COOLDOWN_ATTACHMENT_DOWNLOAD
    allowed, wait = await operation_cooldown.check(
        source_user_id, "attachment_download", key=str(msg_id),
        cooldown_seconds=COOLDOWN_ATTACHMENT_DOWNLOAD,
    )
    if not allowed:
        try:
            await callback.answer(f"⏳ Please wait {wait}s before retrying.", show_alert=True)
        except Exception:
            pass
        return

    status_msg = await callback.message.answer(
        "⏳ <i>Fetching attachment...</i>\n"
        "<i>This is usually instant if someone has requested it before.</i>",
        parse_mode=ParseMode.HTML,
    )

    from app.services.attachment_service import get_attachment_service, AttachmentResult
    from app.nitris.gateway import NitrisCircuitOpenError

    try:
        attachment_service = get_attachment_service()
    except RuntimeError as e:
        logger.error("AttachmentService not initialized: %r", e)
        await status_msg.edit_text(
            "❌ <b>Service unavailable.</b>\n\nPlease try again later.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        result: AttachmentResult = await asyncio.wait_for(
            attachment_service.deliver(
                attachment_url=attachment_url,
                telegram_id=telegram_id,
                source_user_id=source_user_id,
                source_roll_number=roll_number,
                encrypted_password=encrypted_password,
                subject=subject,
            ),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text(
            "⏳ <b>Download is taking longer than expected.</b>\n\n"
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
    except Exception as e:
        logger.error("Attachment deliver failed for msg_id=%d: %r", msg_id, e)
        await status_msg.edit_text(
            f"❌ Failed to download attachment: {html.escape(str(e)[:200])}",
            parse_mode=ParseMode.HTML,
        )
        return

    if result.cache_id is not None and result.cache_id != existing_cache_id:
        try:
            async with get_db_session() as session:
                async with session.begin():
                    from app.db.repositories.inbox_repository import InboxRepository
                    inbox_repo = InboxRepository(session)
                    await inbox_repo.link_attachment_cache(msg_id, result.cache_id)
        except Exception as e:
            logger.warning(
                "Failed to link InboxMessage id=%d to AttachmentCache id=%s: %r",
                msg_id, result.cache_id, e,
            )

    if result.delivered:
        try:
            await status_msg.delete()
        except Exception:
            pass
        return

    if result.not_available:
        await status_msg.edit_text(
            "⚠️ <b>Attachment no longer exists on NITRIS.</b>\n\n"
            "The notice may have been removed by the sender.",
            parse_mode=ParseMode.HTML,
        )
        return

    if result.in_progress:
        await status_msg.edit_text(
            "⏳ <b>Attachment is being prepared.</b>\n\n"
            "Another student is fetching this file. Please tap the download "
            "button again in a few seconds for an instant delivery.",
            parse_mode=ParseMode.HTML,
        )
        return

    if result.permanent:
        await status_msg.edit_text(
            f"❌ <b>Attachment permanently unavailable.</b>\n\n"
            f"<i>{html.escape((result.error or 'Unknown error')[:200])}</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    error = result.error or "Unknown error"
    if "too large" in error.lower():
        direct_url = f"{config.NITRIS_BASE_URL}{attachment_url}"
        await status_msg.edit_text(
            "⚠️ <b>Attachment is too large (&gt;50MB) for Telegram upload.</b>\n\n"
            "You can download it directly from the secure portal link below:\n"
            f"🔗 <a href='{direct_url}'>Direct Download Link</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await status_msg.edit_text(
            f"❌ Failed to download attachment: {html.escape(str(error)[:200])}",
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data == "inbox_search_prompt")
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


@router.message(InboxSearch.waiting_for_query, F.text)
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


@router.message(InboxSearch.waiting_for_query, ~F.text)
async def search_needs_text(message: types.Message) -> None:
    await message.answer(
        "⚠️ Please send your search as a <b>text message</b>.\n\nSend /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("inbox"), StateFilter(None))
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


@router.message(Command("latest"), StateFilter(None))
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

    # Session closed BEFORE rendering/fetching (lease boundary).
    await render_single_message(message, user, messages[0])


@router.callback_query(F.data == "inbox_latest")
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

    # Session closed BEFORE rendering/fetching (lease boundary).
    await render_single_message(callback, user, messages[0])


async def render_search_results(message: types.Message, query: str, results: list) -> None:
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
