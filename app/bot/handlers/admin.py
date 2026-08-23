"""Admin-only commands: system status and QP cache reset."""

import logging
import asyncio
import html

from aiogram import Router, types
from aiogram.filters import Command, StateFilter
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramAPIError
from sqlalchemy import select

from app.config import config
from app.db.database import get_db_session
from app.db.models import User
from app.utils import esc

logger = logging.getLogger(__name__)

router = Router(name="admin_router")


def is_admin(user_id: int) -> bool:
    """Check if a Telegram user ID is in the admin list."""
    return user_id in config.ADMIN_TELEGRAM_IDS


@router.message(Command("status"), StateFilter("*"))
async def cmd_status(message: types.Message):
    """Admin command: show system health and NITRIS gateway metrics."""
    if not is_admin(message.from_user.id):
        return

    from app.nitris.gateway import nitris_gateway
    from app.nitris.job_queue import nitris_job_queue
    from app.nitris.rate_limiter import operation_cooldown
    from sqlalchemy import text as sql_text

    gw = nitris_gateway.get_metrics()
    q_stats = nitris_job_queue.get_stats()
    queue_depth = q_stats.get("queue_depth", 0)
    interactive_depth = q_stats.get("interactive_queue_depth", 0)
    bg_depth = q_stats.get("background_queue_depth", 0)
    dedup_count = q_stats.get("active_dedup_count", 0)
    cooldown_stats = operation_cooldown.get_stats()

    try:
        from app.observability import metrics as obs_metrics
        obs_data = await obs_metrics.snapshot()
        gw_lat = obs_data.get("gateway_latency", {})
        gw_lat_str = f"{gw_lat.get('avg_ms', 0)}ms (p95: {gw_lat.get('p95_ms', 0)}ms)" if gw_lat.get("count", 0) > 0 else "n/a"
    except Exception:
        gw_lat_str = "n/a"

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
        f"  Latency (avg/p95): {gw_lat_str}\n"
        f"  Errors: {gw.get('consecutive_errors', 0)} consecutive, {gw.get('total_errors', 0)} total\n"
        f"  Total requests: {gw.get('total_requests', 0)} (logins: {gw.get('total_logins', 0)})\n"
        f"  Last error: <code>{esc(str(gw.get('last_error') or 'none'))}</code>\n\n"
        f"📋 <b>Job Queue</b>\n"
        f"  Total Pending: {queue_depth} (Interactive: {interactive_depth}, Background: {bg_depth})\n"
        f"  Workers: {q_stats.get('interactive_workers', '?')} interactive, {q_stats.get('background_workers', '?')} background\n"
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


@router.message(Command("admin_reset_qp"), StateFilter("*"))
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
                            pending_file_id = NULL,
                            not_available_until = NULL
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


# --- Broadcast ---

BROADCAST_MAX_RETRIES = config.BROADCAST_MAX_RETRIES        # per-user FloodWait/transient retries
BROADCAST_PACING_SECONDS = config.BROADCAST_PACING_SECONDS  # ~20 msg/s, under Telegram's ~30/s global limit
BROADCAST_MAX_LEN = 4000        # Telegram hard limit is 4096; leave headroom (protocol constant)
BROADCAST_PROGRESS_EVERY = config.BROADCAST_PROGRESS_EVERY  # edit status message every N sends


async def _send_broadcast_one(bot, telegram_id: int, text: str, pin: bool = False) -> str:
    """Send one plain-text broadcast message, optionally pinning it in the user's chat.

    Returns a status string:
      'ok'         — sent (and pinned, when requested)
      'pin_failed' — sent, but the pin call errored (message still delivered)
      'blocked'    — user blocked the bot
      'inactive'   — chat not found / deactivated
      'failed'     — send failed after retries

    Pinning works in private 1-on-1 chats WITHOUT admin rights (the "must be
    admin" rule only applies to groups/channels), so a pin failure is rare and
    is never treated as a delivery failure.
    """
    for attempt in range(BROADCAST_MAX_RETRIES):
        try:
            sent = await bot.send_message(chat_id=telegram_id, text=text)
            if pin:
                try:
                    await bot.pin_chat_message(
                        chat_id=telegram_id,
                        message_id=sent.message_id,
                        disable_notification=True,
                    )
                    # Track it so /unpin can target exactly this message.
                    _last_pinned_by_chat[telegram_id] = sent.message_id
                except TelegramAPIError as e:
                    logger.warning("Broadcast pin failed for %d: %r", telegram_id, e)
                    return "pin_failed"
                except Exception as e:
                    logger.warning("Broadcast pin failed (unexpected) for %d: %r", telegram_id, e)
                    return "pin_failed"
            return "ok"
        except TelegramRetryAfter as e:
            if attempt + 1 >= BROADCAST_MAX_RETRIES:
                logger.warning("Broadcast floodwait exhausted for %d", telegram_id)
                return "failed"
            logger.warning("Broadcast FloodWait to %d: %ds — backing off", telegram_id, e.retry_after)
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramAPIError as e:
            msg = str(e).lower()
            if "chat not found" in msg or "deactivated" in msg:
                return "inactive"
            if attempt + 1 >= BROADCAST_MAX_RETRIES:
                return "failed"
            await asyncio.sleep(1.0 * (attempt + 1))
    return "failed"


