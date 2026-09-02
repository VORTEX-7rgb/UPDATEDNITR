"""Unit & regression tests for the 5 core stability and concurrency bug fixes:
1. Shared transport stays open when NitrisClient.close() is called.
2. _shielded_finish completes cleanup and re-raises CancelledError on cancellation.
3. Bubble ownership checks (check_bubble_owner & owner_token).
4. Timezone normalization to UTC and silent timestamp migration in persist_inbox_sync.
5. Session pool drops dead entry when login_through_gateway raises LoginError.
"""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.config import config, IST
from app.db.database import _shielded_finish
from app.db.models import EventType
from app.nitris.client import NitrisClient, get_shared_transport
from app.nitris.exceptions import LoginError, CredentialsQuarantinedError
from app.nitris.job_handlers import _edit_callback_message
from app.nitris.session_pool import with_pooled_session, _pool
from app.ui.surface import Surface, check_bubble_owner, claim_bubble
from app.workers.sync_worker import normalize_to_utc, persist_inbox_sync


# ── Fix 1: NitrisClient.close() does NOT close shared transport ─────────────

@pytest.mark.asyncio
async def test_nitris_client_close_preserves_shared_transport():
    shared_transport = get_shared_transport()
    client = NitrisClient()
    assert client.closed is False
    client.client.cookies.set("test_cookie", "123")

    await client.close()
    assert client.closed is True
    assert len(client.client.cookies) == 0
    # Crucial: transport must not be closed
    assert not getattr(shared_transport, "_is_closed", False)


# ── Fix 2: _shielded_finish task cancellation propagation & cleanup ─────────

@pytest.mark.asyncio
async def test_shielded_finish_cleanup_completes_and_propagates_cancellation():
    cleaned_up = False

    async def sample_cleanup():
        await asyncio.sleep(0.05)
        nonlocal cleaned_up
        cleaned_up = True

    async def outer_worker():
        try:
            await asyncio.sleep(10)
        finally:
            await _shielded_finish(sample_cleanup())

    task = asyncio.create_task(outer_worker())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleaned_up is True, "Cleanup task must finish completely even when cancelled"


# ── Fix 3: Bubble ownership checks ──────────────────────────────────────────

def test_surface_owner_token_property():
    fake_msg = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=456)
    surf = Surface(fake_msg)
    assert isinstance(surf.owner_token, int)
    assert surf.owner_token > 0
    assert check_bubble_owner(123, 456, surf.owner_token) is True

    # User navigates to newer screen on the same bubble
    newer_token = claim_bubble(fake_msg)
    assert newer_token != surf.owner_token
    assert check_bubble_owner(123, 456, surf.owner_token) is False
    assert check_bubble_owner(123, 456, newer_token) is True
    assert check_bubble_owner(123, 456, None) is True


@pytest.mark.asyncio
async def test_edit_callback_message_drops_stale_edit(monkeypatch):
    edited = []
    fake_bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=lambda **kw: edited.append(kw))
    )
    monkeypatch.setattr("app.nitris.job_handlers._bot", fake_bot)

    fake_msg = SimpleNamespace(chat=SimpleNamespace(id=888), message_id=999)
    old_token = claim_bubble(fake_msg)
    # User clicks away, claiming newer interaction
    new_token = claim_bubble(fake_msg)

    # Edit with stale token should be dropped
    await _edit_callback_message(888, 999, "Stale screen", token=old_token)
    assert len(edited) == 0

    # Edit with active token should proceed
    await _edit_callback_message(888, 999, "Active screen", token=new_token)
    assert len(edited) == 1
    assert edited[0]["text"] == "Active screen"


# ── Fix 4: Timezone normalization & silent timestamp migration ─────────────

def test_normalize_to_utc_converts_naive_ist_to_utc():
    # 24 Aug 2026 10:30 AM naive IST
    naive_ist = datetime(2026, 8, 24, 10, 30, 0)
    utc_dt = normalize_to_utc(naive_ist)

    assert utc_dt.tzinfo == timezone.utc
    # 10:30 IST is 05:00 UTC (5h 30m earlier)
    assert utc_dt.hour == 5
    assert utc_dt.minute == 0
    assert utc_dt.day == 24


