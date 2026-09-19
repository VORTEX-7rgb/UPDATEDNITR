"""Harvest existing question papers and notice attachments from the Telegram storage channel.

Scans all messages in the storage channel, reads their captions and documents,
and updates PostgreSQL with the new bot's valid telegram_file_id.
Zero NITRIS requests! Restores thousands of papers in ~2-3 minutes.
"""
import asyncio
import re
import sys
import logging
import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STORAGE_CHAT_ID = int(os.getenv("QP_STORAGE_CHAT_ID", "0"))
DB_URL = os.getenv("DATABASE_URL", "")
if DB_URL.startswith("postgresql+asyncpg://"):
    DB_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)

# Caption formats:
# QP: "📚 CS2011 | 2024-25/Autumn | mid_sem"
# Attachment: "📎 <b>Attachment:</b> ..." or "📎 Attachment: ..."
QP_CAPTION_RE = re.compile(r"📚\s*([A-Za-z0-9]+)\s*\|\s*([^|]+)\s*\|\s*([a-z_]+)")
ATTACHMENT_CAPTION_RE = re.compile(r"📎\s*(?:<b>)?Attachment:?(?:</b>)?\s*(.*)", re.IGNORECASE)

async def harvest():
    if not BOT_TOKEN or not STORAGE_CHAT_ID or not DB_URL:
        logger.error("Missing BOT_TOKEN, QP_STORAGE_CHAT_ID, or DATABASE_URL environment variables.")
        sys.exit(1)
    bot = Bot(token=BOT_TOKEN)
    conn = await asyncpg.connect(DB_URL)
    
    logger.info("Starting channel cache harvest from %d...", STORAGE_CHAT_ID)
    
    # 1. First find the upper bound message_id
    max_id = 2400
    logger.info("Scanning range 1..%d", max_id)
    
    qp_updated = 0
    att_updated = 0
    skipped = 0
    errors = 0
    
    # Batch processing with rate limiting (~20 msgs/s to avoid 429)
    for mid in range(1, max_id + 1):
        try:
            fwd = await bot.forward_message(
                chat_id=STORAGE_CHAT_ID,
                from_chat_id=STORAGE_CHAT_ID,
                message_id=mid,
                disable_notification=True,
            )
        except TelegramRetryAfter as e:
            logger.warning("FloodWait: sleeping %ds", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            continue
        except TelegramBadRequest as e:
            # Message deleted or empty slot
            skipped += 1
            continue
        except Exception as e:
            errors += 1
            continue
            
        caption = fwd.caption or ""
        doc = fwd.document
        new_msg_id = fwd.message_id
        
        # Always delete the temporary forwarded message
        try:
            await bot.delete_message(chat_id=STORAGE_CHAT_ID, message_id=new_msg_id)
        except Exception:
            pass
            
        if not doc:
            continue
            
        new_file_id = doc.file_id
        file_size = doc.file_size or 0
        file_name = doc.file_name or ""
        kind = "zip" if file_name.lower().endswith(".zip") else "pdf"
        
        # Check QP
        qp_match = QP_CAPTION_RE.search(caption)
        if qp_match:
            sub_code, ac_year, ex_type = [g.strip() for g in qp_match.groups()]
            res = await conn.execute("""
                UPDATE question_paper_caches
                SET telegram_file_id = $1,
                    status = 'paper_available',
                    file_kind = $2,
                    file_size_bytes = $3,
                    updated_at = NOW()
                WHERE subject_code = $4 AND academic_year = $5 AND exam_type = $6
            """, new_file_id, kind, file_size, sub_code, ac_year, ex_type)
            
            if res.endswith("1"):
                qp_updated += 1
                if qp_updated % 50 == 0:
                    logger.info("Harvested %d QPs so far... (latest: %s %s %s)", qp_updated, sub_code, ac_year, ex_type)
            continue
            
        # Check filename fallback for QP: e.g. CS2011_2024-25_Autumn_mid_sem.pdf
        fn_match = re.match(r"([A-Za-z0-9]+)_([0-9]{4}-[0-9]{2}_[A-Za-z]+)_([a-z_]+)\.(pdf|zip)", file_name)
        if fn_match:
            sub_code, safe_year, ex_type, _ = fn_match.groups()
            ac_year = safe_year.replace("_", "/")
            res = await conn.execute("""
                UPDATE question_paper_caches
                SET telegram_file_id = $1,
                    status = 'paper_available',
                    file_kind = $2,
                    file_size_bytes = $3,
                    updated_at = NOW()
                WHERE subject_code = $4 AND academic_year = $5 AND exam_type = $6
            """, new_file_id, kind, file_size, sub_code, ac_year, ex_type)
            if res.endswith("1"):
                qp_updated += 1
                continue

        # Small pacing to stay well within Telegram broadcast limits
        await asyncio.sleep(0.04)

    logger.info("HARVEST COMPLETE! Successfully updated %d Question Papers and %d Attachments.", qp_updated, att_updated)
    await conn.close()
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(harvest())
