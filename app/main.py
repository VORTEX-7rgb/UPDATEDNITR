import asyncio
import sys
import logging
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from app.config import config
from app.bot.telegram import dp
from app.workers.sync_worker import run_sync_worker, run_dispatch_worker

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
    
    # Start background workers
    sync_worker_task = asyncio.create_task(run_sync_worker(bot))
    dispatch_worker_task = asyncio.create_task(run_dispatch_worker(bot))
    
    try:
        # Start polling
        await dp.start_polling(bot, polling_timeout=10)
    finally:
        # Cancel background worker tasks cleanly
        logging.info("Stopping background workers...")
        sync_worker_task.cancel()
        dispatch_worker_task.cancel()
        try:
            await asyncio.gather(sync_worker_task, dispatch_worker_task, return_exceptions=True)
        except Exception:
            pass
        logging.info("Background workers stopped successfully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")

