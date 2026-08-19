"""Add timetable_entries table for storing per-user weekly class timetable.

Revision ID: 0006_add_timetable
Revises: 0005_module_sync_schedule
Create Date: 2026-08-20 00:00:00.000000

Stores one row per (user, weekday, period) — the full weekly class schedule
synced from the NITRIS Home.aspx dashboard. Sync is MANUAL ONLY (triggered
by the /timetablesync command or the 📅 Sync button); per tier_1 architecture
in NITRIS_PORTAL_RECON.json, class_timetable TTL = 7 days, but we override
to manual because timetables only change at semester boundaries and the user
wants explicit control over refreshes.

Schema matches the recon timetable shape 1:1:
    day         -> weekday (smallint, 0=Mon ... 6=Sun)
    period_index-> period_index (1-9)
    start_time  -> start_time (TIME, wall-clock IST)
    end_time    -> end_time (TIME, wall-clock IST)
    subject     -> subject_code (text; "LUNCH" for break rows)
    room        -> room (text, "" if no room)
    is_break    -> is_break (bool, True only for the LUNCH row)

Replace strategy on sync: DELETE WHERE user_id + INSERT all new rows, in a
single transaction. Atomic, no partial state, mirrors the QPaperCache
compare-and-swap discipline.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006_add_timetable"
down_revision: Union[str, None] = "0005_module_sync_schedule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS timetable_entries (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            weekday         SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
            period_index    SMALLINT NOT NULL CHECK (period_index BETWEEN 1 AND 12),
            start_time      TIME NOT NULL,
            end_time        TIME NOT NULL,
            subject_code    TEXT NOT NULL,
            room            TEXT NOT NULL DEFAULT '',
            is_break        BOOLEAN NOT NULL DEFAULT FALSE,
            subject_name    TEXT NOT NULL DEFAULT '',
            course_type     TEXT NOT NULL DEFAULT '',
            synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Lookup: load all entries for a user in one round trip (the typical "now/next"
    # query). A student week has ≤ 45 entries — tiny payload.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_timetable_user
        ON timetable_entries (user_id)
    """)

    # Lookup: ordered walk for a single day's classes (used by /now and /timetable display)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_timetable_user_day_period
        ON timetable_entries (user_id, weekday, period_index)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS timetable_entries")
