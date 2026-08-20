"""question paper negative cache with TTL

Revision ID: 0009_qp_negative_cache
Revises: 0008_credential_quarantine
Create Date: 2026-08-21 02:50:00.000000

ARCHITECTURE
============
Adds not_available_until to question_paper_caches for durable TTL-bounded
negative caching.

When NITRIS returns form HTML on a download postback (paper not uploaded yet),
status is set to 'paper_not_available' and not_available_until is set to NOW() + 24h.
Deliver requests within that window short-circuit with zero NITRIS traffic.
When TTL expires, the row is re-checked against NITRIS.

IDEMPOTENT / REVERSIBLE: ALTER TABLE ADD COLUMN IF NOT EXISTS + DROP COLUMN.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_qp_negative_cache"
down_revision: Union[str, None] = "0008_credential_quarantine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE question_paper_caches
            ADD COLUMN IF NOT EXISTS not_available_until TIMESTAMPTZ
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_question_paper_caches_not_available_until
        ON question_paper_caches (not_available_until)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_question_paper_caches_not_available_until")
    op.execute("""
        ALTER TABLE question_paper_caches
            DROP COLUMN IF EXISTS not_available_until
    """)
