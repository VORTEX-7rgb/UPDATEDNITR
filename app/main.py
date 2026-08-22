import asyncio
import sys
import logging
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from app.config import config
from app.bot.telegram import dp, init_qpaper_service, shutdown_qpaper_service
from app.services.attachment_service import init_attachment_service, shutdown_attachment_service
from app.db.database import async_session_factory
from app.workers.sync_worker import (
    run_dispatch_worker,
    init_event_dispatcher,
    shutdown_event_dispatcher,
)
from app.nitris.gateway import nitris_gateway
from app.nitris.job_queue import nitris_job_queue
from app.nitris.job_handlers import init_job_handlers
from app.nitris.auth_gate import init_auth_gate, init_quarantine
from app.services.scheduler_service import run_scheduler_loop, init_scheduler

# Windows Asyncio Event Loop Fix for WinError 10054 / 121 / 64
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

async def main():
    if not config.BOT_TOKEN:
        logging.error("BOT_TOKEN is not set in .env")
        return

    session = AiohttpSession(timeout=300)
    bot = Bot(
        token=config.BOT_TOKEN, 
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    logging.info("Starting Telegram Bot...")

    # ── Credential quarantine gate (auth_gate) ──────────────────────────
    # Must be initialized before any handler can send notifications, and the
    # in-memory gateway guard must be seeded before any login can run.
    init_auth_gate(bot)
    await init_quarantine(async_session_factory)

    # ── Phase 1+2: Initialize NITRIS Gateway + Job Queue ────────────────
    # Register job handlers (attendance_refresh, inbox_refresh, qp_metadata_fetch, etc.)
    init_job_handlers(bot)
    
    # Phase 5: Register background sync handlers (sync_attendance, sync_inbox)
    await init_scheduler()
    
    # Start the job queue workers (workers go through the gateway)
    await nitris_job_queue.start(bot)
    
    # ── Services ────────────────────────────────────────────────────────
    # Initialize Question Paper service & start background stale-lock reaper
    await init_qpaper_service(bot)

    # Initialize Global Attachment service & start background stale-lock reaper
    init_attachment_service(bot, async_session_factory)

    # Initialize Event Dispatcher service & start background stale-claim reaper
    await init_event_dispatcher(bot)

    # ── Phase 5: Start the durable scheduler (replaces run_sync_worker) ──
    scheduler_task = asyncio.create_task(run_scheduler_loop(bot))
    
    # Start the event dispatch worker
    dispatch_worker_task = asyncio.create_task(run_dispatch_worker(bot))
    
    gw_metrics = nitris_gateway.get_metrics()
    logging.info(
        "NITRIS Gateway: max_concurrent=%d, login_interval=%.1fs, circuit_threshold=%d",
        gw_metrics["configured_max_concurrent"],
        gw_metrics["configured_login_interval"],
        gw_metrics["circuit_threshold"],
    )
    logging.info(
        "NITRIS Job Queue: %d workers, handlers=%s",
        config.NITRIS_JOB_WORKERS,
        nitris_job_queue.get_registered_handlers(),
    )
    logging.info(
        "Module TTLs: %s",
        {k: f"{v}s" for k, v in config.MODULE_TTL_SECONDS.items()},
    )
    
    try:
        await dp.start_polling(bot, polling_timeout=10)
    finally:
        logging.info("Stopping background workers & services...")
        await nitris_job_queue.stop()
        await shutdown_qpaper_service()
        await shutdown_attachment_service()
        await shutdown_event_dispatcher()
        
        scheduler_task.cancel()
        dispatch_worker_task.cancel()
        try:
            await asyncio.gather(scheduler_task, dispatch_worker_task, return_exceptions=True)
        except Exception:
            pass
        
        # Clean shutdown: release the DB pool, the shared NITRIS HTTP
        # transport and the Telegram HTTP session so no connection outlives
        # the event loop (stops 'Event loop is closed' errors on Ctrl+C).
        try:
            from app.db.database import engine
            await engine.dispose()
            logging.info("Database engine disposed.")
        except Exception as e:
            logging.warning("Engine dispose failed: %r", e)
        try:
            from app.nitris.session_pool import drop_all_sessions
            dropped = await drop_all_sessions()
            logging.info("Session pool drained (%d client(s)).", dropped)
        except Exception as e:
            logging.warning("Session pool drain failed: %r", e)
        try:
            from app.nitris.client import close_shared_transport
            await close_shared_transport()
            logging.info("Shared NITRIS transport closed.")
        except Exception as e:
            logging.warning("Shared transport close failed: %r", e)
        try:
            await bot.session.close()
            logging.info("Telegram session closed.")
        except Exception as e:
            logging.warning("Bot session close failed: %r", e)
        
        logging.info("Background workers stopped successfully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
