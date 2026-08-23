"""speed indexes: inbox ordering, unread partial, event message_id expression

Revision ID: 0013_speed_indexes
Revises: 0012_events_user_type_created_idx
Create Date: 2026-08-23

PERF hardening for ~5k-user hot paths:
  - idx_inbox_user_sent_on        → inbox list pages stop sorting per query
  - idx_inbox_user_unread         → unread badge count / mark_all_as_read scan a tiny partial index
  - idx_events_user_type_msgid    → duplicate-notification guard becomes an
                                    index hit instead of scanning every event
                                    row per user inside the advisory-locked
                                    inbox persist transaction
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_speed_indexes"
down_revision: Union[str, None] = "0012_events_user_type_created_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_inbox_user_sent_on",
        "inbox_messages",
        ["user_id", sa.text("sent_on DESC")],
    )
    op.create_index(
        "idx_inbox_user_unread",
        "inbox_messages",
        ["user_id"],
        postgresql_where=sa.text("is_read = false"),
    )
    op.create_index(
        "idx_events_user_type_msgid",
        "events",
        ["user_id", "event_type", sa.text("(payload_json->>'message_id')")],
    )


def downgrade() -> None:
    op.drop_index("idx_events_user_type_msgid", table_name="events")
    op.drop_index("idx_inbox_user_unread", table_name="inbox_messages")
    op.drop_index("idx_inbox_user_sent_on", table_name="inbox_messages")
