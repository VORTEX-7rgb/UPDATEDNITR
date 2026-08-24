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
    IST: ZoneInfo = IST
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    NITRIS_BASE_URL = os.getenv("NITRIS_BASE_URL", "https://eapplication.nitrkl.ac.in")
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/collegeclaw")
    # Postgres pool sizing (per process). Defaults preserve the historical
    # 10+20. Launch tuning: 20+30 (~50 conns) leaves headroom under PG's
    # default max_connections=100.
    DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
    DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
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
    # PERF: token-bucket burst capacity for logins — up to this many
    # back-to-back logins when the bucket is full; refill rate stays
    # 1/NITRIS_GATEWAY_MIN_LOGIN_INTERVAL per second (same average-rate
    # portal protection). Raise interval/burst ONLY after measuring the
    # portal's real tolerance; the circuit breaker backstops overshoots.
    NITRIS_LOGIN_BURST = int(os.getenv("NITRIS_LOGIN_BURST", "3"))
    NITRIS_GATEWAY_CIRCUIT_ERROR_THRESHOLD = int(os.getenv("NITRIS_GATEWAY_CIRCUIT_ERROR_THRESHOLD", "10"))
    NITRIS_GATEWAY_CIRCUIT_RECOVERY_SECONDS = float(os.getenv("NITRIS_GATEWAY_CIRCUIT_RECOVERY_SECONDS", "60"))

    # ── NITRIS Job Queue config (Phase 2) ───────────────────────────────────
    # Total worker coroutines pulling from the priority queue.
    # Split into INTERACTIVE + BACKGROUND lanes:
    #   - Interactive workers drain HIGH-priority jobs only (user button taps)
    #   - Background workers drain MEDIUM/LOW only (periodic syncs)
    # HIGH-priority user taps never wait behind long background syncs.
    NITRIS_JOB_WORKERS = int(os.getenv("NITRIS_JOB_WORKERS", "15"))
    NITRIS_INTERACTIVE_WORKERS = int(os.getenv("NITRIS_INTERACTIVE_WORKERS", "4"))
    # Derived: background workers = max(1, TOTAL - INTERACTIVE)

    # Hard queue bound — enqueue() rejects when queue exceeds this. Prevents
    # unbounded memory growth if NITRIS is down and jobs pile up.
    NITRIS_JOB_QUEUE_MAX_DEPTH = int(os.getenv("NITRIS_JOB_QUEUE_MAX_DEPTH", "200"))

    # Inbox detail fetch concurrency (Phase 4). Bounded to avoid tripping the
    # circuit breaker. 5 simultaneous detail fetches per inbox sync.
    INBOX_DETAIL_FETCH_CONCURRENCY = int(os.getenv("INBOX_DETAIL_FETCH_CONCURRENCY", "5"))

    # Cap on detail-page fetches per inbox sync: only the NEWEST N missing
    # messages get their bodies during a sync. Older messages are persisted
    # header-only (body=None + stale body_fetched_at) and lazily fetched on
    # first open via the cache-first inbox path. Bounds first-sync cost
    # (a 70-notice backlog → 15 detail fetches).
    INBOX_SYNC_DETAIL_LIMIT = int(os.getenv("INBOX_SYNC_DETAIL_LIMIT", "15"))

    # Reduced from 6h → 30min. With parallel detail fetches,
    # re-fetching bodies is cheap. Fresher data > fewer requests.
    INBOX_BODY_TTL_SECONDS = int(os.getenv("INBOX_BODY_TTL_SECONDS", str(30 * 60)))

    # ── Phase 6: Admission control ──────────────────────────────────────────
    # Max concurrent user-initiated registrations (each does a NITRIS login +
    # attendance fetch + DB writes). Without this cap, a registration spike
    # (e.g. campus launch) can saturate the gateway and starve existing users'
    # interactive /attendance taps.
    REGISTRATION_MAX_CONCURRENT = int(os.getenv("REGISTRATION_MAX_CONCURRENT", "4"))

    # Max concurrent QP metadata fetches (each does a NITRIS login). Without
    # this cap, a /papers batch download could starve interactive taps.
    QP_METADATA_MAX_CONCURRENT = int(os.getenv("QP_METADATA_MAX_CONCURRENT", "3"))

    # Job-level retry config (Phase 6.4). When a job fails with a transient
    # error, the queue re-enqueues it with exponential backoff up to this
    # many attempts before giving up.
    JOB_MAX_RETRIES = int(os.getenv("JOB_MAX_RETRIES", "3"))
    JOB_RETRY_BASE_DELAY = float(os.getenv("JOB_RETRY_BASE_DELAY", "2.0"))

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

    # ── Per-user operation cooldowns (rate_limiter) ─────────────────────────
    # Minimum seconds between repeated user-triggered operations. These are
    # UX/abuse guards, not portal protection — the gateway handles that.
    COOLDOWN_ATTENDANCE_REFRESH = int(os.getenv("COOLDOWN_ATTENDANCE_REFRESH", "60"))
    COOLDOWN_INBOX_REFRESH = int(os.getenv("COOLDOWN_INBOX_REFRESH", "60"))
    COOLDOWN_ATTACHMENT_DOWNLOAD = int(os.getenv("COOLDOWN_ATTACHMENT_DOWNLOAD", "10"))
    COOLDOWN_PAPERS_SEARCH = int(os.getenv("COOLDOWN_PAPERS_SEARCH", "10"))
    # Prune expired cooldown entries every N writes — the bounded-memory sweep.
    RATE_LIMITER_PRUNE_EVERY = int(os.getenv("RATE_LIMITER_PRUNE_EVERY", "256"))

    # ── Retention sweeper (snapshots + events) ──────────────────────────────
    # The snapshots table is append-only by design; consumers only ever read
    # the LATEST row per (user, module). This sweeper deletes superseded rows
    # so the table cannot grow unbounded at scale. Same idea for terminal
    # (sent / permanent-failure) events past a grace window.
    # How often the sweeper wakes up.
    RETENTION_INTERVAL_SECONDS = int(os.getenv("RETENTION_INTERVAL_SECONDS", str(6 * 3600)))
    # Newest N snapshots kept per (user_id, module_name). Rank 1..N always survive,
    # so every consumer reading "latest snapshot" is unaffected.
    RETENTION_SNAPSHOT_KEEP = int(os.getenv("RETENTION_SNAPSHOT_KEEP", "10"))
    # Terminal events older than this many days are purged.
    RETENTION_EVENT_DAYS = int(os.getenv("RETENTION_EVENT_DAYS", "14"))
    # Rows deleted per statement/transaction — keeps every txn short so locks
    # are brief and autovacuum keeps up (never one giant DELETE).
    RETENTION_DELETE_BATCH = int(os.getenv("RETENTION_DELETE_BATCH", "5000"))
    # Pause between delete batches — yields to other work and smooths WAL churn.
    RETENTION_BATCH_PAUSE_SECONDS = float(os.getenv("RETENTION_BATCH_PAUSE_SECONDS", "0.5"))

    # ── NITRIS session pool (per-user authenticated clients) ───────────────
    NITRIS_SESSION_TTL_SECONDS = float(os.getenv("NITRIS_SESSION_TTL_SECONDS", "1800"))
    NITRIS_SESSION_POOL_MAX = int(os.getenv("NITRIS_SESSION_POOL_MAX", "256"))

    # ── NITRIS gateway extras ────────────────────────────────────────────────
    # Cooldown after which a quarantined/failed credential may retry login.
    NITRIS_CREDENTIAL_COOLDOWN_SECONDS = int(os.getenv("NITRIS_CREDENTIAL_COOLDOWN_SECONDS", "3600"))
    # Background work leaves this many gateway slots free for interactive taps.
    NITRIS_RESERVED_INTERACTIVE_SLOTS = int(os.getenv("NITRIS_RESERVED_INTERACTIVE_SLOTS", "2"))

    # ── NITRIS client HTTP behavior ──────────────────────────────────────────
    # Per-request timeout applied to every portal call.
    NITRIS_HTTP_TIMEOUT_SECONDS = float(os.getenv("NITRIS_HTTP_TIMEOUT_SECONDS", "30.0"))
    # Resolved module-URL cache lifetime (skips Home.aspx discovery GET).
    # Aligned to the session-pool TTL (1800s): a live session should never
    # outlive its resolved URLs, and fast-path hits refresh the entry on every
    # successful scrape anyway.
    NITRIS_URL_CACHE_TTL_SECONDS = float(os.getenv("NITRIS_URL_CACHE_TTL_SECONDS", "1800"))
    # Year/session probe-hint lifetime (skip redundant dropdown probes).
    NITRIS_PROBE_HINT_TTL_SECONDS = float(os.getenv("NITRIS_PROBE_HINT_TTL_SECONDS", "2700"))

    # ── Event dispatcher ─────────────────────────────────────────────────────
    # Telegram allows ~30 msg/s broadcast limit. With DISPATCH_BATCH_SIZE=600 and
    # 35ms inter-message pacing (~28 msg/s), 600 notifications drain in ~21s,
    # delivering campus-wide notices to 1,000+ students in ~35s flat without
    # hitting 429 FloodWait.
    DISPATCH_BATCH_SIZE = int(os.getenv("DISPATCH_BATCH_SIZE", "600"))
    DISPATCH_CLAIM_STALE_SECONDS = int(os.getenv("DISPATCH_CLAIM_STALE_SECONDS", "300"))
    DISPATCH_MAX_ATTEMPTS = int(os.getenv("DISPATCH_MAX_ATTEMPTS", "5"))
    DISPATCH_INTERVAL_SECONDS = int(os.getenv("DISPATCH_INTERVAL_SECONDS", "5"))
    DISPATCH_REAPER_INTERVAL_SECONDS = int(os.getenv("DISPATCH_REAPER_INTERVAL_SECONDS", "60"))
    DISPATCH_SEND_TIMEOUT_SECONDS = int(os.getenv("DISPATCH_SEND_TIMEOUT_SECONDS", "30"))
    DISPATCH_FLOODWAIT_MAX_RETRIES = int(os.getenv("DISPATCH_FLOODWAIT_MAX_RETRIES", "3"))
    DISPATCH_PACING_SECONDS = float(os.getenv("DISPATCH_PACING_SECONDS", "0.035"))

    # ── Question-paper cache service (mirrors the ATTACHMENT_* block) ───────
    QP_MAX_CONCURRENT_ACQUISITIONS = int(os.getenv("QP_MAX_CONCURRENT_ACQUISITIONS", "8"))
    QP_MAX_CONCURRENT_DELIVERIES = int(os.getenv("QP_MAX_CONCURRENT_DELIVERIES", "25"))
    QP_ACQUIRE_STALE_SECONDS = int(os.getenv("QP_ACQUIRE_STALE_SECONDS", "300"))
    QP_PERMANENT_AFTER = int(os.getenv("QP_PERMANENT_AFTER", "5"))
    QP_WAIT_POLL_INTERVAL_SECONDS = float(os.getenv("QP_WAIT_POLL_INTERVAL_SECONDS", "2.0"))
    QP_WAIT_TIMEOUT_SECONDS = float(os.getenv("QP_WAIT_TIMEOUT_SECONDS", "60.0"))
    QP_FLOODWAIT_MAX_RETRIES = int(os.getenv("QP_FLOODWAIT_MAX_RETRIES", "3"))
    QP_DELIVERY_MAX_RETRIES = int(os.getenv("QP_DELIVERY_MAX_RETRIES", "3"))
    QP_DELIVERY_RETRY_BASE_DELAY = float(os.getenv("QP_DELIVERY_RETRY_BASE_DELAY", "1.0"))

    # ── Broadcast / UX timings ───────────────────────────────────────────────
    BROADCAST_MAX_RETRIES = int(os.getenv("BROADCAST_MAX_RETRIES", "3"))
    BROADCAST_PACING_SECONDS = float(os.getenv("BROADCAST_PACING_SECONDS", "0.05"))
    BROADCAST_PROGRESS_EVERY = int(os.getenv("BROADCAST_PROGRESS_EVERY", "250"))
    ATTENDANCE_SLOW_AFTER_SECONDS = float(os.getenv("ATTENDANCE_SLOW_AFTER_SECONDS", "3.5"))
    COOLDOWN_TIMETABLE_SYNC = int(os.getenv("COOLDOWN_TIMETABLE_SYNC", "60"))

    # ── DB engine housekeeping ───────────────────────────────────────────────
    DB_POOL_DISPOSE_DEBOUNCE_SECONDS = float(os.getenv("DB_POOL_DISPOSE_DEBOUNCE_SECONDS", "60"))

    # ── Pre-warm (admin-driven QP cache filling) ─────────────────────────
    # Max SUBJECTS per /admin_prewarm run (each subject = mid+end papers).
    PREWARM_MAX_ITEMS = int(os.getenv("PREWARM_MAX_ITEMS", "300"))

    # ── Debug mode (Phase 0 security) ───────────────────────────────────────
    # When True, NITRIS HTML snapshots are saved to disk for debugging.
    # MUST be False in production — snapshots contain student PII, and the
    # disk writes are synchronous/blocking inside the event loop.
    # Booting with DEBUG=true requires ALSO setting ALLOW_DEBUG_IN_PROD=1
    # (enforced in app.main) so a stray .env can never ship PII dumps silently.
    DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
    ALLOW_DEBUG_IN_PROD = os.getenv("ALLOW_DEBUG_IN_PROD", "").lower() in ("1", "true", "yes")

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
