import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    NITRIS_BASE_URL = os.getenv("NITRIS_BASE_URL", "https://eapplication.nitrkl.ac.in")
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/collegeclaw")
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

    # Question-paper storage channel — a PRIVATE Telegram channel/supergroup where
    # the bot uploads every unique paper exactly ONCE. All student deliveries are
    # forwards of the cached file_id from this channel (zero re-upload, zero NITRIS
    # traffic on cache hit). The bot must be an admin in this chat.
    # If unset (0), automatically falls back to direct user chat for storage.
    _raw_qp_chat = os.getenv("QP_STORAGE_CHAT_ID", "0").strip()
    try:
        QP_STORAGE_CHAT_ID = int(_raw_qp_chat) if _raw_qp_chat else 0
    except ValueError:
        QP_STORAGE_CHAT_ID = 0

config = Config()
