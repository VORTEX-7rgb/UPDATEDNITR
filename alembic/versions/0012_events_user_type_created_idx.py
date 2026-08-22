"""PERF #4: composite index on events(user_id, event_type, created_at)

Revision ID: 0012_events_user_type_created_idx
Revises: 0011_last_seen_at

Serves the two hottest event reads without per-user scans:
  * Dashboard briefing:  SELECT event_type, COUNT(*) FROM events
                         WHERE user_id=? AND created_at>? GROUP BY event_type
  * Absence lines:       SELECT payload_json FROM events
                         WHERE user_id=? AND event_type=? AND created_at>?
                         ORDER BY created_at DESC LIMIT 20
IDEMPOTENT / REVERSIBLE.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0012_events_user_type_created_idx"
down_revision: Union[str, None] = "0011_last_seen_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_user_type_created
        ON events (user_id, event_type, created_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_events_user_type_created")
