"""inbox cache TTL + global attachment cache

Revision ID: 0007_inbox_cache_and_attachments
Revises: 0006_add_timetable
Create Date: 2026-08-20 12:00:00.000000

ARCHITECTURE
============
Mirrors the proven QuestionPaperCache pattern (migration 0002 + 0004) but for
inbox message bodies and inbox attachments.

Adds:
  1. New table `attachment_caches` — GLOBAL content store. One row per unique
     attachment_path (the normalized URL path; query-string tokens stripped so
     the same file referenced by many students maps to one row).
     - State machine: fetch_in_progress | available | not_available |
                      retryable_failure | permanent_failure
     - Atomic CAS columns (acquired_by, acquired_at, lease_expires_at)
     - Telegram file_id stored here, reused across ALL students.
     - Stale-lock reaper (60s loop) reaps rows stuck in fetch_in_progress
       for > 2× stale window.

  2. Alters `inbox_messages`:
     - ADD body_fetched_at TIMESTAMPTZ — staleness tracking for lazy body fetch
     - ADD attachment_cache_id BIGINT REFERENCES attachment_caches(id)
         ON DELETE SET NULL — links per-user inbox metadata to global content

The existing `inbox_messages.telegram_file_id` column is PRESERVED in this
migration (backwards-compatible). Phase 6 of the refactor will drop it after
all per-user file_ids have been backfilled into the global cache.

IDEMPOTENT: uses IF NOT EXISTS pattern for ALTER TABLE ADD COLUMN.

REVERSIBLE: downgrade drops the new table + new columns. Existing per-user
telegram_file_id is untouched (it was preserved).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_inbox_cache_and_attachments"
down_revision: Union[str, None] = "0006_add_timetable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. New table: attachment_caches (GLOBAL — one row per unique attachment_path) ──
    op.execute("""
        CREATE TABLE IF NOT EXISTS attachment_caches (
            id BIGSERIAL PRIMARY KEY,
            -- Normalized path component of the NITRIS attachment URL.
            -- Example: "/nitris/docs/ReachYourStudent/file.pdf"
            -- Query strings (which contain user tokens) are stripped before
            -- insertion so the SAME file referenced by many students maps to
            -- ONE row in this table. This is the deduplication key.
            attachment_path TEXT NOT NULL,

            -- SHA-256 of file bytes — set after first successful download.
            -- Used for diagnostics + integrity verification.
            content_hash CHAR(64),

            -- Basename from Content-Disposition or URL — for the Telegram
            -- upload filename. Null until first acquisition.
            portal_filename VARCHAR(255),

            -- Telegram-side cache (the entire point of this table).
            -- Once set, this file_id is reusable for ALL students instantly
            -- with zero NITRIS traffic.
            telegram_file_id VARCHAR(500),
            file_kind VARCHAR(10),       -- 'pdf' | 'zip'
            file_size_bytes INTEGER,

            -- State machine — mirrors QuestionPaperCache.
            -- Allowed transitions:
            --   [none]                → retryable_failure    (row created on cold start)
            --   retryable_failure     → fetch_in_progress    (claimed for acquisition)
            --   fetch_in_progress     → available             (download+upload ok)
            --   fetch_in_progress     → not_available         (NITRIS confirmed 404)
            --   fetch_in_progress     → retryable_failure     (transient error)
            --   fetch_in_progress     → permanent_failure     (exhausted retries / hard error)
            --   fetch_in_progress     → fetch_in_progress     (stale-lock reaper, >5 min)
            status VARCHAR(30) NOT NULL DEFAULT 'retryable_failure',

            -- Atomic CAS claim tracking (compare-and-swap pattern from QP)
            acquired_by VARCHAR(64),
            acquired_at TIMESTAMPTZ,
            lease_expires_at TIMESTAMPTZ,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TIMESTAMPTZ,
            error_message TEXT,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Unique index on attachment_path — this IS the deduplication guarantee
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attachment_caches_path
        ON attachment_caches (attachment_path)
    """)

    # Partial index for stale-lock reaper: only fetch_in_progress rows
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_attachment_caches_acquisition
        ON attachment_caches (status, acquired_at)
        WHERE status = 'fetch_in_progress'
    """)

    # Status lookup for delivery routing
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_attachment_caches_status
        ON attachment_caches (status)
    """)

    # ── 2. Alter inbox_messages: add body_fetched_at + attachment_cache_id ──
    op.execute("""
        ALTER TABLE inbox_messages
            ADD COLUMN IF NOT EXISTS body_fetched_at TIMESTAMPTZ
    """)
    op.execute("""
        ALTER TABLE inbox_messages
            ADD COLUMN IF NOT EXISTS attachment_cache_id BIGINT
            REFERENCES attachment_caches(id) ON DELETE SET NULL
    """)

    # Index for stale-body-TTL lookup (per user — find messages with stale bodies)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_inbox_messages_body_fetched
        ON inbox_messages (user_id, body_fetched_at)
        WHERE body IS NOT NULL
    """)


def downgrade() -> None:
    # Reverse order — drop indexes, then columns, then table
    op.execute("DROP INDEX IF EXISTS idx_inbox_messages_body_fetched")
    op.execute("""
        ALTER TABLE inbox_messages
            DROP COLUMN IF EXISTS attachment_cache_id,
            DROP COLUMN IF EXISTS body_fetched_at
    """)
    op.execute("DROP INDEX IF EXISTS idx_attachment_caches_status")
    op.execute("DROP INDEX IF EXISTS idx_attachment_caches_acquisition")
    op.execute("DROP INDEX IF EXISTS idx_attachment_caches_path")
    op.execute("DROP TABLE IF EXISTS attachment_caches")
