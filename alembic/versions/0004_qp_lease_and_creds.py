"""qp lease and user credential validity tracking

Revision ID: 0004_qp_lease_and_creds
Revises: 0003_event_dispatcher_state
Create Date: 2026-08-18 20:00:00.000000

Adds lease expiration and heartbeat tracking to question_paper_caches, and credential
health tracking to users.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0004_qp_lease_and_creds"
down_revision: Union[str, None] = "0003_event_dispatcher_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS credentials_valid BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS qp_fail_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS qp_cooldown_until TIMESTAMPTZ
    """)

    op.execute("""
        ALTER TABLE question_paper_caches
            ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS pending_file_id VARCHAR(500)
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE question_paper_caches
            DROP COLUMN IF EXISTS pending_file_id,
            DROP COLUMN IF EXISTS heartbeat_at,
            DROP COLUMN IF EXISTS lease_expires_at
    """)
    op.execute("""
        ALTER TABLE users
            DROP COLUMN IF EXISTS qp_cooldown_until,
            DROP COLUMN IF EXISTS qp_fail_count,
            DROP COLUMN IF EXISTS credentials_valid
    """)
