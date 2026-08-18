"""qp state machine — add status + acquisition tracking columns

Revision ID: 0002_qp_state_machine
Revises: 0001_initial_schema
Create Date: 2026-08-17 19:00:00.000000

Adds explicit state-machine columns to question_paper_caches so the cache can
distinguish:
  - paper_available       (telegram_file_id set, instant delivery)
  - paper_not_available   (NITRIS confirmed no paper exists — clean UI state)
  - fetch_in_progress     (a worker is currently acquiring — concurrent requests collapse)
  - retryable_failure     (transient failure — next request can re-acquire)
  - permanent_failure     (exhausted retries — human intervention needed)

Also adds:
  - acquired_by / acquired_at  → crash-safe lock tracking + stale-lock reaping
  - file_kind                  → 'pdf' or 'zip' (NITRIS returns both)
  - file_size_bytes            → for monitoring / quota
  - last_attempt_at            → for retry backoff
  - attempt_count              → for retry exhaustion
  - error_message              → diagnostic
  - updated_at                 → row mtime

Idempotent: uses IF NOT EXISTS via ADD COLUMN ... IF NOT EXISTS pattern.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0002_qp_state_machine"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use raw text + IF NOT EXISTS so this migration is idempotent on already-modified DBs
    op.execute("""
        ALTER TABLE question_paper_caches
            ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'fetch_in_progress',
            ADD COLUMN IF NOT EXISTS acquired_by VARCHAR(64),
            ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS file_kind VARCHAR(10),
            ADD COLUMN IF NOT EXISTS file_size_bytes INTEGER,
            ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS error_message TEXT,
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    """)

    # Migrate existing rows: any row with telegram_file_id set becomes paper_available;
    # rows with portal_postback_target but no telegram_file_id stay fetch_in_progress
    # (will be acquired on first request).
    op.execute("""
        UPDATE question_paper_caches
        SET status = 'paper_available'
        WHERE telegram_file_id IS NOT NULL AND status = 'fetch_in_progress'
    """)
    # Rows with no telegram_file_id but a postback target = stub from metadata sync;
    # mark them as retryable_failure so the first user request will try to acquire.
    op.execute("""
        UPDATE question_paper_caches
        SET status = 'retryable_failure'
        WHERE telegram_file_id IS NULL AND status = 'fetch_in_progress'
    """)

    # Index for stale-lock reaper: WHERE status = 'fetch_in_progress' AND acquired_at < ...
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_qp_cache_acquisition
        ON question_paper_caches (status, acquired_at)
        WHERE status = 'fetch_in_progress'
    """)

    # Index for retry-policy queries
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_qp_cache_status_attempts
        ON question_paper_caches (status, attempt_count, last_attempt_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qp_cache_status_attempts")
    op.execute("DROP INDEX IF EXISTS idx_qp_cache_acquisition")
    op.execute("""
        ALTER TABLE question_paper_caches
            DROP COLUMN IF EXISTS updated_at,
            DROP COLUMN IF EXISTS error_message,
            DROP COLUMN IF EXISTS attempt_count,
            DROP COLUMN IF EXISTS last_attempt_at,
            DROP COLUMN IF EXISTS file_size_bytes,
            DROP COLUMN IF EXISTS file_kind,
            DROP COLUMN IF EXISTS acquired_at,
            DROP COLUMN IF EXISTS acquired_by,
            DROP COLUMN IF EXISTS status
    """)
