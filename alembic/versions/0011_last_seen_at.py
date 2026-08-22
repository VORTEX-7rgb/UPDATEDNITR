"""claw briefing marker: sync_states.last_seen_at

Revision ID: 0011_last_seen_at
Revises: 0010_fix_inbox_tokens
Create Date: 2026-08-22 06:00:00.000000

Adds ONE nullable column powering the "while you were gone" briefing:
    sync_states.last_seen_at TIMESTAMPTZ NULL

NULL = user has never had a dashboard rendered (briefing stays suppressed
until first render stamps it). Purely additive; nothing reads it except the
dashboard renderer.

IDEMPOTENT / REVERSIBLE.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011_last_seen_at"
down_revision: Union[str, None] = "0010_fix_inbox_tokens"
branch_labels: Union[str, Sequence[str]] = None
depends_on: Union[str, Sequence[str]] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sync_states
            ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE sync_states DROP COLUMN IF EXISTS last_seen_at")
