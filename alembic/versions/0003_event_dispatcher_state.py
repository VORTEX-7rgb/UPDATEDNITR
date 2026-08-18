"""event dispatcher state — atomic claim + per-event mark-sent + retry policy

Revision ID: 0003_event_dispatcher_state
Revises: 0002_qp_state_machine
Create Date: 2026-08-17 20:00:00.000000

Adds explicit state-machine columns to the events table so the dispatcher can:
  - Claim events atomically via Compare-And-Swap (CAS)
  - Mark each event as sent immediately on delivery (no bulk-update duplicate risk)
  - Recover claims from crashed workers via a background stale-claim reaper
  - Track delivery attempts and terminate persistent failures (e.g. user blocked bot)

Columns added:
  - claimed_at TIMESTAMPTZ      — when a worker claimed this event (NULL=unclaimed)
  - claimed_by VARCHAR(64)     — worker UUID (multi-process safety)
  - sent_at TIMESTAMPTZ        — when delivered to Telegram
  - attempt_count INTEGER       — retry counter
  - last_error TEXT             — last error message
  - permanent_failure BOOLEAN   — terminal state for un-deliverable events

Idempotent: uses IF NOT EXISTS via ADD COLUMN ... IF NOT EXISTS pattern.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003_event_dispatcher_state"
down_revision: Union[str, None] = "0002_qp_state_machine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE events
            ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(64),
            ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_error TEXT,
            ADD COLUMN IF NOT EXISTS permanent_failure BOOLEAN NOT NULL DEFAULT FALSE
    """)

    # Partial index for the atomic claim query
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_claim
        ON events (id, claimed_at)
        WHERE sent = FALSE AND permanent_failure = FALSE
    """)

    # Partial index for permanent failures / monitoring
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_permanent
        ON events (id)
        WHERE permanent_failure = TRUE
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_events_permanent")
    op.execute("DROP INDEX IF EXISTS idx_events_claim")
    op.execute("""
        ALTER TABLE events
            DROP COLUMN IF EXISTS permanent_failure,
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS attempt_count,
            DROP COLUMN IF EXISTS sent_at,
            DROP COLUMN IF EXISTS claimed_by,
            DROP COLUMN IF EXISTS claimed_at
    """)
