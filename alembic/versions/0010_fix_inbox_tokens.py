"""fix inbox token shift unique constraint and make portal_message_id authoritative

Revision ID: 0010_fix_inbox_tokens
Revises: 0009_qp_negative_cache
Create Date: 2026-08-21 17:40:00.000000

ARCHITECTURE
============
Fixes the unique constraint violation when ASP.NET GridView postback tokens shift
(e.g., ctl12 -> ctl13) upon new message arrival.

Changes:
  1. Deduplicates any legacy duplicate rows per (user_id, portal_message_id) keeping latest.
  2. Drops the UNIQUE index on (user_id, token).
  3. Adds a UNIQUE index on (user_id, portal_message_id).
  4. Adds a non-unique index on (user_id, token) for fast token lookups.

IDEMPOTENT / REVERSIBLE.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010_fix_inbox_tokens"
down_revision: Union[str, None] = "0009_qp_negative_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Deduplicate legacy rows keeping the latest ID per (user_id, portal_message_id)
    op.execute("""
        DELETE FROM inbox_messages
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM inbox_messages
            GROUP BY user_id, portal_message_id
        )
    """)

    # 2. Drop the old unique index on (user_id, token)
    op.execute("DROP INDEX IF EXISTS idx_inbox_user_token")

    # 3. Create the stable UNIQUE index on (user_id, portal_message_id)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_user_portal_msg_id
        ON inbox_messages (user_id, portal_message_id)
    """)

    # 4. Create a non-unique index on (user_id, token) for fast O(1) queries
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_inbox_messages_user_token
        ON inbox_messages (user_id, token)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_inbox_messages_user_token")
    op.execute("DROP INDEX IF EXISTS idx_inbox_user_portal_msg_id")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_user_token
        ON inbox_messages (user_id, token)
    """)
