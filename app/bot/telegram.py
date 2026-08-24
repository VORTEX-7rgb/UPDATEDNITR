"""Bot assembly: Dispatcher + router registration + global error handler.

This module is intentionally thin. All feature handlers live in
``app/bot/handlers/*`` (one router per feature) and shared state lives in
``app/bot/fsm.py``, ``app/bot/common.py``, and ``app/bot/qpaper_registry.py``.

The legacy symbols below are re-exported for backward compatibility so existing
importers (``main.py``, ``job_handlers.py``, and tests) keep working unchanged.
"""

import logging

from aiogram import Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

# Re-exported for ``patch("app.bot.telegram.select")`` in tests.
from sqlalchemy import select  # noqa: F401

from app.db.database import is_db_connection_error

from app.bot.handlers.timetable import router as timetable_router
from app.bot.handlers.registration import router as registration_router
from app.bot.handlers.attendance import router as attendance_router
from app.bot.handlers.inbox import router as inbox_router
from app.bot.handlers.papers import router as papers_router
from app.bot.handlers.admin import router as admin_router

logger = logging.getLogger(__name__)


# --- Dispatcher assembly -----------------------------------------------------

dp = Dispatcher()

from app.bot.middlewares import WarmSessionMiddleware
dp.update.outer_middleware(WarmSessionMiddleware())

dp.include_router(timetable_router)
dp.include_router(registration_router)
dp.include_router(attendance_router)
dp.include_router(inbox_router)
dp.include_router(papers_router)
dp.include_router(admin_router)


# --- QPaperService lifecycle -------------------------------------------------

from app.bot.qpaper_registry import init_qpaper_service, shutdown_qpaper_service  # noqa: E402

# --- Global error handler ----------------------------------------------------

_GENERIC_ERROR_TEXT = (
    "🦀 <b>Something broke on our side.</b>\n\n"
    "Your existing data is still safe. Try again in a moment."
)


@dp.errors()
async def db_error_handler(event: ErrorEvent):
    """Global error handler: DB-offline nicety + catch-all so NO update dies silently.

    Previously non-DB exceptions returned False and vanished — users tapped a
    button and got zero feedback while the only trace was an internal log line.
    """
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

    # ── Catch-all: log with full traceback, answer the user, stop propagation.
    logger.error(
        "Unhandled handler error on update_id=%s: %r",
        getattr(event.update, "update_id", "?"),
        exception,
        exc_info=exception,
    )
    update = event.update
    try:
        if update.message:
            await update.message.answer(_GENERIC_ERROR_TEXT, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            try:
                await update.callback_query.answer(
                    "🦀 Something went wrong. Please try again.", show_alert=True
                )
            except Exception:
                await update.callback_query.message.answer(
                    _GENERIC_ERROR_TEXT, parse_mode=ParseMode.HTML
                )
    except Exception as send_err:
        logger.error("Failed to deliver generic error notice: %r", send_err)
    return True


# --- Backward-compatibility re-exports --------------------------------------
# Keep these importable from ``app.bot.telegram`` so existing callers and tests
# don't need to change.

from app.bot.fsm import (  # noqa: E402,F401
    Registration,
    Deregistration,
    InboxSearch,
    QuestionPaperFlow,
)

from app.bot.common import (  # noqa: E402,F401
    format_attendance_message,
    format_attendance_message_from_snapshot,
    format_dashboard_text,
    get_dashboard_keyboard,
)

from app.bot.handlers.registration import process_password  # noqa: E402,F401
from app.bot.handlers.inbox import render_single_message, _render_inbox_list_text  # noqa: E402,F401
from app.bot.handlers.admin import is_admin  # noqa: E402,F401
