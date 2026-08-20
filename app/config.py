import os
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# Load .env but DON'T override existing env vars. This lets test harnesses
# (conftest.py) and CI set DATABASE_URL / ENCRYPTION_KEY without being
# clobbered by a stray .env file found by dotenv's upward directory walk.
load_dotenv(override=False)

# ── IST (Asia/Kolkata) — the single source of truth for all date/time work ──
# India has no DST since 1970, so this ZoneInfo is stable forever. EVERY
# datetime.now() call in this codebase MUST pass this tz:
#     datetime.now(config.IST)
# Bare datetime.now() is forbidden in the timetable module — the AST test
# test_no_naive_datetime_now.py enforces this. Without explicit IST, a UTC-default
# server clock would shift weekday/time by 5h30m and silently affect every
# "now/next" computation.
IST = ZoneInfo("Asia/Kolkata")


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
    # Scaled to safely serve up to 5k+ registered students.
    NITRIS_GATEWAY_MAX_CONCURRENT = int(os.getenv("NITRIS_GATEWAY_MAX_CONCURRENT", "8"))
    NITRIS_GATEWAY_MIN_LOGIN_INTERVAL = float(os.getenv("NITRIS_GATEWAY_MIN_LOGIN_INTERVAL", "1.5"))
    NITRIS_GATEWAY_CIRCUIT_ERROR_THRESHOLD = int(os.getenv("NITRIS_GATEWAY_CIRCUIT_ERROR_THRESHOLD", "10"))
    NITRIS_GATEWAY_CIRCUIT_RECOVERY_SECONDS = float(os.getenv("NITRIS_GATEWAY_CIRCUIT_RECOVERY_SECONDS", "60"))

    # ── NITRIS Job Queue config (Phase 2) ───────────────────────────────────
    # Number of worker coroutines pulling from the priority queue.
    # Each worker goes through the gateway, so effective concurrency is
    # min(NITRIS_JOB_WORKERS, NITRIS_GATEWAY_MAX_CONCURRENT).
    NITRIS_JOB_WORKERS = int(os.getenv("NITRIS_JOB_WORKERS", "6"))

    # ── Per-module TTL scheduler config (Phase 5) ───────────────────────────
    # Authoritative per-module sync intervals (in seconds).
    # Scaled for 5k users:
    # attendance: 12h (updates daily after each class)
    # inbox: 4h (students get Telegram notifications on new notices)
    # timetable: 7d (changes only at semester boundary)
    MODULE_TTL_SECONDS = {
        "attendance": int(os.getenv("MODULE_TTL_ATTENDANCE", str(12 * 3600))),     # 12h
        "inbox": int(os.getenv("MODULE_TTL_INBOX", str(4 * 3600))),                 # 4h
        "timetable": int(os.getenv("MODULE_TTL_TIMETABLE", str(7 * 24 * 3600))),    # 7d
    }

    # Scheduler batch size — how many due jobs to claim per cycle
    SCHEDULER_BATCH_SIZE = int(os.getenv("SCHEDULER_BATCH_SIZE", "25"))

    # Scheduler poll interval (how often to check for due work)
    SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL", "30"))  # 30s

    # Scheduler claim staleness — claimed rows become reclaimable after this
    SCHEDULER_CLAIM_STALE_SECONDS = int(os.getenv("SCHEDULER_CLAIM_STALE_SECONDS", "300"))  # 5min

    # Scheduler queue-depth backpressure — when the job queue has at least this
    # many pending jobs, the scheduler skips claiming new work. Prevents the
    # cold-start / post-downtime thundering herd (all 5k users due at once).
    SCHEDULER_MAX_QUEUE_DEPTH = int(os.getenv("SCHEDULER_MAX_QUEUE_DEPTH", "50"))

    # ── Debug mode (Phase 0 security) ───────────────────────────────────────
    # When True, NITRIS HTML snapshots are saved to disk for debugging.
    # MUST be False in production — snapshots contain student PII.
    DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

    # ── Timetable feature (Phase 6) ────────────────────────────────────────
    # Lookahead window for the "next class" search when there are no more
    # classes today (or on weekends). 7 = the natural weekly cycle.
    TIMETABLE_LOOKAHEAD_DAYS = int(os.getenv("TIMETABLE_LOOKAHEAD_DAYS", "7"))

    # Grace period in minutes — a class ending at 10:00 still shows as "current"
    # up to 10:0X (where X = grace). Prevents the awkward "no class now" blip
    # the second a class ends while the student is packing up.
    TIMETABLE_CLASS_END_GRACE_MIN = int(os.getenv("TIMETABLE_CLASS_END_GRACE_MIN", "2"))

    # Single-flight dedup key prefix for timetable sync jobs.
    TIMETABLE_SYNC_DEDUP_PREFIX = "timetable_sync"

    # ── Phase 7: Inbox Cache-First + Global Attachment Cache config ─────────
    # Max age (seconds) of a cached notice body before render_single_message
    # triggers a background re-fetch. 6 hours balances freshness against
    # NITRIS load — notices rarely change after publication.
    INBOX_BODY_TTL_SECONDS = int(os.getenv("INBOX_BODY_TTL_SECONDS", str(6 * 3600)))

    # Attachment acquisition staleness — a worker's lock lease expires after
    # this many seconds. Mirrors QP_CACHE_STALE_SECONDS.
    ATTACHMENT_CACHE_STALE_SECONDS = int(os.getenv("ATTACHMENT_CACHE_STALE_SECONDS", "300"))  # 5 min

    # Maximum attempts before a failed attachment download transitions to
    # permanent_failure (avoids infinite retries on corrupt files / deleted portal links).
    ATTACHMENT_CACHE_PERMANENT_AFTER = int(os.getenv("ATTACHMENT_CACHE_PERMANENT_AFTER", "5"))

    # Concurrency controls for attachment downloads and deliveries
    ATTACHMENT_MAX_CONCURRENT_ACQUISITIONS = int(os.getenv("ATTACHMENT_MAX_CONCURRENT_ACQUISITIONS", "8"))
    ATTACHMENT_MAX_CONCURRENT_DELIVERIES = int(os.getenv("ATTACHMENT_MAX_CONCURRENT_DELIVERIES", "25"))
    ATTACHMENT_WAIT_POLL_INTERVAL = float(os.getenv("ATTACHMENT_WAIT_POLL_INTERVAL", "2.0"))
    ATTACHMENT_WAIT_TIMEOUT = float(os.getenv("ATTACHMENT_WAIT_TIMEOUT", "60.0"))
    ATTACHMENT_FLOODWAIT_MAX_RETRIES = int(os.getenv("ATTACHMENT_FLOODWAIT_MAX_RETRIES", "3"))
    ATTACHMENT_DELIVERY_MAX_RETRIES = int(os.getenv("ATTACHMENT_DELIVERY_MAX_RETRIES", "3"))
    ATTACHMENT_DELIVERY_RETRY_BASE_DELAY = float(os.getenv("ATTACHMENT_DELIVERY_RETRY_BASE_DELAY", "1.0"))

    # Telegram storage channel for global attachments. Explicitly setting
    # ATTACHMENT_STORAGE_CHAT_ID wins; otherwise falls back to the QP storage
    # channel (shared media channel); otherwise 0 (direct-user fallback uploads).
    _raw_attach_chat = os.getenv("ATTACHMENT_STORAGE_CHAT_ID")
    _raw_qp_chat = os.getenv("QP_STORAGE_CHAT_ID")
    if _raw_attach_chat:
        ATTACHMENT_STORAGE_CHAT_ID = int(_raw_attach_chat)
    elif _raw_qp_chat:
        ATTACHMENT_STORAGE_CHAT_ID = int(_raw_qp_chat)
    else:
        ATTACHMENT_STORAGE_CHAT_ID = 0


config = Config()

