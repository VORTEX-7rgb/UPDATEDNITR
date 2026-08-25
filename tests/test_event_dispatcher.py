"""Comprehensive unit and concurrency test suite for EventDispatcherService.

Uses in-memory FakeSession and FakeBot to deterministically test:
  - Atomic CAS claim query (zero cross-worker overlap)
  - Immediate per-event mark-sent (crash window ~10ms, not bulk ~30s)
  - Stale-claim reaper (recovers crashed worker claims)
  - FloodWait retry & backoff
  - Terminal state transitions (user blocked, deactivated, orphaned, exhausted retries)
  - Ensuring no DB session is held during Telegram network send
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import pytest

from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramAPIError

from app.db.models import EventType
from app.services.event_dispatcher_service import (
    EventDispatcherService,
    claim_events,
    mark_event_sent,
    mark_event_permanent_failure,
    release_event_claim,
    reap_stale_claims,
    get_telegram_id_for_user,
    _format_notification,
    MAX_DISPATCH_ATTEMPTS,
    CLAIM_STALE_SECONDS,
)


# ── Fakes ──────────────────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, rows: list, rowcount: int = 0):
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """In-memory DB session that emulates the SQL queries used by EventDispatcherService."""

    def __init__(
        self,
        event_store: Dict[int, Dict[str, Any]],
        user_store: Dict[int, int],
        lock: asyncio.Lock,
        active_sessions_tracker: Optional[list] = None,
    ):
        self.event_store = event_store
        self.user_store = user_store
        self.lock = lock
        self.active_sessions_tracker = active_sessions_tracker

    async def __aenter__(self):
        if self.active_sessions_tracker is not None:
            self.active_sessions_tracker.append(self)
        return self

    async def __aexit__(self, *args):
        if self.active_sessions_tracker is not None and self in self.active_sessions_tracker:
            self.active_sessions_tracker.remove(self)

    def begin(self):
        return self

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        now = datetime.now(timezone.utc)

        async with self.lock:
            # 1. Atomic claim query
            if "UPDATE events" in sql and "RETURNING" in sql:
                worker_id = params.get("worker_id", "test-worker")
                stale_secs = int(params.get("stale_secs", CLAIM_STALE_SECONDS))
                batch_size = int(params.get("batch", 25))

                eligible_ids = []
                for eid, ev in sorted(self.event_store.items(), key=lambda x: x[0]):
                    if ev.get("sent") or ev.get("permanent_failure"):
                        continue
                    claimed_at = ev.get("claimed_at")
                    is_unclaimed = claimed_at is None
                    is_stale = (
                        claimed_at is not None
                        and (now - claimed_at).total_seconds() > stale_secs
                    )
                    if is_unclaimed or is_stale:
                        eligible_ids.append(eid)
                        if len(eligible_ids) >= batch_size:
                            break

                results = []
                for eid in eligible_ids:
                    ev = self.event_store[eid]
                    ev["claimed_at"] = now
                    ev["claimed_by"] = worker_id
                    # M1 fix: claiming does NOT touch attempt_count — the
                    # budget only burns on real failed delivery attempts.
                    results.append((
                        eid,
                        ev["user_id"],
                        ev["event_type"],
                        ev["payload_json"],
                        ev.get("attempt_count", 0),
                    ))
                return FakeResult(results, rowcount=len(results))

            # 2. Mark event sent
            if "SET sent = TRUE" in sql and "permanent_failure = TRUE" not in sql:
                eid = params.get("id")
                if eid in self.event_store:
                    ev = self.event_store[eid]
                    ev["sent"] = True
                    ev["sent_at"] = now
                    ev["claimed_at"] = None
                    ev["claimed_by"] = None
                    ev["last_error"] = None
                return FakeResult([], rowcount=1)

            # 3. Mark permanent failure
            if "SET sent = TRUE" in sql and "permanent_failure = TRUE" in sql:
                eid = params.get("id")
                if eid in self.event_store:
                    ev = self.event_store[eid]
                    ev["sent"] = True
                    ev["sent_at"] = now
                    ev["permanent_failure"] = True
                    ev["claimed_at"] = None
                    ev["claimed_by"] = None
                    ev["last_error"] = params.get("err")
                return FakeResult([], rowcount=1)

            # 4. Release claim (M1 fix: a REAL failed attempt — burns budget)
            if "SET claimed_at = NULL" in sql and "sent = FALSE" not in sql:
                eid = params.get("id")
                if eid in self.event_store:
                    ev = self.event_store[eid]
                    ev["claimed_at"] = None
                    ev["claimed_by"] = None
                    if "attempt_count = attempt_count + 1" in sql:
                        ev["attempt_count"] = ev.get("attempt_count", 0) + 1
                    ev["last_error"] = params.get("err")
                return FakeResult([], rowcount=1)

            # 5. Stale claim reaper
            if "stale-claim-reaped" in sql:
                stale_secs = int(params.get("stale_secs", CLAIM_STALE_SECONDS))
                count = 0
                for eid, ev in self.event_store.items():
                    if not ev.get("sent") and not ev.get("permanent_failure"):
                        claimed_at = ev.get("claimed_at")
                        if claimed_at and (now - claimed_at).total_seconds() > (stale_secs * 2):
                            ev["claimed_at"] = None
                            ev["claimed_by"] = None
                            ev["last_error"] = (ev.get("last_error") or "") + " [stale-claim-reaped]"
                            count += 1
                return FakeResult([], rowcount=count)

            # 6. Telegram ID lookup — single (legacy) and batched forms
            if "SELECT telegram_id FROM users" in sql:
                uid = params.get("id")
                tid = self.user_store.get(uid)
                if tid is not None:
                    return FakeResult([(tid,)])
                return FakeResult([])

            if "FROM users" in sql and "ANY(:ids)" in sql:
                ids = params.get("ids") or []
                rows = [(uid, self.user_store[uid]) for uid in ids if uid in self.user_store]
                return FakeResult(rows)

            return FakeResult([])


class FakeBot:
    """Mock Telegram bot allowing failure simulations."""

    def __init__(self, active_sessions_tracker: Optional[list] = None):
        self.sent_messages: List[Dict[str, Any]] = []
        self.fail_mode: Optional[str] = None
        self.floodwait_seconds: int = 1
        self.floodwait_remaining: int = 0
        self.active_sessions_tracker = active_sessions_tracker
        self.assert_no_active_session_during_send = True

    async def send_message(self, chat_id: int, text: str, parse_mode=None, reply_markup=None):
        if self.assert_no_active_session_during_send and self.active_sessions_tracker:
            if len(self.active_sessions_tracker) > 0:
                raise RuntimeError("DB session was held open during bot.send_message network call!")

        if self.fail_mode == "floodwait":
            if self.floodwait_remaining > 0:
                self.floodwait_remaining -= 1
                raise TelegramRetryAfter(method="sendMessage", message="Flood control", retry_after=self.floodwait_seconds)

        elif self.fail_mode == "blocked":
            raise TelegramForbiddenError(method="sendMessage", message="Forbidden: bot was blocked by the user")

        elif self.fail_mode == "deactivated":
            raise TelegramAPIError(method="sendMessage", message="Bad Request: chat not found")

        elif self.fail_mode == "transient":
            raise TelegramAPIError(method="sendMessage", message="Internal server error")

        self.sent_messages.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        })
        return True


# ── Test Suite ─────────────────────────────────────────────────────────────

@pytest.fixture
def test_setup():
    lock = asyncio.Lock()
    active_sessions = []
    user_store = {1: 10001, 2: 10002, 3: 10003}
    event_store = {
        1: {"id": 1, "user_id": 1, "event_type": "attendance_updated", "payload_json": {"subject_name": "Maths", "subject_code": "MA1001", "changes": {"tc": {"old": "10", "new": "11"}}}, "sent": False, "attempt_count": 0, "claimed_at": None, "permanent_failure": False},
        2: {"id": 2, "user_id": 2, "event_type": "new_absence_detected", "payload_json": {"subject_name": "Physics", "subject_code": "PH1001", "old_ua": "1", "new_ua": "2", "total_classes": "15"}, "sent": False, "attempt_count": 0, "claimed_at": None, "permanent_failure": False},
        3: {"id": 3, "user_id": 3, "event_type": "new_message_received", "payload_json": {"sender": "Dean", "subject": "Exam Schedule", "body_snippet": "Mid sem dates announced", "has_attachment": False, "message_id": 42}, "sent": False, "attempt_count": 0, "claimed_at": None, "permanent_failure": False},
    }

    def session_factory():
        return FakeSession(event_store, user_store, lock, active_sessions)

    bot = FakeBot(active_sessions_tracker=active_sessions)
    service = EventDispatcherService(bot=bot, session_factory=session_factory, worker_id="test-worker-1")
    return service, bot, event_store, user_store, session_factory


@pytest.mark.asyncio
async def test_per_event_mark_sent_crash_window(test_setup):
    service, bot, event_store, user_store, session_factory = test_setup

    sent_count = await service._dispatch_once()
    assert sent_count == 3
    assert len(bot.sent_messages) == 3

    # Verify all 3 events are marked sent with cleared claims
    for eid in [1, 2, 3]:
        ev = event_store[eid]
        assert ev["sent"] is True
        assert ev["claimed_at"] is None
        assert ev["sent_at"] is not None
        # M1 fix: a SUCCESSFUL delivery never burns retry budget — only real
        # failures do. Restarts can therefore never drop unsent notifications.
        assert ev["attempt_count"] == 0


@pytest.mark.asyncio
async def test_no_db_session_held_during_telegram_send(test_setup):
    """Ensures no active database session remains open while bot.send_message executes."""
    service, bot, event_store, user_store, session_factory = test_setup
    bot.assert_no_active_session_during_send = True

    sent_count = await service._dispatch_once()
    assert sent_count == 3


@pytest.mark.asyncio
async def test_atomic_claim_prevents_cross_process_duplicates(test_setup):
    """Verifies that two concurrent worker instances receive disjoint claims with zero overlap."""
    service, bot, event_store, user_store, session_factory = test_setup

    # Worker 1 claims
    claimed_1 = await claim_events(session_factory, worker_id="worker-1", batch_size=2)
    assert len(claimed_1) == 2
    ids_1 = {r["id"] for r in claimed_1}
    assert ids_1 == {1, 2}

    # Worker 2 claims concurrently
    claimed_2 = await claim_events(session_factory, worker_id="worker-2", batch_size=2)
    assert len(claimed_2) == 1
    ids_2 = {r["id"] for r in claimed_2}
    assert ids_2 == {3}

    # Zero overlap
    assert len(ids_1.intersection(ids_2)) == 0


@pytest.mark.asyncio
async def test_stale_claim_reaper(test_setup):
    """Verifies that the reaper reclaims events claimed > 10 min ago by a crashed worker."""
    service, bot, event_store, user_store, session_factory = test_setup

    # Simulate crashed worker that claimed event 1 15 minutes ago
    event_store[1]["claimed_at"] = datetime.now(timezone.utc) - timedelta(minutes=15)
    event_store[1]["claimed_by"] = "crashed-worker"

    reclaimed = await reap_stale_claims(session_factory)
    assert reclaimed == 1
    assert event_store[1]["claimed_at"] is None
    assert event_store[1]["claimed_by"] is None
    assert "[stale-claim-reaped]" in event_store[1]["last_error"]


@pytest.mark.asyncio
async def test_floodwait_retry(test_setup):
    """Verifies FloodWait is caught and retried automatically."""
    service, bot, event_store, user_store, session_factory = test_setup
    bot.fail_mode = "floodwait"
    bot.floodwait_seconds = 0
    bot.floodwait_remaining = 2  # Fail 2 times then succeed on 3rd

    sent_count = await service._dispatch_once()
    assert sent_count == 3
    assert event_store[1]["sent"] is True


@pytest.mark.asyncio
async def test_floodwait_exhausted_releases_claim(test_setup):
    """Verifies that when FloodWait retries are exhausted, the claim is released for the next cycle."""
    service, bot, event_store, user_store, session_factory = test_setup
    bot.fail_mode = "floodwait"
    bot.floodwait_seconds = 0
    bot.floodwait_remaining = 10  # Exceeds max 3 retries

    sent_count = await service._dispatch_once()
    assert sent_count == 0

    # Events should remain unsent but claim released so next cycle can try
    assert event_store[1]["sent"] is False
    assert event_store[1]["claimed_at"] is None
    assert "floodwait_exhausted" in (event_store[1]["last_error"] or "")
    # M1 fix: exactly ONE real failed attempt burned from the budget.
    assert event_store[1]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_user_blocked_bot_marked_permanent(test_setup):
    """Verifies TelegramForbiddenError transitions the event to permanent_failure = True."""
    service, bot, event_store, user_store, session_factory = test_setup
    bot.fail_mode = "blocked"

    sent_count = await service._dispatch_once()
    assert sent_count == 0

    for eid in [1, 2, 3]:
        ev = event_store[eid]
        assert ev["sent"] is True  # Marked sent to clear queue
        assert ev["permanent_failure"] is True
        assert "user blocked" in ev["last_error"]


@pytest.mark.asyncio
async def test_user_deactivated_marked_permanent(test_setup):
    """Verifies chat not found / deactivated transitions event to permanent failure."""
    service, bot, event_store, user_store, session_factory = test_setup
    bot.fail_mode = "deactivated"

    sent_count = await service._dispatch_once()
    assert sent_count == 0

    for eid in [1, 2, 3]:
        ev = event_store[eid]
        assert ev["sent"] is True
        assert ev["permanent_failure"] is True
        assert "deactivated" in ev["last_error"]


@pytest.mark.asyncio
async def test_orphaned_event_marked_permanent_not_silently_dropped(test_setup):
    """Verifies orphaned events (user_id not in users table) are marked permanent_failure."""
    service, bot, event_store, user_store, session_factory = test_setup
    # Remove user 1 from users table
    del user_store[1]

    sent_count = await service._dispatch_once()
    assert sent_count == 2  # Events 2 and 3 sent

    ev1 = event_store[1]
    assert ev1["sent"] is True
    assert ev1["permanent_failure"] is True
    assert "orphaned event" in ev1["last_error"]


@pytest.mark.asyncio
async def test_retry_exhaustion_permanent_failure(test_setup):
    """Verifies that an event failing persistently across 5 REAL attempts
    transitions to permanent_failure. (M1: only failed deliveries increment
    the counter — restarts/reclaims never burn budget for unsent events.)"""
    service, bot, event_store, user_store, session_factory = test_setup
    bot.fail_mode = "transient"

    # The event has already failed 5 real times.
    event_store[1]["attempt_count"] = 5

    sent_count = await service._dispatch_once()
    assert sent_count == 0

    ev1 = event_store[1]
    assert ev1["permanent_failure"] is True
    assert "exhausted 5 attempts" in ev1["last_error"]


@pytest.mark.asyncio
async def test_m1_repeated_claims_never_burn_attempt_budget(test_setup):
    """M1 core guarantee: claims/reclaims/restarts do NOT touch attempt_count.
    A notification that keeps getting claimed but never actually attempted
    (e.g. bot restarted mid-cycle) can never be silently dropped."""
    service, bot, event_store, user_store, session_factory = test_setup

    for _ in range(5):
        claimed = await claim_events(session_factory, worker_id="crasher")
        assert len(claimed) == 3
        # Simulate a crash: nothing sent, no release — just stale-claim the row.
        for r in claimed:
            event_store[r["id"]]["claimed_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=15)
            )
        await reap_stale_claims(session_factory)

        for eid in [1, 2, 3]:
            assert event_store[eid]["attempt_count"] == 0, (
                "claim/reap cycles must never consume retry budget"
            )

    # One REAL failed delivery burns exactly one attempt.
    bot.fail_mode = "transient"
    await service._dispatch_once()
    assert all(event_store[eid]["attempt_count"] == 1 for eid in [1, 2, 3])


# ── Per-user digest (spam fix, incident 2026-08-25) ───────────────────────

def _msg_event(eid: int, user_id: int, sender: str, subject: str) -> dict:
    return {
        "id": eid, "user_id": user_id, "event_type": "new_message_received",
        "payload_json": {"sender": sender, "subject": subject,
                         "body_snippet": "…", "has_attachment": False, "message_id": eid},
        "sent": False, "attempt_count": 0, "claimed_at": None, "permanent_failure": False,
    }


@pytest.mark.asyncio
async def test_message_backlog_digested_into_one_bubble(test_setup):
    """A burst of same-user message events (the racing-sync backlog scenario)
    must arrive as ONE digest bubble — not N back-to-back notifications —
    with every grouped event atomically marked sent after the single send."""
    service, bot, event_store, user_store, session_factory = test_setup

    event_store.clear()
    for i in range(1, 5):
        event_store[i] = _msg_event(i, user_id=3, sender=f"Prof {i}", subject=f"Notice {i}")

    sent_count = await service._dispatch_once()

    assert sent_count == 4
    assert len(bot.sent_messages) == 1, "digest must collapse N events into ONE message"
    text = bot.sent_messages[0]["text"]
    assert "4 new notices" in text
    for i in range(1, 5):
        assert f"Notice {i}" in text
    for eid in range(1, 5):
        ev = event_store[eid]
        assert ev["sent"] is True
        assert ev["attempt_count"] == 0


@pytest.mark.asyncio
async def test_mixed_event_types_never_digested(test_setup):
    """Attendance/absence alerts are safety-critical one-offs: a same-user mix
    of message + attendance events sends individually (no lossy merging)."""
    service, bot, event_store, user_store, session_factory = test_setup

    event_store.clear()
    event_store[1] = {
        "id": 1, "user_id": 3, "event_type": "attendance_updated",
        "payload_json": {"subject_name": "Maths", "subject_code": "MA1001",
                          "changes": {"tc": {"old": "10", "new": "11"}}},
        "sent": False, "attempt_count": 0, "claimed_at": None, "permanent_failure": False,
    }
    event_store[2] = _msg_event(2, user_id=3, sender="Dean", subject="Exam Schedule")

    sent_count = await service._dispatch_once()

    assert sent_count == 2
    assert len(bot.sent_messages) == 2, "mixed types must NOT merge into a digest"
    for eid in [1, 2]:
        assert event_store[eid]["sent"] is True


@pytest.mark.asyncio
async def test_digest_failure_releases_every_group_claim(test_setup):
    """If the digest send fails retryably, EVERY event in the group gets its
    claim released and exactly one real attempt burned — identical semantics
    to per-event dispatch."""
    service, bot, event_store, user_store, session_factory = test_setup

    event_store.clear()
    for i in range(1, 4):
        event_store[i] = _msg_event(i, user_id=3, sender=f"Prof {i}", subject=f"Notice {i}")

    bot.fail_mode = "transient"
    sent_count = await service._dispatch_once()

    assert sent_count == 0
    for eid in range(1, 4):
        ev = event_store[eid]
        assert ev["sent"] is False
        assert ev["claimed_at"] is None
        assert ev["attempt_count"] == 1
        assert "retryable" in (ev["last_error"] or "")


def test_digest_formatter_lists_senders_and_subjects():
    from app.services.event_dispatcher_service import _format_message_digest
    events = [
        {"event_type": "new_message_received",
         "payload_json": {"sender": "Dean", "subject": "Exams", "has_attachment": True}},
        {"event_type": "message_updated",
         "payload_json": {"sender": "Registrar", "subject": "Fees"}},
    ]
    text, markup = _format_message_digest(events)
    assert "1 new notice" in text
    assert "1 updated notice" in text
    assert "Dean" in text and "<b>Exams</b>" in text and "📎" in text
    assert "Registrar" in text and "<b>Fees</b>" in text
    assert markup is not None