@pytest.mark.asyncio
async def test_persist_inbox_sync_silent_timestamp_migration(monkeypatch):
    # Setup mocks for repository & session
    created_events = []
    existing_msg = SimpleNamespace(
        id=42,
        token="token_abc",
        portal_message_id="1001",
        subject="Fee Reminder",
        sender="Academic Office",
        sent_on=datetime(2026, 8, 24, 10, 30, 0, tzinfo=timezone.utc),  # Old skewed UTC
        body="Detailed body text",
        body_fetched_at=datetime.now(timezone.utc),
        attachment_url=None,
        attachment_cache_id=None,
        is_read=True,
    )

    class FakeInboxRepo:
        def __init__(self, session):
            pass
        async def get_by_portal_message_ids(self, user_id, pids):
            return [existing_msg]
        async def has_any_messages(self, user_id):
            return True

    class FakeEventRepo:
        def __init__(self, session):
            pass
        async def has_message_event(self, user_id, event_type, msg_id):
            return False
        async def create_event(self, **kw):
            created_events.append(kw)

    monkeypatch.setattr("app.db.repositories.inbox_repository.InboxRepository", FakeInboxRepo)
    monkeypatch.setattr("app.db.repositories.event_repository.EventRepository", FakeEventRepo)
    monkeypatch.setattr("app.workers.sync_worker.EventRepository", FakeEventRepo)
    monkeypatch.setattr("app.workers.sync_worker.wait_for_db_recovery", AsyncMock())

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def fake_get_session():
        session = MagicMock()
        session.execute = AsyncMock()
        @asynccontextmanager
        async def fake_begin():
            yield
        session.begin = fake_begin
        yield session

    monkeypatch.setattr("app.workers.sync_worker.get_db_session", fake_get_session)

    # Scraped message has the SAME subject, but correct IST parsed datetime
    corrected_naive_ist = datetime(2026, 8, 24, 10, 30, 0) # Normalizes to 05:00 UTC
    scraped = [{
        "portal_message_id": "1001",
        "token": "token_abc",
        "sender": "Academic Office",
        "subject": "Fee Reminder",  # UNCHANGED
        "sent_on": corrected_naive_ist,
    }]

    await persist_inbox_sync(user_id=1, scraped_messages=scraped, detail_cache={}, existing_by_id={})

    # Assert silent migration occurred:
    # 1. Timestamp updated to true UTC (05:00)
    assert existing_msg.sent_on.hour == 5
    # 2. Body was NOT wiped
    assert existing_msg.body == "Detailed body text"
    # 3. Read status was NOT reset
    assert existing_msg.is_read is True
    # 4. Zero notification events created
    assert len(created_events) == 0


# ── Fix 5: Session pool drops dead entry on login failure ───────────────────

@pytest.mark.asyncio
async def test_session_pool_drops_entry_on_login_failure(monkeypatch):
    _pool.clear()
    user_id = 999
    dropped = False

    async def fake_login(client, roll, password, *, user_id):
        raise LoginError("Invalid password")

    @contextlib_async_acquire
    async def fake_acquire():
        yield

    monkeypatch.setattr("app.nitris.gateway.nitris_gateway.acquire", fake_acquire)
    monkeypatch.setattr("app.nitris.gateway.nitris_gateway.login_through_gateway", fake_login)
    monkeypatch.setattr("app.nitris.session_pool.decrypt_password", lambda p: "pass")

    async def work(client, password):
        return "ok"

    with pytest.raises(LoginError):
        await with_pooled_session(
            user_id=user_id,
            roll_number="123CS001",
            encrypted_password="enc",
            work=work,
        )

    # Entry must have been dropped from _pool on login failure
    assert user_id not in _pool
    _pool.clear()


from contextlib import asynccontextmanager
def contextlib_async_acquire(func):
    return asynccontextmanager(func)
