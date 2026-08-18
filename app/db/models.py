"""SQLAlchemy declarative database models with strict indexing and constraints."""

from datetime import datetime
from typing import Optional, Any
from enum import Enum
from sqlalchemy import BigInteger, String, ForeignKey, DateTime, Boolean, Index, JSON, Integer, Text, UniqueConstraint

class EventType(str, Enum):
    NEW_SUBJECT_ADDED = "new_subject_added"
    ATTENDANCE_UPDATED = "attendance_updated"
    NEW_ABSENCE_DETECTED = "new_absence_detected"
    NEW_MESSAGE_RECEIVED = "new_message_received"
    MESSAGE_UPDATED = "message_updated"

class QPStatus(str, Enum):
    """Lifecycle states for a question_paper_caches row.

    State transitions (all atomic via UPDATE...WHERE status=...):
      [none]                       → fetch_in_progress    (first claim)
      fetch_in_progress            → paper_available       (download+upload succeeded)
      fetch_in_progress            → paper_not_available   (NITRIS confirmed no paper)
      fetch_in_progress            → retryable_failure     (transient error)
      fetch_in_progress            → permanent_failure    (exhausted retries or hard error)
      retryable_failure            → fetch_in_progress    (re-claim on next request)
      fetch_in_progress(stale)     → fetch_in_progress    (stale-lock reaper, >5 min)
    """
    PAPER_AVAILABLE = "paper_available"
    PAPER_NOT_AVAILABLE = "paper_not_available"
    FETCH_IN_PROGRESS = "fetch_in_progress"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"

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
    credentials_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    qp_fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qp_cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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
    sync_schedules: Mapped[list["ModuleSyncSchedule"]] = relationship(
        "ModuleSyncSchedule", back_populates="user", cascade="all, delete-orphan"
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
    """Persistence store for delta changes detected in snapshots.

    State machine (mirrors QPaperService pattern):
      sent=False, claimed_at=NULL                  → ready to be claimed by dispatcher
      sent=False, claimed_at=NOW()                 → in-flight, being sent
      sent=True, sent_at=NOW(), permanent_failure=False  → delivered successfully
      sent=True, permanent_failure=True            → terminal failure (user blocked, exhausted retries, orphaned)

    Atomic claim prevents duplicate sends across processes. Per-event mark_sent
    eliminates the bulk-update vulnerability. Stale-claim reaper reclaims
    crashed dispatcher's claims.
    """
    
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)

    # Delivery state (state machine)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    permanent_failure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Claim tracking (atomic multi-process-safe claim)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Retry policy
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="events")

    def __repr__(self) -> str:
        return (
            f"<Event id={self.id} user_id={self.user_id} type='{self.event_type}' "
            f"sent={self.sent} attempts={self.attempt_count}"
            f"{' PERM' if self.permanent_failure else ''}>"
        )


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
    """Global multi-tenant cache for NITRIS question papers.

    A single row per (subject_code, academic_year, exam_type) tuple. Shared across
    ALL students — never duplicated per user. The telegram_file_id stored here is the
    durable Telegram file reference for the bot's private QP storage channel; forwarding
    it to any user costs zero NITRIS traffic.

    Lifecycle state machine (see QPStatus enum):
      fetch_in_progress  → paper_available | paper_not_available | retryable_failure | permanent_failure
      retryable_failure  → fetch_in_progress (re-claim)
      fetch_in_progress stale (>5min) → re-claimed by next request

    Atomic state transitions are enforced by the QPaperService via UPDATE...WHERE status=...
    Compare-And-Swap pattern, never by read-modify-write.
    """

    __tablename__ = "question_paper_caches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    academic_year: Mapped[str] = mapped_column(String(50), nullable=False)
    exam_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "mid_sem" or "end_sem"
    
    portal_postback_target: Mapped[str] = mapped_column(String(500), nullable=False)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pending_file_id: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # State machine
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=QPStatus.FETCH_IN_PROGRESS.value, index=True
    )
    acquired_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    acquired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    file_kind: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # pdf | zip
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<QuestionPaperCache id={self.id} code='{self.subject_code}' "
            f"year='{self.academic_year}' type='{self.exam_type}' "
            f"status={self.status} cached={bool(self.telegram_file_id)}>"
        )


# Composite lookup index for high-speed cache queries (unique per paper)
Index("idx_qp_cache_lookup",
      QuestionPaperCache.subject_code,
      QuestionPaperCache.academic_year,
      QuestionPaperCache.exam_type,
      unique=True)
Index("idx_inbox_portal_msg_id", InboxMessage.portal_message_id)

# Partial index for stale-lock reaper: only rows currently being acquired
Index(
    "idx_qp_cache_acquisition",
    QuestionPaperCache.status,
    QuestionPaperCache.acquired_at,
    postgresql_where=QuestionPaperCache.status == QPStatus.FETCH_IN_PROGRESS.value,
)
# Retry-policy lookup
Index(
    "idx_qp_cache_status_attempts",
    QuestionPaperCache.status,
    QuestionPaperCache.attempt_count,
    QuestionPaperCache.last_attempt_at,
)


class ModuleSyncSchedule(Base):
    """Durable per-user per-module sync schedule for the background TTL scheduler."""

    __tablename__ = "module_sync_schedule"
    __table_args__ = (
        UniqueConstraint("user_id", "module_name", name="uq_module_sync_schedule_user_module"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_sync_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scheduler_claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="sync_schedules")

    def __repr__(self) -> str:
        return (
            f"<ModuleSyncSchedule id={self.id} user_id={self.user_id} module='{self.module_name}' "
            f"next_sync_at={self.next_sync_at} status='{self.last_status}' failures={self.consecutive_failures}>"
        )


Index(
    "idx_module_sync_schedule_due",
    ModuleSyncSchedule.next_sync_at,
    postgresql_where=ModuleSyncSchedule.last_status != "disabled",
)
Index("idx_module_sync_schedule_claim", ModuleSyncSchedule.scheduler_claimed_at)

# Event dispatcher atomic-claim partial index
Index(
    "idx_events_claim",
    Event.id,
    Event.claimed_at,
    postgresql_where=(Event.sent == False) & (Event.permanent_failure == False),
)
Index(
    "idx_events_permanent",
    Event.id,
    postgresql_where=Event.permanent_failure == True,
)

