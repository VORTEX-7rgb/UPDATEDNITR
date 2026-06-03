"""SQLAlchemy declarative database models with strict indexing and constraints."""

from datetime import datetime
from typing import Optional, Any
from enum import Enum
from sqlalchemy import BigInteger, String, ForeignKey, DateTime, Boolean, Index, JSON

class EventType(str, Enum):
    NEW_SUBJECT_ADDED = "new_subject_added"
    ATTENDANCE_UPDATED = "attendance_updated"
    NEW_ABSENCE_DETECTED = "new_absence_detected"
    NEW_MESSAGE_RECEIVED = "new_message_received"
    MESSAGE_UPDATED = "message_updated"

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    """Declarative Base class for all schema models."""
    pass


class User(Base):
    """User credentials and platform configuration."""
    
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    roll_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    encrypted_password: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    snapshots: Mapped[list["Snapshot"]] = relationship(
        "Snapshot", back_populates="user", cascade="all, delete-orphan"
    )
    events: Mapped[list["Event"]] = relationship(
        "Event", back_populates="user", cascade="all, delete-orphan"
    )
    sync_state: Mapped[Optional["SyncState"]] = relationship(
        "SyncState", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    inbox_messages: Mapped[list["InboxMessage"]] = relationship(
        "InboxMessage", back_populates="user", cascade="all, delete-orphan"
    )


    def __repr__(self) -> str:
        # Secure representation: Never print password field
        return f"<User id={self.id} telegram_id={self.telegram_id} roll_number='{self.roll_number}'>"


class Snapshot(Base):
    """Immutable periodic snapshots of user portal data (e.g. attendance)."""
    
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_name: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="snapshots")

    def __repr__(self) -> str:
        return f"<Snapshot id={self.id} user_id={self.user_id} module='{self.module_name}' hash='{self.snapshot_hash[:8]}...'>"


class Event(Base):
    """Persistence store for delta changes detected in snapshots."""
    
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="events")

    def __repr__(self) -> str:
        return f"<Event id={self.id} user_id={self.user_id} type='{self.event_type}' sent={self.sent}>"


class SyncState(Base):
    """Execution state tracker for user periodic syncing."""
    
    __tablename__ = "sync_states"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    last_sync: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    failure_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="sync_state")

    def __repr__(self) -> str:
        return f"<SyncState user_id={self.user_id} failures={self.failure_count} last_success={self.last_success}>"


# Explicit composite or specific index declarations for high performance lookups
Index("idx_snapshots_user_module", Snapshot.user_id, Snapshot.module_name)
Index("idx_events_user_sent", Event.user_id, Event.sent)
Index("idx_events_created_at", Event.created_at)
Index("idx_events_event_type", Event.event_type)


class InboxMessage(Base):
    """Secure message headers, body caches, and attachment details scraped from NITRIS."""

    __tablename__ = "inbox_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    portal_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token: Mapped[str] = mapped_column(String(200), nullable=False)
    
    sender: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    
    attachment_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    sent_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="inbox_messages")

    def __repr__(self) -> str:
        return f"<InboxMessage id={self.id} user_id={self.user_id} sender='{self.sender}' subject='{self.subject[:20]}...' is_read={self.is_read}>"


# Token-level stable unique constraint per user
Index("idx_inbox_user_token", InboxMessage.user_id, InboxMessage.token, unique=True)


class QuestionPaperCache(Base):
    """Caches Telegram file IDs and portal download postback targets for exam papers."""
    
    __tablename__ = "question_paper_caches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    academic_year: Mapped[str] = mapped_column(String(50), nullable=False)
    exam_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "mid_sem" or "end_sem"
    
    portal_postback_target: Mapped[str] = mapped_column(String(500), nullable=False)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<QuestionPaperCache id={self.id} code='{self.subject_code}' year='{self.academic_year}' type='{self.exam_type}' cached={bool(self.telegram_file_id)}>"


# Composite lookup index for high-speed cache queries
Index("idx_qp_cache_lookup", QuestionPaperCache.subject_code, QuestionPaperCache.academic_year, QuestionPaperCache.exam_type, unique=True)
Index("idx_inbox_portal_msg_id", InboxMessage.portal_message_id)



