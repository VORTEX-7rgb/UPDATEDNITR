"""initial schema — all 6 tables from app.db.models

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-17 18:35:00.000000

This migration creates the COMPLETE database schema as defined in
app.db.models.Base.metadata. It is the source of truth for the schema.

Tables:
  - users                  (id, telegram_id, roll_number, encrypted_password, created_at, updated_at)
  - snapshots              (id, user_id, module_name, snapshot_json, snapshot_hash, created_at)
  - events                 (id, user_id, event_type, payload_json, sent, created_at)
  - sync_states            (id, user_id, last_sync, last_success, last_error, failure_count, last_metrics)
  - inbox_messages         (id, user_id, portal_message_id, token, sender, subject, body,
                            attachment_url, telegram_file_id, is_read, sent_on, created_at)
  - question_paper_caches  (id, subject_code, academic_year, exam_type,
                            portal_postback_target, telegram_file_id, created_at)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("roll_number", sa.String(length=50), nullable=False),
        sa.Column("encrypted_password", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_index("ix_users_roll_number", "users", ["roll_number"])

    # ------------------------------------------------------------ snapshots
    op.create_table(
        "snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("module_name", sa.String(length=100), nullable=False),
        sa.Column(
            "snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_snapshots_user_id", "snapshots", ["user_id"])
    op.create_index("ix_snapshots_snapshot_hash", "snapshots", ["snapshot_hash"])
    op.create_index(
        "idx_snapshots_user_module",
        "snapshots",
        ["user_id", "module_name"],
    )

    # --------------------------------------------------------------- events
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("sent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_user_id", "events", ["user_id"])
    op.create_index("ix_events_sent", "events", ["sent"])
    op.create_index("ix_events_created_at", "events", ["created_at"])
    op.create_index("ix_events_event_type", "events", ["event_type"])
    op.create_index("idx_events_user_sent", "events", ["user_id", "sent"])

    # ---------------------------------------------------------- sync_states
    op.create_table(
        "sync_states",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_sync", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_sync_states_user_id", "sync_states", ["user_id"])

    # ------------------------------------------------------- inbox_messages
    op.create_table(
        "inbox_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("portal_message_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.String(length=200), nullable=False),
        sa.Column("sender", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("attachment_url", sa.String(length=1000), nullable=True),
        sa.Column("telegram_file_id", sa.String(length=500), nullable=True),
        sa.Column("is_read", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sent_on", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inbox_messages_user_id", "inbox_messages", ["user_id"])
    op.create_index("ix_inbox_messages_is_read", "inbox_messages", ["is_read"])
    op.create_index(
        "ix_inbox_messages_portal_message_id",
        "inbox_messages",
        ["portal_message_id"],
    )
    op.create_index(
        "idx_inbox_user_token",
        "inbox_messages",
        ["user_id", "token"],
        unique=True,
    )

    # ------------------------------------------------ question_paper_caches
    op.create_table(
        "question_paper_caches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_code", sa.String(length=50), nullable=False),
        sa.Column("academic_year", sa.String(length=50), nullable=False),
        sa.Column("exam_type", sa.String(length=20), nullable=False),
        sa.Column("portal_postback_target", sa.String(length=500), nullable=False),
        sa.Column("telegram_file_id", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_question_paper_caches_subject_code",
        "question_paper_caches",
        ["subject_code"],
    )
    op.create_index(
        "idx_qp_cache_lookup",
        "question_paper_caches",
        ["subject_code", "academic_year", "exam_type"],
        unique=True,
    )


def downgrade() -> None:
    """Tear down the entire schema — destructive, use with care."""
    op.drop_index("idx_qp_cache_lookup", table_name="question_paper_caches")
    op.drop_index(
        "ix_question_paper_caches_subject_code", table_name="question_paper_caches"
    )
    op.drop_table("question_paper_caches")

    op.drop_index("idx_inbox_user_token", table_name="inbox_messages")
    op.drop_index(
        "ix_inbox_messages_portal_message_id", table_name="inbox_messages"
    )
    op.drop_index("ix_inbox_messages_is_read", table_name="inbox_messages")
    op.drop_index("ix_inbox_messages_user_id", table_name="inbox_messages")
    op.drop_table("inbox_messages")

    op.drop_index("ix_sync_states_user_id", table_name="sync_states")
    op.drop_table("sync_states")

    op.drop_index("idx_events_user_sent", table_name="events")
    op.drop_index("ix_events_event_type", table_name="events")
    op.drop_index("ix_events_created_at", table_name="events")
    op.drop_index("ix_events_sent", table_name="events")
    op.drop_index("ix_events_user_id", table_name="events")
    op.drop_table("events")

    op.drop_index("idx_snapshots_user_module", table_name="snapshots")
    op.drop_index("ix_snapshots_snapshot_hash", table_name="snapshots")
    op.drop_index("ix_snapshots_user_id", table_name="snapshots")
    op.drop_table("snapshots")

    op.drop_index("ix_users_roll_number", table_name="users")
    op.drop_table("users")
