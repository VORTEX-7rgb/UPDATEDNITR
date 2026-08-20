"""Admin-only commands: system status and QP cache reset."""

import logging
import html

from aiogram import Router, types
from aiogram.filters import Command, StateFilter
from aiogram.enums import ParseMode

from app.config import config
from app.db.database import get_db_session
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
