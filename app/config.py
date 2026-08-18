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

    # ── Admin Telegram IDs (comma-separated) ────────────────────────────────
    # These users can run /status, /admin_reset_qp, and other admin commands.
    # Example: ADMIN_TELEGRAM_IDS=123456789,987654321
    _raw_admin_ids = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
    ADMIN_TELEGRAM_IDS = frozenset(
        int(x.strip()) for x in _raw_admin_ids.split(",") if x.strip().isdigit()
    )

    # ── NITRIS Gateway config (Phase 1) ─────────────────────────────────────
    # Conservative FIXED capacity. Adapts DOWNWARD only on failures (never up).
    # These defaults are safe for a single bot process serving up to ~5k users.
    NITRIS_GATEWAY_MAX_CONCURRENT = int(os.getenv("NITRIS_GATEWAY_MAX_CONCURRENT", "5"))
    NITRIS_GATEWAY_MIN_LOGIN_INTERVAL = float(os.getenv("NITRIS_GATEWAY_MIN_LOGIN_INTERVAL", "1.5"))
    NITRIS_GATEWAY_CIRCUIT_ERROR_THRESHOLD = int(os.getenv("NITRIS_GATEWAY_CIRCUIT_ERROR_THRESHOLD", "10"))
    NITRIS_GATEWAY_CIRCUIT_RECOVERY_SECONDS = float(os.getenv("NITRIS_GATEWAY_CIRCUIT_RECOVERY_SECONDS", "60"))

    # ── NITRIS Job Queue config (Phase 2) ───────────────────────────────────
    # Number of worker coroutines pulling from the priority queue.
    # Each worker goes through the gateway, so effective concurrency is
    # min(NITRIS_JOB_WORKERS, NITRIS_GATEWAY_MAX_CONCURRENT).
    NITRIS_JOB_WORKERS = int(os.getenv("NITRIS_JOB_WORKERS", "3"))

    # ── Per-module TTL scheduler config (Phase 5) ───────────────────────────
    # Authoritative per-module sync intervals (in seconds).
    # DO NOT hardcode these elsewhere — import from config.
    # attendance: 6h (updates daily after each class)
    # inbox: 15min (near-real-time)
    # timetable: 7d (changes only at semester boundary)
    # question_papers: no periodic background sync (cached forever via Telegram file_id)
    MODULE_TTL_SECONDS = {
        "attendance": int(os.getenv("MODULE_TTL_ATTENDANCE", str(6 * 3600))),      # 6h
        "inbox": int(os.getenv("MODULE_TTL_INBOX", str(15 * 60))),                  # 15min
        "timetable": int(os.getenv("MODULE_TTL_TIMETABLE", str(7 * 24 * 3600))),    # 7d
    }

    # Scheduler batch size — how many due jobs to claim per cycle
    SCHEDULER_BATCH_SIZE = int(os.getenv("SCHEDULER_BATCH_SIZE", "25"))

    # Scheduler poll interval (how often to check for due work)
    SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL", "30"))  # 30s

    # Scheduler claim staleness — claimed rows become reclaimable after this
    SCHEDULER_CLAIM_STALE_SECONDS = int(os.getenv("SCHEDULER_CLAIM_STALE_SECONDS", "300"))  # 5min

    # ── Debug mode (Phase 0 security) ───────────────────────────────────────
    # When True, NITRIS HTML snapshots are saved to disk for debugging.
    # MUST be False in production — snapshots contain student PII.
    DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

config = Config()
