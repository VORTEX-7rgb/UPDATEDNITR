"""credential quarantine (one LoginError = permanent block until re-register)

Revision ID: 0008_credential_quarantine
Revises: 0007_inbox_cache_and_attachments
Create Date: 2026-08-20 17:00:00.000000

ARCHITECTURE
============
Adds the per-user credential-quarantine columns backing the auth gate.

  1. `credentials_version` — bumped on every successful re-registration.
     Gives "one authentication attempt per credential version": a quarantined
     credential version can never be auto-retried, and a new version is only
     minted when the user explicitly re-submits credentials.

  2. `credentials_invalid_at` — timestamp of the most recent quarantine
     transition (audit + observability).

`credentials_valid` (already present) remains the single boolean gate. The
hard invariant is enforced in app/nitris/auth_gate.py + app/nitris/gateway.py:

    one confirmed LoginError  →  credentials_valid = FALSE  →  notify user
    all automatic login paths refuse (gateway in-memory guard + DB check)
    only /forgot / re-registration can flip credentials_valid back to TRUE

IDEMPOTENT / REVERSIBLE: ALTER TABLE ADD COLUMN IF NOT EXISTS + DROP COLUMN.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_credential_quarantine"
down_revision: Union[str, None] = "0007_inbox_cache_and_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS credentials_version INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS credentials_invalid_at TIMESTAMPTZ
    """)
    # Fast lookup of currently-quarantined users (startup seed + admin stats).
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_credentials_valid
        ON users (credentials_valid)
        WHERE credentials_valid = FALSE
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_users_credentials_valid")
    op.execute("""
        ALTER TABLE users
            DROP COLUMN IF EXISTS credentials_invalid_at,
            DROP COLUMN IF EXISTS credentials_version
    """)
