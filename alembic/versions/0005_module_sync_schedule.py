"""module sync schedule for per-module TTL scheduler (Phase 5)

Revision ID: 0005_module_sync_schedule
Revises: 0004_qp_lease_and_creds
Create Date: 2026-08-19 00:00:00.000000

Creates module_sync_schedule table for durable, per-module TTL background sync scheduling.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0005_module_sync_schedule"
down_revision: Union[str, None] = "0004_qp_lease_and_creds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS module_sync_schedule (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            module_name VARCHAR(100) NOT NULL,
            last_synced_at TIMESTAMPTZ,
            next_sync_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            last_error TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            scheduler_claimed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_module_sync_schedule_user_module UNIQUE (user_id, module_name)
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_module_sync_schedule_due
        ON module_sync_schedule (next_sync_at ASC)
        WHERE last_status != 'disabled'
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_module_sync_schedule_claim
        ON module_sync_schedule (scheduler_claimed_at)
    """)

    # Backfill: create schedule rows for existing users with staggered next_sync_at
    # to avoid a thundering herd on startup.
    op.execute("""
        INSERT INTO module_sync_schedule (user_id, module_name, next_sync_at, last_status)
        SELECT
            u.id,
            m.module,
            NOW() + (RANDOM() * INTERVAL '6 hours'),
            'pending'
        FROM users u
        CROSS JOIN (VALUES ('attendance'), ('inbox')) AS m(module)
        WHERE u.credentials_valid = TRUE
        ON CONFLICT (user_id, module_name) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS module_sync_schedule")
