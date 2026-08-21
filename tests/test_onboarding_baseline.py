"""Tests for the post-registration inbox baseline (silent) sync path.

Verifies that persist_inbox_sync(baseline=True) inserts messages WITHOUT
creating NEW_MESSAGE_RECEIVED events (the "historical backlog spam" fix),
while baseline=False keeps the normal notify-on-new behavior.
"""

import pytest
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import app.workers.sync_worker as sw


class _FakeMsg:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _AsyncCM:
    """Tiny async context manager for ``async with session.begin():``."""
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


async def _noop_recovery(worker_name):
    return None


def _scraped_one():
    return [{
        "portal_message_id": "p1",
        "token": "tok1",  # non-postback → "recent" message path
        "sender": "Dean Academic",
        "subject": "Exam Notice",
        "sent_on": datetime(2026, 8, 20, tzinfo=timezone.utc),
    }]


def _setup(monkeypatch):
    """Wire the DB/session/repo fakes so persist_inbox_sync runs without a DB."""
    monkeypatch.setattr(sw, "wait_for_db_recovery", _noop_recovery)

    fake_session = MagicMock()
    fake_session.begin.return_value = _AsyncCM()
    fake_session.execute = AsyncMock()

    @asynccontextmanager
    async def _fake_get_db_session():
        yield fake_session

    monkeypatch.setattr(sw, "get_db_session", _fake_get_db_session)

    fake_inbox_repo = MagicMock()
    fake_inbox_repo.create_message = AsyncMock(return_value=_FakeMsg(
        id=1, portal_message_id="p1", sender="Dean Academic",
        subject="Exam Notice", body="hello", attachment_url=None,
    ))
    fake_inbox_repo.get_by_portal_message_ids = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.db.repositories.inbox_repository.InboxRepository",
        lambda session: fake_inbox_repo,
    )

    fake_event_repo = MagicMock()
    fake_event_repo.create_event = AsyncMock()
    monkeypatch.setattr(sw, "EventRepository", lambda session: fake_event_repo)

    return fake_event_repo


@pytest.mark.asyncio
async def test_baseline_sync_is_silent(monkeypatch):
    fake_event_repo = _setup(monkeypatch)
    await sw.persist_inbox_sync(1, _scraped_one(), {"p1": {"body": "hello", "attachment_url": None}}, {}, baseline=True)
    fake_event_repo.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_normal_sync_notifies(monkeypatch):
    fake_event_repo = _setup(monkeypatch)
    await sw.persist_inbox_sync(1, _scraped_one(), {"p1": {"body": "hello", "attachment_url": None}}, {}, baseline=False)
    fake_event_repo.create_event.assert_awaited_once()