async def _run_broadcast(
    bot, telegram_ids: list[int], text: str,
    status_chat_id: int, status_message_id: int,
    pin: bool = False,
) -> None:
    """Background worker: fan out the broadcast and report a final summary.

    Runs sequentially (no event-loop flooding), closes over no DB session,
    and never raises — so a failure in one send can't kill the whole run.
    """
    sent = blocked = inactive = failed = pin_failed = 0
    total = len(telegram_ids)

    for idx, tid in enumerate(telegram_ids, start=1):
        status = await _send_broadcast_one(bot, tid, text, pin=pin)
        if status == "ok":
            sent += 1
        elif status == "pin_failed":
            sent += 1
            pin_failed += 1
        elif status == "blocked":
            blocked += 1
        elif status == "inactive":
            inactive += 1
        else:
            failed += 1

        if idx % BROADCAST_PROGRESS_EVERY == 0:
            try:
                await bot.edit_message_text(
                    chat_id=status_chat_id,
                    message_id=status_message_id,
                    text=(
                        f"📣 <b>Broadcasting…</b> {idx}/{total}\n"
                        f"✅ {sent} · 🚫 {blocked} · 👤 {inactive} · ❌ {failed}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        # Gentle pacing under Telegram's global ~30 msg/s bot limit.
        await asyncio.sleep(BROADCAST_PACING_SECONDS)

    summary_lines = [
        f"✅ <b>Broadcast complete</b>",
        "",
        f"📨 Delivered: <b>{sent}</b>/{total}",
    ]
    if pin:
        summary_lines.append(f"📌 Pinned: <b>{sent - pin_failed}</b>")
        if pin_failed:
            summary_lines.append(f"⚠️ Delivered but not pinned: <b>{pin_failed}</b>")
    summary_lines += [
        f"🚫 Blocked the bot: <b>{blocked}</b>",
        f"👤 Inactive/deleted: <b>{inactive}</b>",
        f"❌ Failed: <b>{failed}</b>",
    ]
    summary = "\n".join(summary_lines)

    try:
        await bot.edit_message_text(
            chat_id=status_chat_id, message_id=status_message_id,
            text=summary, parse_mode=ParseMode.HTML,
        )
    except Exception:
        try:
            await bot.send_message(status_chat_id, summary, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Failed to deliver broadcast summary to admin %d: %r", status_chat_id, e)


async def _broadcast_common(message: types.Message, pin: bool, command_name: str) -> None:
    """Shared logic for /broadcast and /broadcastpin (admin-gated)."""
    if not is_admin(message.from_user.id):
        return

    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        pin_desc = " and pins it in their chat" if pin else ""
        await message.answer(
            f"Usage: <code>/{command_name} &lt;message text&gt;</code>\n\n"
            f"Sends the message to <b>ALL</b> registered users{pin_desc} as plain text.\n"
            "Runs in the background and reports a summary when done.",
            parse_mode=ParseMode.HTML,
        )
        return

    text = args[1].strip()
    if len(text) > BROADCAST_MAX_LEN:
        text = text[:BROADCAST_MAX_LEN]

    # Fetch IDs in a SHORT DB session, then close it before any network send.
    async with get_db_session() as session:
        rows = (await session.execute(select(User.telegram_id))).scalars().all()
    telegram_ids = list(rows)

    if not telegram_ids:
        await message.answer("⚠️ No registered users to broadcast to.")
        return

    action = "Broadcast + Pin" if pin else "Broadcast"
    status_msg = await message.answer(
        f"📣 <b>{action} started</b>\n\n"
        f"👥 Target: <b>{len(telegram_ids)}</b> users\n"
        f"⏳ Running in background — this message updates with progress.",
        parse_mode=ParseMode.HTML,
    )

    from app.utils import spawn_tracked
    spawn_tracked(
        _run_broadcast(
            message.bot, telegram_ids, text,
            status_msg.chat.id, status_msg.message_id,
            pin=pin,
        ),
        name=f"broadcast-{len(telegram_ids)}-users",
    )


@router.message(Command("broadcast"), StateFilter("*"))
async def cmd_broadcast(message: types.Message):
    """Admin command: send a plain-text message to all registered users."""
    await _broadcast_common(message, pin=False, command_name="broadcast")


@router.message(Command("broadcastpin"), StateFilter("*"))
async def cmd_broadcastpin(message: types.Message):
    """Admin command: send + pin a plain-text message in every user's chat."""
    await _broadcast_common(message, pin=True, command_name="broadcastpin")


# ── /unpin — remove the last pinned broadcast from every user's chat ────────

# Chat → message_id of the most recent broadcast WE pinned in that chat.
# Lets /unpin target exactly our own pinned message; falls back to Telegram's
# "unpin most recent" when the map is cold (e.g. after a restart).
_last_pinned_by_chat: dict[int, int] = {}


async def _unpin_one(bot, telegram_id: int) -> str:
    """Unpin the last broadcast-pinned message for one user.

    Returns a status string:
      'ok'       — unpinned successfully
      'no_pin'   — nothing pinned in that chat
      'blocked'  — user blocked the bot
      'inactive' — chat not found / deactivated
      'failed'   — unpin failed after retries
    """
    for attempt in range(BROADCAST_MAX_RETRIES):
        try:
            recorded = _last_pinned_by_chat.get(telegram_id)
            if recorded is not None:
                await bot.unpin_chat_message(chat_id=telegram_id, message_id=recorded)
                _last_pinned_by_chat.pop(telegram_id, None)
            else:
                # Cold map (restart): unpin the MOST RECENT pinned message.
                await bot.unpin_chat_message(chat_id=telegram_id)
            return "ok"
        except TelegramRetryAfter as e:
            if attempt + 1 >= BROADCAST_MAX_RETRIES:
                logger.warning("Unpin floodwait exhausted for %d", telegram_id)
                return "failed"
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramAPIError as e:
            msg = str(e).lower()
            if "message to unpin" in msg and "not found" in msg:
                return "no_pin"
            if "chat not found" in msg or "deactivated" in msg:
                return "inactive"
            if attempt + 1 >= BROADCAST_MAX_RETRIES:
                logger.warning("Unpin failed for %d: %r", telegram_id, e)
                return "failed"
            await asyncio.sleep(1.0 * (attempt + 1))
    return "failed"


async def _run_unpin_all(
    bot, telegram_ids: list[int],
    status_chat_id: int, status_message_id: int,
) -> None:
    """Background worker: unpin across all registered users and report."""
    counts = {"ok": 0, "no_pin": 0, "blocked": 0, "inactive": 0, "failed": 0}
    total = len(telegram_ids)

    for idx, tid in enumerate(telegram_ids, start=1):
        status = await _unpin_one(bot, tid)
        counts[status] += 1

        if idx % BROADCAST_PROGRESS_EVERY == 0:
            try:
                await bot.edit_message_text(
                    chat_id=status_chat_id,
                    message_id=status_message_id,
                    text=(
                        f"📌 <b>Unpinning…</b> {idx}/{total}\n"
                        f"✅ {counts['ok']} · ⚪ {counts['no_pin']} · "
                        f"🚫 {counts['blocked']} · 👤 {counts['inactive']} · ❌ {counts['failed']}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        await asyncio.sleep(BROADCAST_PACING_SECONDS)

    summary = (
        f"📌 <b>Unpin complete</b>\n\n"
        f"🎯 Target: <b>{total}</b> users\n"
        f"✅ Unpinned: <b>{counts['ok']}</b>\n"
        f"⚪ Nothing pinned: <b>{counts['no_pin']}</b>\n"
        f"🚫 Blocked the bot: <b>{counts['blocked']}</b>\n"
        f"👤 Inactive/deleted: <b>{counts['inactive']}</b>\n"
        f"❌ Failed: <b>{counts['failed']}</b>"
    )
    try:
        await bot.edit_message_text(
            chat_id=status_chat_id, message_id=status_message_id,
            text=summary, parse_mode=ParseMode.HTML,
        )
    except Exception:
        try:
            await bot.send_message(status_chat_id, summary, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Failed to deliver unpin summary to admin %d: %r", status_chat_id, e)


@router.message(Command("unpin"), StateFilter("*"))
async def cmd_unpin(message: types.Message):
    """Admin command: remove the last pinned broadcast from every user's chat."""
    if not is_admin(message.from_user.id):
        return

    # Fetch IDs in a SHORT DB session, then close before any network work.
    async with get_db_session() as session:
        rows = (await session.execute(select(User.telegram_id))).scalars().all()
    telegram_ids = list(rows)

    if not telegram_ids:
        await message.answer("⚠️ No registered users to unpin for.")
        return

    status_msg = await message.answer(
        f"📌 <b>Unpin started</b>\n\n"
        f"👥 Target: <b>{len(telegram_ids)}</b> users\n"
        f"⏳ Running in background — this message updates with progress.",
        parse_mode=ParseMode.HTML,
    )

    from app.utils import spawn_tracked
    spawn_tracked(
        _run_unpin_all(
            message.bot, telegram_ids,
            status_msg.chat.id, status_msg.message_id,
        ),
        name=f"unpin-{len(telegram_ids)}-users",
    )
